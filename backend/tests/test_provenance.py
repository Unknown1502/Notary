"""Tests for Trust Mode 2 signing and the storage key strategies."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from notary.provenance import signing
from notary.storage import keys


@pytest.fixture
def identity(tmp_path):
    return signing.generate_key(tmp_path / "test-ed25519.pem")


def test_signature_verifies_against_the_signed_hash(identity):
    digest = "a" * 64
    block = signing.sign_manifest_hash(digest, identity)
    assert block.algorithm == "Ed25519"
    assert signing.verify_signature(block, digest) is True


def test_signature_fails_against_a_different_hash(identity):
    """The core tamper case: bytes changed, so the manifest hash changed."""
    block = signing.sign_manifest_hash("a" * 64, identity)
    assert signing.verify_signature(block, "b" * 64) is False


def test_tampered_signature_is_rejected(identity):
    block = signing.sign_manifest_hash("a" * 64, identity)
    mutated = list(block.signature)
    mutated[5] = "A" if mutated[5] != "A" else "B"
    block.signature = "".join(mutated)
    assert signing.verify_signature(block, "a" * 64) is False


def test_signature_from_another_key_is_rejected(identity, tmp_path):
    """Forgery: a valid signature, but not from the key we published."""
    digest = "c" * 64
    attacker = signing.generate_key(tmp_path / "attacker.pem")
    forged = signing.sign_manifest_hash(digest, attacker)
    forged.public_key = identity.public_key_b64  # claim to be us
    assert signing.verify_signature(forged, digest) is False


def test_hash_is_normalised_before_signing(identity):
    """Casing and whitespace must not change the signature's meaning."""
    block = signing.sign_manifest_hash("AbCd" + "0" * 60, identity)
    assert signing.verify_signature(block, "abcd" + "0" * 60) is True
    assert signing.verify_signature(block, "  abcd" + "0" * 60 + "  ") is True


def test_key_round_trips_through_pem(tmp_path):
    original = signing.generate_key(tmp_path / "k.pem")
    reloaded = signing.load_identity(tmp_path / "k.pem", "k")
    assert reloaded.public_key_b64 == original.public_key_b64

    digest = "d" * 64
    block = signing.sign_manifest_hash(digest, original)
    assert signing.verify_signature(block, digest) is True


def test_refuses_to_overwrite_an_existing_key(tmp_path):
    """Overwriting invalidates every certificate already signed."""
    path = tmp_path / "k.pem"
    signing.generate_key(path)
    with pytest.raises(FileExistsError):
        signing.generate_key(path)


def test_missing_key_raises_signing_unavailable(tmp_path):
    with pytest.raises(signing.SigningUnavailable):
        signing.load_identity(tmp_path / "absent.pem", "absent")


def test_empty_hash_cannot_be_signed(identity):
    with pytest.raises(ValueError):
        signing.sign_manifest_hash("", identity)


# --------------------------------------------------------------------------
# Key strategies
# --------------------------------------------------------------------------


def test_vault_keys_group_a_record_under_one_prefix():
    """One prefix must return the complete sealed record. That is the audit query."""
    args = ("acme-pharma", "cmp-q3", "asset-1")
    prefix = keys.vault_prefix(*args)
    for key in (
        keys.vault_asset_key(*args),
        keys.vault_manifest_key(*args),
        keys.vault_verdict_key(*args),
        keys.vault_certificate_key(*args),
    ):
        assert key.startswith(prefix + "/")


def test_slug_neutralises_path_traversal():
    """A tenant name with a slash would forge a directory level."""
    assert "/" not in keys.slug("acme/../evil")
    assert "/" not in keys.slug("a/b/c")
    assert keys.slug("") == "unnamed"


def test_content_addressing_deduplicates_identical_bytes():
    assert keys.content_key(b"same") == keys.content_key(b"same")
    assert keys.content_key(b"same") != keys.content_key(b"different")


def test_content_key_fans_out_two_levels():
    key = keys.content_key(b"payload", ext="mp4")
    parts = key.split("/")
    assert parts[0] == "cache"
    assert len(parts[1]) == 2 and len(parts[2]) == 2
    assert parts[3].startswith(parts[1] + parts[2])


# --------------------------------------------------------------------------
# Certificate document round trip
#
# This guards a bug that made the vault write-only: certificate_document() adds
# derived keys (schema, trust_mode, trust_mode_label, verification_instructions)
# so the sealed file is self-describing, but Certificate is extra="forbid", so
# feeding the document back raised. Certificates were written to B2 and could
# never be read out — the library rebuilt itself empty after every restart and
# the "B2 is the system of record" claim was false.
#
# Only a real round trip through real storage surfaces that, which is why it is
# pinned here.
# --------------------------------------------------------------------------


def _certificate(**overrides):
    from datetime import UTC, datetime, timedelta

    from notary.models import (
        BoardDecision,
        BoardVerdict,
        Certificate,
        CheckKind,
        CriterionId,
        CriterionOutcome,
        CriterionVerdict,
        Severity,
    )

    verdict = BoardVerdict(
        run_id="run-1",
        decision=BoardDecision.VERIFIED,
        criteria=[
            CriterionVerdict(
                criterion=CriterionId.PALETTE_ADHERENCE,
                outcome=CriterionOutcome.PASS,
                kind=CheckKind.DETERMINISTIC,
                severity=Severity.BLOCKING,
                rationale="on palette",
            )
        ],
        summary="cleared",
    )
    defaults = dict(
        asset_id="asset-1", campaign_id="cmp-1", tenant="acme", run_id="run-1",
        asset_key="vault/acme/cmp-1/asset-1/asset.mp4",
        asset_url="https://example.test/asset.mp4",
        manifest_key="m.json", verdict_key="v.json",
        sha256="a" * 64, manifest_hash="b" * 64,
        provider="supplied", model="none", prompt="a coastal path",
        verdict=verdict,
        retention_until=datetime.now(UTC) + timedelta(days=7),
    )
    defaults.update(overrides)
    return Certificate(**defaults)


def test_certificate_survives_a_document_round_trip():
    from notary.provenance import certificate_document, certificate_from_document

    original = _certificate(asset_version_id="v-abc")
    recovered = certificate_from_document(certificate_document(original))

    assert recovered.certificate_id == original.certificate_id
    assert recovered.sha256 == original.sha256
    assert recovered.manifest_hash == original.manifest_hash
    assert recovered.asset_version_id == "v-abc"
    assert recovered.verdict.decision is original.verdict.decision
    assert len(recovered.verdict.criteria) == 1


def test_document_carries_derived_keys_the_model_rejects():
    """The asymmetry itself, pinned. If these ever become model fields the
    stripping in certificate_from_document must be revisited."""
    from notary.models import Certificate
    from notary.provenance import certificate_document

    doc = certificate_document(_certificate())
    derived = set(doc) - set(Certificate.model_fields)

    assert derived == {
        "schema", "trust_mode", "trust_mode_label", "verification_instructions",
    }
    with pytest.raises(ValidationError):
        Certificate.model_validate(doc)


def test_signed_certificate_round_trips_with_its_signature(identity):
    from notary.provenance import certificate_document, certificate_from_document
    from notary.provenance.signing import sign_manifest_hash, verify_signature

    digest = "c" * 64
    original = _certificate(manifest_hash=digest,
                            signature=sign_manifest_hash(digest, identity))
    recovered = certificate_from_document(certificate_document(original))

    assert recovered.signature is not None
    assert recovered.trust_mode == 2
    # The recovered signature must still verify — a round trip that silently
    # corrupted it would produce a certificate that looks signed and is not.
    assert verify_signature(recovered.signature, digest) is True
