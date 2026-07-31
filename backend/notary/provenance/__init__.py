"""Provenance: signing, certificate assembly, and live verification.

Notary implements Genblaze **Trust Mode 2** (authenticated integrity), which
the SDK defines but has not yet shipped. The manifest's `signature` field is
reserved and excluded from the canonical hash precisely so that an implementer
can fill it in without invalidating the hash. That is what signing.py does.

Mode 3 (C2PA) remains roadmap here too, and is described honestly as such in
docs/TRUST-MODEL.md rather than implied.
"""

from .certificate import (
    build_certificate,
    build_verdict_document,
    canonical_json,
    certificate_document,
    hash_payload,
)
from .signing import (
    SigningIdentity,
    SigningUnavailable,
    apply_to_manifest,
    generate_key,
    load_identity,
    load_or_create,
    sign_manifest_hash,
    signature_from_manifest,
    verify_signature,
)
from .verify import (
    VerificationError,
    hash_remote_asset,
    summarize,
    verify_certificate,
)

__all__ = [
    "SigningIdentity",
    "SigningUnavailable",
    "VerificationError",
    "apply_to_manifest",
    "build_certificate",
    "build_verdict_document",
    "canonical_json",
    "certificate_document",
    "generate_key",
    "hash_payload",
    "hash_remote_asset",
    "load_identity",
    "load_or_create",
    "sign_manifest_hash",
    "signature_from_manifest",
    "summarize",
    "verify_certificate",
    "verify_signature",
]
