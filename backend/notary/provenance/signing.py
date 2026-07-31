"""Genblaze Trust Mode 2 — authenticated integrity via Ed25519.

What the SDK ships, and what it doesn't
---------------------------------------
`docs/features/trust-modes.md` defines three levels and ships only the first:

    Mode 1  Integrity          The manifest hasn't changed and the assets match
                               their committed hashes. Shipped today.
    Mode 2  Authenticated      Adds signing "via a pluggable interface with
            integrity          Ed25519 as the default". On the roadmap.
    Mode 3  Standards-         C2PA compatibility. On the roadmap.
            verifiable

Mode 1's limitation is stated plainly in those docs: "A tamperer can modify the
asset, recompute the manifest, re-embed, and produce a manifest that verifies
against itself." Integrity proves nothing changed since *someone* committed it.
It cannot say who.

And then the load-bearing detail:

    "The `signature` and `encryption_scheme` fields on `Manifest` are reserved
     (excluded from the canonical hash) for forward compatibility."

The socket is open. This module plugs into it.

Why the exclusion matters
-------------------------
A signature cannot be part of what it signs -- writing the signature into the
manifest would change the manifest, invalidating the signature. The SDK already
solved this by excluding `signature` from the canonical hash. So the protocol is
clean and needs no wrapper format:

    1. Genblaze computes canonical_hash over the manifest, signature excluded.
    2. Notary signs those hash bytes with an Ed25519 private key.
    3. Notary writes the signature into the reserved field.
    4. canonical_hash is unchanged, because the field is excluded.
    5. A verifier recomputes canonical_hash, then checks the signature over it.

What this does and does not buy you
-----------------------------------
Signing answers "did the holder of key K attest to this manifest?" It does NOT
prove the media is real, that the model behaved, or that the key wasn't stolen.
Key custody is the whole trust anchor, and in this deployment the key is a local
PEM file -- adequate for a demo, not for production. docs/TRUST-MODEL.md states
that limitation rather than papering over it, and OPERATIONS.md describes the
KMS path.
"""

from __future__ import annotations

import base64
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from ..models import SignatureBlock

log = logging.getLogger(__name__)


class SigningUnavailable(RuntimeError):
    """Raised when signing is required but no usable key is present.

    Notary fails closed on this. An unsigned certificate rendered by a UI that
    advertises Trust Mode 2 is worse than no certificate: it teaches the viewer
    to trust a badge that means nothing.
    """


@dataclass(frozen=True)
class SigningIdentity:
    key_id: str
    private_key: Ed25519PrivateKey
    public_key: Ed25519PublicKey

    @property
    def public_key_b64(self) -> str:
        raw = self.public_key.public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        return base64.b64encode(raw).decode("ascii")

    def public_key_pem(self) -> str:
        return self.public_key.public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        ).decode("ascii")


# --------------------------------------------------------------------------
# Key management
# --------------------------------------------------------------------------


def generate_key(path: Path, *, overwrite: bool = False) -> SigningIdentity:
    """Create a new Ed25519 keypair and persist the private key as PKCS#8 PEM.

    Unencrypted on disk, deliberately and visibly: this is a development key.
    Production custody belongs in a KMS or HSM where the private key never
    leaves the boundary -- see docs/OPERATIONS.md#key-custody.
    """
    if path.exists() and not overwrite:
        raise FileExistsError(
            f"{path} already exists. Refusing to overwrite a signing key. "
            "Certificates signed with the old key would fail verification."
        )

    private = Ed25519PrivateKey.generate()
    pem = private.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(pem)
    try:
        path.chmod(0o600)
    except (OSError, NotImplementedError):  # pragma: no cover - Windows/CI
        log.debug("could not chmod signing key at %s", path)

    log.info("generated Ed25519 signing key at %s", path)
    return SigningIdentity(
        key_id=path.stem, private_key=private, public_key=private.public_key()
    )


def load_identity(path: Path, key_id: str) -> SigningIdentity:
    if not path.exists():
        raise SigningUnavailable(
            f"signing key not found at {path}. Run "
            "`python -m notary.scripts.generate_key` to create one, or set "
            "NOTARY_REQUIRE_SIGNING=false to issue Mode 1 certificates."
        )

    try:
        private = serialization.load_pem_private_key(path.read_bytes(), password=None)
    except Exception as exc:  # noqa: BLE001
        raise SigningUnavailable(f"could not load signing key {path}: {exc}") from exc

    if not isinstance(private, Ed25519PrivateKey):
        raise SigningUnavailable(
            f"{path} is not an Ed25519 key (got {type(private).__name__}). "
            "Trust Mode 2 specifies Ed25519 as the default algorithm."
        )

    return SigningIdentity(
        key_id=key_id, private_key=private, public_key=private.public_key()
    )


