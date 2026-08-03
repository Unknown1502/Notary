"""Assembling the sealed record.

Certification is the moment a take stops being a draft and becomes a claim.
Four things are written under one Object Lock retention window, and the order
matters:

    1. asset.mp4        the media, with the Genblaze manifest embedded
    2. manifest.json    the provenance document, canonical-hashed
    3. verdict.json     the Board's complete finding, including any human
                        sign-off
    4. certificate.json the signed envelope binding all three together

The verdict is written into the vault under the *same* retention as the asset,
which is the answer to the obvious challenge: "is the approval part of the
immutable record, or just the file?" It is part of it. It cannot be revised
after the fact any more than the video can.

Note that the verdict reaches the record twice, by different routes, and this
is intentional redundancy rather than duplication:

    via the manifest   -- moderation findings in step metadata, and the vision
                          verdict as a content-addressed asset from the
                          BoardReviewProvider step. Bound by the canonical hash.
    via verdict.json   -- the full human-readable finding with measurements and
                          rationale, under Object Lock.

The first is cryptographically bound but terse. The second is complete but
relies on storage immutability. Together they cover each other's weakness.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from ..models import (
    BoardVerdict,
    CampaignBrief,
    Certificate,
    ReviewSession,
    Take,
)
from .signing import SigningIdentity, SigningUnavailable, sign_manifest_hash

log = logging.getLogger(__name__)


def canonical_json(payload: Any) -> str:
    """Deterministic JSON. Same input, same bytes, on any machine.

    Sorted keys and no incidental whitespace, matching the discipline the
    Genblaze manifest itself uses. Without this a hash over a document is not
    reproducible and therefore not evidence.
    """
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def hash_payload(payload: Any) -> str:
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def build_verdict_document(
    verdict: BoardVerdict, brief: CampaignBrief, take: Take
) -> dict[str, Any]:
    """The verdict.json written into the vault.

    Self-describing on purpose. Someone opening this file years later, without
    Notary, should be able to reconstruct what was checked, how, against what
    thresholds, and who decided -- without reading our source.
    """
    return {
        "schema": "notary.verdict/v1",
        "verdict_id": verdict.verdict_id,
        "run_id": verdict.run_id,
        "take_number": verdict.take_number,
        "decision": verdict.decision.value,
        "summary": verdict.summary,
        "reviewed_at": verdict.reviewed_at.isoformat(),
        "compliance_profile": brief.compliance_profile,
        "campaign": {
            "campaign_id": brief.campaign_id,
            "title": brief.title,
            "tenant": brief.tenant,
            "submitted_by": brief.submitted_by,
        },
        "brand_guardrails": {
            "palette": brief.brand_kit.palette,
            "palette_tolerance_delta_e": brief.brand_kit.palette_tolerance,
            "palette_min_coverage": brief.brand_kit.palette_min_coverage,
            "banned_terms": brief.brand_kit.banned_terms,
            "mandatory_disclosures": brief.brand_kit.mandatory_disclosures,
        },
        "channel": {
            "aspect_ratio": brief.channel.aspect_ratio,
            "duration_seconds": brief.channel.duration_seconds,
            "placement": brief.channel.placement,
        },
        "criteria": [
            {
                "id": c.criterion.value,
                "outcome": c.outcome.value,
                "check_kind": c.kind.value,
                "severity": c.severity.value,
                "rationale": c.rationale,
                "confidence": c.confidence,
                "measurement": c.measurement,
                "evidence_frame": c.evidence_frame,
            }
            for c in verdict.criteria
        ],
        "review_machinery": {
            "deterministic_engine": "notary.board.deterministic",
            "deterministic_note": (
                "Measured from the asset. Reproducible by any party holding "
                "the file; requires no trust in Notary."
            ),
            "vision_model": verdict.vision_model,
            "vision_provider": verdict.vision_provider,
            "vision_note": (
                "Perceptual judgement. May be wrong; uncertainty is escalated "
                "to a human rather than resolved automatically."
            ),
            "frames_reviewed": verdict.frames_reviewed,
            "tokens_in": verdict.tokens_in,
            "tokens_out": verdict.tokens_out,
            "estimated_cost_usd": verdict.estimated_cost_usd,
        },
        "human_signoff": (
            {
                "reviewer": verdict.human_review.reviewer,
                "decision": verdict.human_review.decision,
                "note": verdict.human_review.note,
                "signed_at": verdict.human_review.signed_at.isoformat(),
            }
            if verdict.human_review
            else None
        ),
        "generation": {
            "image_provider": take.image_provider,
            "image_model": take.image_model,
            "video_provider": take.video_provider,
            "video_model": take.video_model,
            "used_fallback": take.used_fallback,
            "fallback_reason": take.fallback_reason,
            "parent_run_id": take.parent_run_id,
        },
    }


def build_certificate(
    *,
    session: ReviewSession,
    take: Take,
    verdict: BoardVerdict,
    asset_key: str,
    asset_url: str,
    manifest_key: str,
    verdict_key: str,
    manifest_hash: str,
    thumbnail_url: str | None,
    retention_days: int,
    identity: SigningIdentity | None,
    require_signing: bool,
    object_lock_mode: str = "COMPLIANCE",
    parameters: dict[str, Any] | None = None,
    asset_version_id: str | None = None,
) -> Certificate:
    """Assemble and sign the certificate.

    Signing is the last step and, when `require_signing` is set, a hard gate:
    if the key is unavailable the certificate is not issued at all. Emitting an
    unsigned certificate from a system that advertises Mode 2 would train users
    to trust a badge that sometimes means nothing.
    """
    if not take.sha256:
        raise ValueError("cannot certify a take with no computed asset digest")

    signature = None
    if identity is not None:
        signature = sign_manifest_hash(manifest_hash, identity)
        log.info(
            "certificate signed (Trust Mode 2) with key '%s' over manifest hash %s",
            identity.key_id,
            manifest_hash[:16],
        )
    elif require_signing:
        raise SigningUnavailable(
            "NOTARY_REQUIRE_SIGNING is set but no signing identity was "
            "available. Refusing to issue a Mode 1 certificate from a Mode 2 "
            "deployment."
        )
    else:
        log.warning("issuing a Trust Mode 1 certificate: signing is disabled")

    return Certificate(
        asset_id=take.take_id,
        campaign_id=session.brief.campaign_id,
        tenant=session.brief.tenant,
        run_id=take.run_id,
        asset_key=asset_key,
        asset_url=asset_url,
        asset_version_id=asset_version_id,
        manifest_key=manifest_key,
        verdict_key=verdict_key,
        thumbnail_url=thumbnail_url,
        sha256=take.sha256,
        manifest_hash=manifest_hash,
        signature=signature,
        provider=take.video_provider or take.image_provider or "unknown",
        model=take.video_model or take.image_model or "unknown",
        prompt=take.prompt,
        parameters=parameters or {
            "aspect_ratio": session.brief.channel.aspect_ratio,
            "duration_seconds": session.brief.channel.duration_seconds,
            "used_fallback": take.used_fallback,
        },
        verdict=verdict,
        lineage=session.lineage(),
        retention_until=datetime.now(UTC) + timedelta(days=retention_days),
        object_lock_mode=object_lock_mode,  # type: ignore[arg-type]
    )


def certificate_from_document(payload: dict[str, Any]) -> Certificate:
    """Parse a certificate.json back into a Certificate.

    `certificate_document()` adds derived, presentation-only keys -- `schema`,
    `trust_mode`, `trust_mode_label`, `verification_instructions` -- so that the
    sealed file is self-describing to a reader who has never seen this codebase.
    The model is `extra="forbid"`, so feeding the document straight back to
    `Certificate.model_validate()` raises.

    That asymmetry silently broke the round trip: certificates were written to
    B2 and could never be read out, so the library rebuilt itself as empty after
    every restart and the "B2 is the system of record" claim was false.

    Derived keys are dropped rather than tolerated, because they are outputs of
    the model and must never be able to contradict it -- a document claiming
    `trust_mode: 2` with no signature should lose that argument.
    """
    known = set(Certificate.model_fields)
    return Certificate.model_validate({k: v for k, v in payload.items() if k in known})


def certificate_document(certificate: Certificate) -> dict[str, Any]:
    """The certificate.json written to the vault."""
    doc = certificate.model_dump(mode="json")
    doc["schema"] = "notary.certificate/v1"
    doc["trust_mode"] = certificate.trust_mode
    doc["trust_mode_label"] = (
        "Mode 2 — authenticated integrity (Ed25519)"
        if certificate.trust_mode == 2
        else "Mode 1 — integrity only"
    )
    doc["verification_instructions"] = {
        "asset_integrity": (
            "Download asset_url and compute SHA-256. It must equal `sha256`."
        ),
        "signature": (
            "Base64-decode `signature.public_key` to a raw 32-byte Ed25519 key. "
            "Base64-decode `signature.signature`. Verify it over the ASCII "
            "bytes of the lowercase hex string in `manifest_hash`."
        ),
        "independence_note": (
            "Both checks are performable with standard tooling and without "
            "Notary. That is the point of publishing them here."
        ),
    }
    return doc
