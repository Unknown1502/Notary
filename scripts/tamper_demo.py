#!/usr/bin/env python
"""Attack a real Notary certificate and watch the seal catch it.

    python scripts/tamper_demo.py

Runs entirely offline with real cryptography -- a real Ed25519 keypair, a real
signature over a real canonical hash. Nothing here is simulated.

Why this exists
---------------
"Tamper-evident" is a claim, and a claim a reviewer cannot test is a claim they
should discount. This script runs four attacks against a genuine certificate
and shows which are caught and -- importantly -- which are not:

    1. Modify the video after certification          -> caught
    2. Modify the sealed verdict                     -> caught
    3. Forge a certificate with the attacker's key   -> caught
    4. Re-sign modified content with the STOLEN key  -> NOT caught

The fourth is the honest one. If the private key is compromised, signatures
prove nothing, and no amount of cryptography fixes that. It is included
deliberately, because a security demo that only shows its wins is marketing.
Object Lock is what constrains attack 4 in a real deployment: the attacker can
mint a new record, but cannot replace the sealed one.
"""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from notary.provenance import signing

GREEN, RED, YELLOW, DIM, BOLD, RESET = (
    "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[1m", "\033[0m"
)


def paint(text: str, colour: str) -> str:
    return f"{colour}{text}{RESET}" if sys.stdout.isatty() else text


def rule(title: str = "") -> None:
    print()
    if title:
        print(paint(f"  {title}", BOLD))
        print(paint("  " + "-" * (len(title)), DIM))


def canonical(payload: dict) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def manifest_hash(payload: dict) -> str:
    return hashlib.sha256(canonical(payload).encode("utf-8")).hexdigest()


def outcome(caught: bool, message: str) -> None:
    label = paint("CAUGHT", GREEN) if caught else paint("NOT CAUGHT", RED)
    print(f"    -> [{label}] {message}")


