#!/usr/bin/env python
"""Generate the Ed25519 signing key for Genblaze Trust Mode 2.

    python scripts/generate_key.py

The private key is written unencrypted to keys/notary-ed25519.pem. That is
acceptable for development and explicitly not acceptable for production, where
the key belongs in a KMS or HSM and should never touch the filesystem. See
docs/OPERATIONS.md#key-custody.

Regenerating the key invalidates every certificate signed with the previous
one, so this refuses to overwrite an existing key unless --force is passed.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from notary.config import get_settings  # noqa: E402
from notary.provenance import generate_key  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--force",
        action="store_true",
        help="overwrite an existing key (invalidates every prior signature)",
    )
    args = parser.parse_args()

    settings = get_settings()
    path = settings.signing_key_path

    try:
        identity = generate_key(path, overwrite=args.force)
    except FileExistsError as exc:
        print(f"{exc}\n\nPass --force only if you accept invalidating old signatures.")
        return 1

    print(f"private key : {path}")
    print(f"key id      : {settings.signing_key_id}")
    print(f"algorithm   : Ed25519")
    print(f"public key  : {identity.public_key_b64}")
    print()
    print("Public key PEM (publish this so third parties can verify):")
    print(identity.public_key_pem())
    print(
        "Keep the private key out of version control. keys/ is gitignored.\n"
        "Anyone holding it can issue certificates in this deployment's name."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
