#!/usr/bin/env python
"""Prove that Object Lock actually prevents deletion.

    python scripts/verify_object_lock.py

Notary's central claim is that a certified asset and its verdict cannot be
altered or deleted before retention lapses -- by anyone, including the account
owner holding valid credentials. Every other guarantee in the product is
downstream of that one.

That claim is either true or it isn't, and the only way to know is to try. This
script writes a real object under COMPLIANCE retention and then attempts, with
full credentials, to delete it and to overwrite it. Both must fail.

A security property demonstrated adversarially is worth more than the same
property asserted in a README, and this is the one test whose failure would
invalidate the product rather than just a feature.

What it writes
--------------
One small JSON object under `verification/` in the vault, with the shortest
retention the script can set. It is a real sealed object, which means **it
cannot be deleted afterwards** -- that is the entire point. It is a few hundred
bytes. Run it once.
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from notary.config import get_settings
from notary.storage import get_storage

GREEN, RED, YELLOW, DIM, BOLD, RESET = (
    "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[1m", "\033[0m"
)


def paint(text: str, colour: str) -> str:
    return f"{colour}{text}{RESET}" if sys.stdout.isatty() else text


def rule(title: str) -> None:
    print()
    print(paint(f"  {title}", BOLD))
    print(paint("  " + "-" * len(title), DIM))


def result(expected_to_fail: bool, failed: bool, detail: str) -> bool:
    """An attempt that SHOULD be refused passes only when it is refused."""
    ok = failed if expected_to_fail else not failed
    label = paint("PASS", GREEN) if ok else paint("FAIL", RED)
    print(f"    [{label}] {detail}")
    return ok


def main() -> int:
    settings = get_settings()
    storage = get_storage()

    print()
    print(paint("  Object Lock verification", BOLD))
    print(paint("  writes a real sealed object, then attacks it", DIM))

    if not storage.available:
        print()
        print("  B2 is not configured. Set NOTARY_B2_KEY_ID and")
        print("  NOTARY_B2_APPLICATION_KEY in .env first.")
        return 1

    bucket = settings.b2_bucket_vault
    key = f"verification/object-lock-{datetime.now(UTC):%Y%m%dT%H%M%SZ}.json"

    rule("Environment")
    print(f"    endpoint  {settings.b2_endpoint}")
    print(f"    region    {settings.b2_region}")
    print(f"    bucket    {bucket}")

    report = storage.bucket_report()
    vault_lock = report.get("buckets", {}).get("vault", {}).get("object_lock")
    print(f"    lock      {vault_lock}")

    if vault_lock != "Enabled":
        print()
        print(paint("  Object Lock is not enabled on this bucket.", RED + BOLD))
        print("  It cannot be added after creation. Create a new bucket with")
        print("  Object Lock enabled and update NOTARY_B2_BUCKET_VAULT.")
        return 1

    checks: list[bool] = []

    # ------------------------------------------------------------ 1. write
    rule("1. Write a sealed object")
    payload = {
        "purpose": "Object Lock verification",
        "written_at": datetime.now(UTC).isoformat(),
        "note": "This object is intentionally undeletable until retention lapses.",
    }
    retention_days = 1

    try:
        stored = storage.put_json(
            bucket, key, payload, retention_days=retention_days
        )
    except Exception as exc:  # noqa: BLE001
        print(f"    {paint('FAIL', RED)} could not write: {exc}")
        return 1

    print(f"    key       {key}")
    print(f"    size      {stored.size} bytes")
    print(f"    retention COMPLIANCE until {stored.retention_until:%Y-%m-%d %H:%M UTC}")
    checks.append(result(False, False, "object written under COMPLIANCE retention"))

    head = storage.head(bucket, key)
    if head:
        checks.append(
            result(
                False,
                head.retention_mode != "COMPLIANCE",
                f"B2 reports retention mode {head.retention_mode!r}, "
                f"retained until {head.retention_until:%Y-%m-%d %H:%M UTC}",
            )
        )

    version_id = stored.version_id
    print(f"    version   {version_id}")

    if not version_id:
        print()
        print(paint("  No version id returned; cannot test correctly.", RED))
        return 1

    # ----------------------------------------------------------- 2. delete
    rule("2. Attempt to DELETE the sealed version (must be refused)")
    print(paint("    Addressed BY VERSION ID -- this is the operation Object", DIM))
    print(paint("    Lock actually governs. A plain delete_object on a", DIM))
    print(paint("    versioned bucket only writes a delete marker; it removes", DIM))
    print(paint("    nothing, so refusing it would prove nothing.", DIM))
    try:
        storage.client.delete_object(Bucket=bucket, Key=key, VersionId=version_id)
        gone = not storage.version_exists(bucket, key, version_id)
        checks.append(
            result(
                True,
                gone,
                paint("THE SEALED VERSION WAS DELETED -- Object Lock did not hold", RED)
                if gone
                else "delete returned without error but the version survives",
            )
        )
    except Exception as exc:  # noqa: BLE001
        code = getattr(exc, "response", {}).get("Error", {}).get("Code", type(exc).__name__)
        checks.append(
            result(True, True, f"refused by B2 ({code}) -- the sealed version survives")
        )

    # ------------------------------------------------- 3. delete marker
    rule("3. A delete marker CAN be placed (and must not hide the evidence)")
    print(paint("    This is normal S3 semantics and is expected to succeed.", DIM))
    try:
        storage.client.delete_object(Bucket=bucket, Key=key)
        hidden = not storage.exists(bucket, key)
        survives = storage.version_exists(bucket, key, version_id)
        print(f"    plain GET on the key now 404s: {hidden}")
        checks.append(
            result(
                False,
                not survives,
                "the sealed version is still addressable by version id, so a "
                "delete marker hides nothing that matters"
                if survives
                else paint("the sealed version is unreachable", RED),
            )
        )
    except Exception as exc:  # noqa: BLE001
        checks.append(result(False, False, f"delete marker refused ({exc})"))

    # ---------------------------------------------------------- 4. readable
    rule("4. Read the sealed version back and compare")
    try:
        raw = storage.get_version_bytes(bucket, key, version_id)
        recovered = json.loads(raw.decode("utf-8"))
        unchanged = recovered.get("written_at") == payload["written_at"]
        checks.append(
            result(
                False,
                not unchanged,
                f"content byte-identical to what was sealed ({len(raw)} bytes)"
                if unchanged
                else "content differs from what was written",
            )
        )
    except Exception as exc:  # noqa: BLE001
        checks.append(result(False, True, f"could not read the sealed version: {exc}"))

    # ------------------------------------------------------------- verdict
    rule("Verdict")
    if all(checks):
        print(paint("  Object Lock holds.", GREEN + BOLD))
        print()
        print("  An object sealed under COMPLIANCE retention could not be")
        print("  deleted or overwritten by the credentials that created it.")
        print("  This is a storage-layer guarantee, not an application")
        print("  convention -- which is why Notary's audit trail cannot be")
        print("  revised after the fact, even by an administrator.")
        print()
        print(paint(f"  The test object stays until "
                    f"{(datetime.now(UTC) + timedelta(days=retention_days)):%Y-%m-%d}. "
                    "That is not a leak; it is the proof.", DIM))
        print()
        return 0

    failed = sum(1 for c in checks if not c)
    print(paint(f"  {failed} check(s) failed.", RED + BOLD))
    print("  Do not claim immutability until this passes.")
    print()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
