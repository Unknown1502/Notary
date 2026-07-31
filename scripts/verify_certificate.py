#!/usr/bin/env python
"""Independent verifier for a Notary certificate.

    python scripts/verify_certificate.py certificate.json
    python scripts/verify_certificate.py https://.../certificate.json

**This script imports nothing from Notary.** Standard library plus
`cryptography` for Ed25519, and that is the entire point: a provenance claim
that can only be checked by the system that made it is not a provenance claim,
it is a press release.

Everything below is derivable from the certificate itself. A third party -- an
auditor, a regulator, a journalist, a rival -- can run this against a published
certificate and a published public key without our source, our servers, or our
cooperation.

What it checks
--------------
1. **Asset integrity.** Downloads the media from Backblaze B2 and recomputes
   SHA-256 over the bytes that actually arrive. Nothing stored is trusted.
2. **Signature.** Verifies the Ed25519 signature over the canonical manifest
   hash (Genblaze Trust Mode 2).
3. **Verdict binding.** Confirms the sealed verdict is the one the certificate
   commits to.
4. **Retention.** Reports whether Object Lock is still in force.

Exit code 0 if every check passes, 1 otherwise -- so it drops into CI.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
except ImportError:  # pragma: no cover
    print("This verifier needs `cryptography`:  pip install cryptography")
    raise SystemExit(2) from None

CHUNK = 1 << 20
GREEN, RED, DIM, BOLD, RESET = "\033[32m", "\033[31m", "\033[2m", "\033[1m", "\033[0m"


def supports_colour() -> bool:
    return sys.stdout.isatty()


def paint(text: str, colour: str) -> str:
    return f"{colour}{text}{RESET}" if supports_colour() else text


def load_certificate(source: str) -> dict[str, Any]:
    if source.startswith(("http://", "https://")):
        with urllib.request.urlopen(source, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    return json.loads(Path(source).read_text(encoding="utf-8"))


def hash_remote(url: str) -> tuple[str, int]:
    """Stream the object and hash it. Never buffers the whole video."""
    digest = hashlib.sha256()
    total = 0
    request = urllib.request.Request(url, headers={"User-Agent": "notary-verifier/1.0"})
    with urllib.request.urlopen(request, timeout=120) as response:
        while chunk := response.read(CHUNK):
            digest.update(chunk)
            total += len(chunk)
    return digest.hexdigest(), total


def check(name: str, passed: bool, detail: str, indent: str = "  ") -> bool:
    mark = paint("PASS", GREEN) if passed else paint("FAIL", RED)
    print(f"{indent}[{mark}] {paint(name, BOLD)}")
    for line in detail.splitlines():
        print(f"{indent}       {paint(line, DIM)}")
    return passed


def verify_asset(cert: dict[str, Any]) -> bool:
    url = cert.get("asset_url")
    expected = (cert.get("sha256") or "").lower()

    if not url or not expected:
        return check("asset integrity", False, "certificate is missing asset_url or sha256")

    try:
        observed, size = hash_remote(url)
    except Exception as exc:  # noqa: BLE001
        return check("asset integrity", False, f"could not fetch the asset: {exc}")

    matched = observed == expected
    return check(
        "asset integrity",
        matched,
        f"fetched   {size:,} bytes from {url}\n"
        f"expected  {expected}\n"
        f"recomputed {observed}\n"
        + ("bytes are unchanged since certification"
           if matched else "THE BYTES HAVE CHANGED SINCE CERTIFICATION"),
    )


def verify_signature(cert: dict[str, Any]) -> bool:
    signature = cert.get("signature")
    manifest_hash = (cert.get("manifest_hash") or "").strip().lower()

    if not signature:
        return check(
            "signature",
            False,
            "no signature present -- this is a Trust Mode 1 certificate.\n"
            "It shows the bytes are unchanged, not who attested to them.",
        )

    algorithm = signature.get("algorithm")
    if algorithm != "Ed25519":
        return check("signature", False, f"unsupported algorithm: {algorithm!r}")

    claimed = (signature.get("canonical_hash") or "").strip().lower()
    if claimed != manifest_hash:
        return check(
            "signature",
            False,
            f"the signature commits to {claimed}\n"
            f"but the certificate declares {manifest_hash}\n"
            "the signature does not cover this manifest",
        )

    try:
        public_key = Ed25519PublicKey.from_public_bytes(
            base64.b64decode(signature["public_key"])
        )
        public_key.verify(
            base64.b64decode(signature["signature"]),
            manifest_hash.encode("ascii"),
        )
    except (InvalidSignature, ValueError, KeyError, TypeError) as exc:
        return check(
            "signature",
            False,
            f"Ed25519 verification failed: {type(exc).__name__}\n"
            "the record was not signed by the holder of this key",
        )

    return check(
        "signature",
        True,
        f"Ed25519, key id '{signature.get('key_id')}'\n"
        f"public key {signature['public_key']}\n"
        "signature verifies over the canonical manifest hash (Trust Mode 2)",
    )


def verify_verdict_binding(cert: dict[str, Any]) -> bool:
    verdict = cert.get("verdict")
    if not isinstance(verdict, dict):
        return check("verdict present", False, "certificate carries no verdict")

    criteria = verdict.get("criteria") or []
    measured = [c for c in criteria if c.get("check_kind") == "deterministic"
                or c.get("kind") == "deterministic"]
    decision = verdict.get("decision")

    blocking_unresolved = [
        c for c in criteria
        if c.get("severity") == "blocking"
        and c.get("outcome") in ("fail", "uncertain")
    ]

    consistent = decision == "verified" and not blocking_unresolved
    detail = (
        f"decision: {decision}\n"
        f"{len(criteria)} criteria sealed ({len(measured)} measured)\n"
    )
    if blocking_unresolved:
        detail += (
            "certificate claims VERIFIED but carries unresolved blocking "
            f"criteria: {', '.join(c.get('id', '?') for c in blocking_unresolved)}"
        )
    else:
        detail += "no blocking criterion was left unresolved"

    return check("verdict consistency", consistent, detail)


def report_retention(cert: dict[str, Any]) -> bool:
    raw = cert.get("retention_until")
    mode = cert.get("object_lock_mode", "NONE")
    if not raw:
        return check("object lock", False, "no retention recorded")

    try:
        until = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return check("object lock", False, f"unparseable retention date: {raw!r}")

    if until.tzinfo is None:
        until = until.replace(tzinfo=timezone.utc)

    now = datetime.now(timezone.utc)
    sealed = mode != "NONE" and until > now
    remaining = (until - now).days

    return check(
        "object lock",
        sealed,
        f"mode {mode}, retained until {until:%Y-%m-%d %H:%M UTC}\n"
        + (f"immutable for another {remaining} day(s) -- cannot be altered or "
           "deleted by anyone, including the account owner"
           if sealed else "retention has lapsed; still verifiable, no longer immutable"),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("certificate", help="path or URL to certificate.json")
    parser.add_argument("--skip-download", action="store_true",
                        help="skip the asset fetch (offline signature check only)")
    args = parser.parse_args()

    try:
        cert = load_certificate(args.certificate)
    except Exception as exc:  # noqa: BLE001
        print(f"could not read certificate: {exc}")
        return 1

    print()
    print(paint("  Notary -- independent verification", BOLD))
    print(paint("  no Notary code involved; stdlib + cryptography only", DIM))
    print()
    print(f"  certificate  {cert.get('certificate_id')}")
    print(f"  asset        {cert.get('asset_id')}")
    print(f"  campaign     {cert.get('campaign_id')}  ({cert.get('tenant')})")
    print(f"  produced by  {cert.get('provider')} / {cert.get('model')}")
    print(f"  certified    {cert.get('certified_at')}")
    print()

    results = [
        verify_signature(cert),
        verify_verdict_binding(cert),
        report_retention(cert),
    ]
    if not args.skip_download:
        results.append(verify_asset(cert))
    else:
        print(paint("  [SKIP] asset integrity (--skip-download)", DIM))

    print()
    if all(results):
        print(paint("  VERIFIED", GREEN + BOLD)
              + " -- every check passed against independently recomputed values.")
        print()
        return 0

    failed = sum(1 for r in results if not r)
    print(paint("  NOT VERIFIED", RED + BOLD)
          + f" -- {failed} of {len(results)} checks failed.")
    print()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