def main() -> int:
    workdir = Path(tempfile.mkdtemp(prefix="notary-tamper-"))

    print()
    print(paint("  Notary -- tamper demonstration", BOLD))
    print(paint("  real Ed25519 keys, real signatures, real hashes", DIM))

    # ---------------------------------------------------------------- setup
    rule("Setting up a genuine certified asset")

    video = workdir / "asset.mp4"
    video.write_bytes(b"\x00\x00\x00\x20ftypisom" + b"GENUINE VIDEO PAYLOAD " * 64)
    asset_sha = hashlib.sha256(video.read_bytes()).hexdigest()
    print(f"    video      {len(video.read_bytes()):,} bytes")
    print(f"    sha256     {asset_sha}")

    verdict = {
        "decision": "verified",
        "criteria": [
            {"id": "palette_adherence", "outcome": "pass", "check_kind": "deterministic",
             "severity": "blocking", "measurement": {"coverage": 0.94, "mean_delta_e": 4.1}},
            {"id": "mandatory_disclosure", "outcome": "pass", "check_kind": "deterministic",
             "severity": "blocking"},
            {"id": "visual_artifacts", "outcome": "pass", "check_kind": "perceptual",
             "severity": "blocking", "confidence": 0.88},
        ],
        "summary": "All criteria cleared.",
    }

    manifest = {
        "run_id": "run-genuine-001",
        "asset_sha256": asset_sha,
        "provider": "gmicloud",
        "model": "kling-image2video-v2.1-master",
        "verdict_digest": hashlib.sha256(canonical(verdict).encode()).hexdigest(),
    }
    original_hash = manifest_hash(manifest)
    print(f"    manifest   {original_hash}")

    identity = signing.generate_key(workdir / "notary.pem")
    block = signing.sign_manifest_hash(original_hash, identity)
    print(f"    signed     Ed25519, key '{identity.key_id}'")
    print(f"    public key {identity.public_key_b64}")

    rule("Baseline: the untampered record")
    valid = signing.verify_signature(block, original_hash)
    bytes_ok = hashlib.sha256(video.read_bytes()).hexdigest() == asset_sha
    print(f"    signature verifies : {paint('yes', GREEN) if valid else paint('no', RED)}")
    print(f"    bytes unchanged    : {paint('yes', GREEN) if bytes_ok else paint('no', RED)}")

    # ------------------------------------------------------------ attack 1
    rule("Attack 1 -- swap the video, keep the certificate")
    print(paint("    An attacker replaces the certified video with different", DIM))
    print(paint("    content and serves the original certificate alongside it.", DIM))

    video.write_bytes(b"\x00\x00\x00\x20ftypisom" + b"TAMPERED VIDEO PAYLOAD " * 64)
    new_sha = hashlib.sha256(video.read_bytes()).hexdigest()
    print(f"    certificate claims  {asset_sha[:32]}...")
    print(f"    file now hashes to  {new_sha[:32]}...")
    outcome(new_sha != asset_sha,
            "recomputing SHA-256 over the served bytes exposes the swap")

    video.write_bytes(b"\x00\x00\x00\x20ftypisom" + b"GENUINE VIDEO PAYLOAD " * 64)

    # ------------------------------------------------------------ attack 2
    rule("Attack 2 -- rewrite the verdict after approval")
    print(paint("    A criterion that actually failed is edited to 'pass',", DIM))
    print(paint("    to make a rejected asset look like it cleared review.", DIM))

    forged_verdict = json.loads(json.dumps(verdict))
    forged_verdict["criteria"][0]["outcome"] = "fail"
    forged_digest = hashlib.sha256(canonical(forged_verdict).encode()).hexdigest()

    tampered_manifest = dict(manifest, verdict_digest=forged_digest)
    tampered_hash = manifest_hash(tampered_manifest)

    print(f"    original manifest hash  {original_hash[:32]}...")
    print(f"    tampered manifest hash  {tampered_hash[:32]}...")
    outcome(
        not signing.verify_signature(block, tampered_hash),
        "the verdict is inside the signed manifest, so editing it breaks the signature",
    )

    # ------------------------------------------------------------ attack 3
    rule("Attack 3 -- forge a certificate with a different key")
    print(paint("    The attacker generates their own keypair and signs a", DIM))
    print(paint("    manifest of their choosing, then claims it is ours.", DIM))

    attacker = signing.generate_key(workdir / "attacker.pem")
    forged_block = signing.sign_manifest_hash(tampered_hash, attacker)
    print(f"    our public key      {identity.public_key_b64[:36]}...")
    print(f"    attacker public key {attacker.public_key_b64[:36]}...")

    forged_block.public_key = identity.public_key_b64  # claim to be us
    outcome(
        not signing.verify_signature(forged_block, tampered_hash),
        "a signature made by another key fails against our published public key",
    )

    # ------------------------------------------------------------ attack 4
    rule("Attack 4 -- re-sign tampered content with a STOLEN key")
    print(paint("    The attacker has exfiltrated the private key and", DIM))
    print(paint("    re-signs their modified manifest with it.", DIM))

    stolen_block = signing.sign_manifest_hash(tampered_hash, identity)
    caught = not signing.verify_signature(stolen_block, tampered_hash)
    outcome(caught, "signature check")

    print()
    print(paint("    This one succeeds, and Notary says so.", YELLOW + BOLD))
    print(paint("    A signature proves possession of a key, nothing more. If the", DIM))
    print(paint("    key is compromised, no signature scheme detects it.", DIM))
    print()
    print("    What still constrains this attacker in a real deployment:")
    print(paint("      * Object Lock (COMPLIANCE) means the original sealed record", DIM))
    print(paint("        cannot be overwritten or deleted before retention lapses --", DIM))
    print(paint("        not by an admin, not by the account owner, not by them.", DIM))
    print(paint("        They can mint a NEW record; they cannot revise THIS one.", DIM))
    print(paint("      * The two records now conflict, and the sealed one is", DIM))
    print(paint("        provably older and immutable.", DIM))
    print(paint("      * Production key custody belongs in a KMS, so the key is", DIM))
    print(paint("        never exfiltratable as a file. See docs/OPERATIONS.md.", DIM))

    # ---------------------------------------------------------------- close
    rule("Summary")
    rows = [
        ("Swap the certified video", True),
        ("Rewrite the sealed verdict", True),
        ("Forge with a different key", True),
        ("Re-sign with a stolen key", False),
    ]
    for label, detected in rows:
        mark = paint("caught", GREEN) if detected else paint("not caught", RED)
        print(f"    {label:34s} {mark}")

    print()
    print(paint("  Three of four attacks are defeated by cryptography alone.", BOLD))
    print(paint("  The fourth is defeated by key custody and storage immutability,", BOLD))
    print(paint("  not by signatures -- which is why Notary uses Object Lock too.", BOLD))
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