def load_or_create(path: Path, key_id: str) -> SigningIdentity:
    try:
        return load_identity(path, key_id)
    except SigningUnavailable:
        log.warning("no signing key at %s; generating a development key", path)
        identity = generate_key(path)
        return SigningIdentity(
            key_id=key_id,
            private_key=identity.private_key,
            public_key=identity.public_key,
        )


# --------------------------------------------------------------------------
# Sign / verify
# --------------------------------------------------------------------------


def _hash_bytes(canonical_hash: str) -> bytes:
    """Bytes actually signed.

    The hex digest is signed as ASCII rather than decoded to raw bytes. Both
    are defensible; encoding it explicitly here means a third-party verifier
    written in another language has an unambiguous spec to follow, and the
    choice is documented rather than implied.
    """
    return canonical_hash.strip().lower().encode("ascii")


def sign_manifest_hash(canonical_hash: str, identity: SigningIdentity) -> SignatureBlock:
    """Produce the Trust Mode 2 signature block for a manifest hash."""
    if not canonical_hash:
        raise ValueError("cannot sign an empty canonical hash")

    signature = identity.private_key.sign(_hash_bytes(canonical_hash))
    return SignatureBlock(
        algorithm="Ed25519",
        key_id=identity.key_id,
        public_key=identity.public_key_b64,
        signature=base64.b64encode(signature).decode("ascii"),
        signed_at=datetime.now(UTC),
        canonical_hash=canonical_hash.strip().lower(),
    )


def verify_signature(block: SignatureBlock, canonical_hash: str | None = None) -> bool:
    """Verify a signature block, optionally against a freshly computed hash.

    Passing `canonical_hash` is what makes this meaningful. Verifying the block
    against the hash stored *inside the block* only proves the block is
    self-consistent -- exactly the Mode 1 weakness this module exists to close.
    The caller should recompute the manifest hash and pass it in.
    """
    target = (canonical_hash or block.canonical_hash).strip().lower()

    if canonical_hash is not None and target != block.canonical_hash.strip().lower():
        log.warning(
            "signature block hash mismatch: block claims %s, recomputed %s",
            block.canonical_hash,
            target,
        )
        return False

    try:
        public = Ed25519PublicKey.from_public_bytes(base64.b64decode(block.public_key))
        public.verify(base64.b64decode(block.signature), _hash_bytes(target))
    except (InvalidSignature, ValueError, TypeError) as exc:
        log.warning("signature verification failed: %s", exc)
        return False
    return True


def encode_signature_block(block: SignatureBlock) -> str:
    """Serialise a signature block to the string the manifest field holds.

    `Manifest.signature` is typed `str | None` (verified against
    genblaze-core 0.3.8), so the block is stored as canonical JSON rather than
    a nested object. Assigning a dict to it fails pydantic validation -- an
    earlier version of this function did exactly that, which would have made
    every live certification raise at the moment of signing.

    Canonical form (sorted keys, no incidental whitespace) so the string is
    byte-reproducible.
    """
    return json.dumps(
        block.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
    )


def apply_to_manifest(manifest: object, block: SignatureBlock) -> bool:
    """Write the signature into the manifest's reserved `signature` field.

    The SDK's own source marks this field as intentionally excluded from the
    canonical hash: "Cryptographic signature (reserved). Not included in hash."
    That exclusion is what makes Trust Mode 2 implementable at all -- the
    signature can be written into the manifest without invalidating the hash it
    commits to.
    """
    try:
        setattr(manifest, "signature", encode_signature_block(block))
        return True
    except (AttributeError, TypeError, ValueError) as exc:
        log.info(
            "manifest signature field not writable (%s); the signature is "
            "carried on the certificate instead",
            exc,
        )
        return False


def signature_from_manifest(manifest: object) -> SignatureBlock | None:
    raw = getattr(manifest, "signature", None)
    if not raw:
        return None

    # The field is a JSON string; tolerate a dict for forward compatibility.
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            log.warning("manifest signature field is not valid JSON")
            return None

    try:
        return SignatureBlock.model_validate(raw)
    except Exception:  # noqa: BLE001
        log.warning("manifest carries an unparseable signature block")
        return None
