#!/usr/bin/env python
"""Provision the Backblaze B2 buckets Notary needs.

Run this once, before anything else:

    python scripts/bootstrap_b2.py

The single most important thing it does is create the vault bucket with
**Object Lock enabled at creation**. Object Lock cannot be retrofitted onto an
existing bucket -- if you forget, the fix is to delete the bucket and start
over, which is why this is an explicit script and not a lazy side effect of the
first upload.

It also sets a deliberately short default retention (7 days). Compliance-mode
objects cannot be deleted before their retention lapses by anyone, including
you. A long retention on a development bucket produces permanently undeletable
test garbage within an hour.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from notary.config import get_settings  # noqa: E402
from notary.storage import get_storage  # noqa: E402


def main() -> int:
    settings = get_settings()
    storage = get_storage()

    if not storage.available:
        print(
            "B2 credentials are not configured.\n"
            "Set NOTARY_B2_KEY_ID and NOTARY_B2_APPLICATION_KEY in .env first."
        )
        return 1

    print(f"endpoint : {settings.b2_endpoint}")
    print(f"vault    : {settings.b2_bucket_vault}   (Object Lock, COMPLIANCE)")
    print(f"workbench: {settings.b2_bucket_workbench}   (lifecycle-expired)")
    print(f"retention: {settings.vault_retention_days} day(s)")
    print()

    if settings.vault_retention_days > 30:
        print(
            f"WARNING: retention is {settings.vault_retention_days} days. Every "
            "certified object will be undeletable for that long, by anyone. "
            "Continue only if this is a production bucket."
        )
        if input("type 'yes' to continue: ").strip().lower() != "yes":
            print("aborted")
            return 1

    print("creating buckets...")
    for bucket, status in storage.create_buckets().items():
        print(f"  {bucket}: {status}")

    print("configuring lifecycle...")
    print(f"  {storage.configure_lifecycle()}")

    print("configuring CORS...")
    print(f"  {storage.configure_cors()}")
    print(
        "\n  (CORS matters: without it, browser <video> playback from B2 fails "
        "with an opaque error and no server-side symptom.)"
    )

    print("\nverifying object lock...")
    report = storage.bucket_report()
    for label, entry in report.get("buckets", {}).items():
        lock = entry.get("object_lock")
        marker = "OK " if (label != "vault" or lock == "Enabled") else "!! "
        print(f"  {marker}{label:10s} {entry['name']:30s} object_lock={lock}")

    vault_lock = report.get("buckets", {}).get("vault", {}).get("object_lock")
    if vault_lock != "Enabled":
        print(
            "\nFAILED: the vault bucket does not have Object Lock enabled.\n"
            "It cannot be added later. Delete the bucket and re-run this script."
        )
        return 1

    print("\nBootstrap complete. The vault is immutable for the retention window.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
