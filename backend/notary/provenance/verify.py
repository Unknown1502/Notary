"""Live verification: re-derive every claim on a certificate from real bytes.

Resolves docs/SPIKES.md #4. The SDK's `Manifest.verify()` performs a Mode 1
integrity check -- it confirms the manifest is self-consistent. What a viewer
of a certificate actually wants to know is different and stronger:

    "Do the bytes sitting in Backblaze B2 *right now* still match what was
     certified, and did the holder of the signing key attest to it?"

Answering that requires fetching the object and hashing it. So Notary does,
streaming the asset in chunks rather than trusting any stored value.

The report is a list of independent checks, not one boolean, because the
partial failures carry the most information:

    bytes match + signature invalid   -> the record was re-signed or forged
    bytes differ + signature valid    -> the asset was swapped under a real
                                         certificate; the seal caught it
    both fail                         -> unrelated object at that key
    retention lapsed                  -> still verifiable, no longer immutable

Collapsing those into "verified: false" would throw away the diagnosis.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import UTC, datetime

import httpx

from ..models import Certificate, VerificationCheck, VerificationReport
from .signing import verify_signature

log = logging.getLogger(__name__)

CHUNK_SIZE = 1 << 20  # 1 MiB
MAX_VERIFY_BYTES = 512 << 20  # 512 MiB safety ceiling


class VerificationError(RuntimeError):
    pass


async def hash_remote_asset(
    url: str, *, client: httpx.AsyncClient | None = None
) -> tuple[str, int]:
    """Stream an object and return (sha256_hex, bytes_read).

    Streamed rather than buffered so verifying a large video does not load it
    into memory, and so the byte counter is honest about what was actually
    hashed.
    """
    owns_client = client is None
    client = client or httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=10.0))

    digest = hashlib.sha256()
    total = 0
    try:
        async with client.stream("GET", url, follow_redirects=True) as response:
            if response.status_code != 200:
                raise VerificationError(
                    f"asset fetch returned HTTP {response.status_code} for {url}"
                )
            async for chunk in response.aiter_bytes(CHUNK_SIZE):
                total += len(chunk)
                if total > MAX_VERIFY_BYTES:
                    raise VerificationError(
                        f"asset exceeds the {MAX_VERIFY_BYTES // (1 << 20)} MiB "
                        "verification ceiling"
                    )
                digest.update(chunk)
    except httpx.HTTPError as exc:
        raise VerificationError(f"could not fetch {url}: {exc}") from exc
    finally:
        if owns_client:
            await client.aclose()

    return digest.hexdigest(), total


async def verify_certificate(
    certificate: Certificate,
    *,
    client: httpx.AsyncClient | None = None,
    recomputed_manifest_hash: str | None = None,
) -> VerificationReport:
    """Re-verify a certificate end to end against live storage."""
    checks: list[VerificationCheck] = []
    bytes_hashed = 0

    # ------------------------------------------------------ 1. asset integrity
    try:
        observed_sha, bytes_hashed = await hash_remote_asset(
            certificate.asset_url, client=client
        )
        matched = observed_sha == certificate.sha256.lower()
        checks.append(
            VerificationCheck(
                name="asset_integrity",
                passed=matched,
                detail=(
                    f"Recomputed SHA-256 over {bytes_hashed:,} bytes fetched from "
                    "Backblaze B2 "
                    + ("matches" if matched else "DOES NOT match")
                    + " the certified digest."
                ),
                expected=certificate.sha256.lower(),
                observed=observed_sha,
            )
        )
    except VerificationError as exc:
        checks.append(
            VerificationCheck(
                name="asset_integrity",
                passed=False,
                detail=f"Could not verify asset bytes: {exc}",
                expected=certificate.sha256.lower(),
                observed=None,
            )
        )

    # --------------------------------------------------- 2. manifest integrity
    if recomputed_manifest_hash is not None:
        matched = recomputed_manifest_hash.lower() == certificate.manifest_hash.lower()
        checks.append(
            VerificationCheck(
                name="manifest_integrity",
                passed=matched,
                detail=(
                    "Canonical manifest hash recomputed from the stored manifest "
                    + ("matches." if matched else "DOES NOT match.")
                ),
                expected=certificate.manifest_hash.lower(),
                observed=recomputed_manifest_hash.lower(),
            )
        )

    # ---------------------------------------------------------- 3. signature
    if certificate.signature is not None:
        target_hash = recomputed_manifest_hash or certificate.manifest_hash
        valid = verify_signature(certificate.signature, target_hash)
        checks.append(
            VerificationCheck(
                name="signature",
                passed=valid,
                detail=(
                    f"Ed25519 signature by key '{certificate.signature.key_id}' "
                    + ("verifies against" if valid else "FAILS against")
                    + " the canonical manifest hash (Genblaze Trust Mode 2)."
                ),
                expected=certificate.signature.key_id,
                observed=certificate.signature.public_key[:24] + "…",
            )
        )
    else:
        checks.append(
            VerificationCheck(
                name="signature",
                passed=False,
                detail=(
                    "No signature present. This certificate is Trust Mode 1 "
                    "(integrity only): it proves the bytes are unchanged, not "
                    "who attested to them."
                ),
            )
        )

    # ------------------------------------------------------- 4. object lock
    now = datetime.now(UTC)
    sealed = certificate.object_lock_mode != "NONE" and certificate.retention_until > now
    if sealed:
        remaining = certificate.retention_until - now
        detail = (
            f"Object Lock ({certificate.object_lock_mode}) is in force for "
            f"another {remaining.days} day(s), until "
            f"{certificate.retention_until:%Y-%m-%d %H:%M UTC}. Neither this "
            "asset nor its verdict can be altered or deleted before then, by "
            "anyone — including the account owner."
        )
    elif certificate.object_lock_mode == "NONE":
        detail = (
            "This object is not under Object Lock. Its integrity is verifiable "
            "but its persistence is not guaranteed."
        )
    else:
        detail = (
            f"Object Lock retention lapsed on "
            f"{certificate.retention_until:%Y-%m-%d}. The record remains "
            "verifiable but is no longer immutable."
        )

    checks.append(
        VerificationCheck(
            name="object_lock",
            passed=sealed,
            detail=detail,
            expected=certificate.object_lock_mode,
            observed=certificate.retention_until.isoformat(),
        )
    )

    return VerificationReport(
        certificate_id=certificate.certificate_id,
        checks=checks,
        bytes_hashed=bytes_hashed,
        source=certificate.asset_url,
    )


def summarize(report: VerificationReport) -> str:
    if report.passed:
        return (
            f"All {len(report.checks)} checks passed. "
            f"{report.bytes_hashed:,} bytes re-hashed from B2."
        )
    failed = [c.name for c in report.checks if not c.passed]
    return f"{len(failed)} of {len(report.checks)} checks failed: {', '.join(failed)}."
