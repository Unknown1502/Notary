"""Domain vocabulary for Notary.

One idea drives the whole schema: **a criterion knows how it is checked.**

Every rubric criterion declares itself either DETERMINISTIC (computed from the
bytes -- aspect ratio, palette distance, duration, banned lexemes) or
PERCEPTUAL (requires judgement -- is the logo legible, is the tone right, are
there artifacts). They are enforced by different machinery, they carry
different confidence, and they fail differently:

    DETERMINISTIC  ->  genblaze ModerationHook, no model in the loop.
                       A FAIL here is a fact. It is reproducible from the
                       asset alone and needs no trust in Notary.

    PERCEPTUAL     ->  a vision model, wrapped as a real pipeline step so its
                       verdict lands in the manifest.
                       A FAIL here is an opinion. It is allowed to be wrong,
                       which is exactly why UNCERTAIN routes to a human
                       instead of shipping.

Keeping these apart is what lets Notary claim precision on the objective half
without overclaiming on the subjective half. See docs/TRUST-MODEL.md.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

HEX_COLOR = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")


def _now() -> datetime:
    return datetime.now(UTC)


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:12]}"


class Base(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


# --------------------------------------------------------------------------
# Rubric
# --------------------------------------------------------------------------


class CheckKind(StrEnum):
    DETERMINISTIC = "deterministic"
    PERCEPTUAL = "perceptual"


class Severity(StrEnum):
    BLOCKING = "blocking"
    """A FAIL cannot be waived by the Board. It rejects, or it escalates."""

    ADVISORY = "advisory"
    """A FAIL is recorded and surfaced, but does not by itself reject."""


class CriterionId(StrEnum):
    ASPECT_RATIO = "aspect_ratio"
    DURATION = "duration"
    PALETTE_ADHERENCE = "palette_adherence"
    BANNED_LEXEMES = "banned_lexemes"
    MANDATORY_DISCLOSURE = "mandatory_disclosure"
    LOGO_PRESENCE = "logo_presence"
    LOGO_LEGIBILITY = "logo_legibility"
    VISUAL_ARTIFACTS = "visual_artifacts"
    TONE_ALIGNMENT = "tone_alignment"
    PROHIBITED_IMAGERY = "prohibited_imagery"


class Criterion(Base):
    id: CriterionId
    label: str
    description: str
    kind: CheckKind
    severity: Severity = Severity.BLOCKING
    rationale: str = ""
    """Why a regulator or brand team cares. Rendered in the UI so a reviewer
    sees the reason for a rule, not just the rule."""


# --------------------------------------------------------------------------
# Brief
# --------------------------------------------------------------------------


class BrandKit(Base):
    name: str
    palette: list[str] = Field(default_factory=list, description="Hex colors.")
    palette_tolerance: float = Field(
        default=18.0, ge=0.0, le=100.0,
        description="Max CIE76 deltaE from the nearest brand color for a pixel "
                    "to count as on-palette.",
    )
    palette_min_coverage: float = Field(
        default=0.55, ge=0.0, le=1.0,
        description="Fraction of sampled non-neutral pixels that must be "
                    "on-palette for PALETTE_ADHERENCE to pass.",
    )
    logo_uri: str | None = None
    banned_terms: list[str] = Field(default_factory=list)
    mandatory_disclosures: list[str] = Field(default_factory=list)
    tone_guidance: str = ""

    @field_validator("palette")
    @classmethod
    def _validate_palette(cls, v: list[str]) -> list[str]:
        for color in v:
            if not HEX_COLOR.match(color):
                raise ValueError(f"invalid hex color: {color!r}")
        return [c.lower() for c in v]


class ChannelSpec(Base):
    aspect_ratio: str = "16:9"
    duration_seconds: int = Field(default=5, ge=1, le=60)
    placement: str = "social-preroll"

    @field_validator("aspect_ratio")
    @classmethod
    def _validate_ar(cls, v: str) -> str:
        if not re.match(r"^\d{1,2}:\d{1,2}$", v):
            raise ValueError("aspect_ratio must look like '16:9'")
        return v

    @property
    def ratio(self) -> float:
        w, h = self.aspect_ratio.split(":")
        return int(w) / int(h)


class CampaignBrief(Base):
    campaign_id: str = Field(default_factory=lambda: _new_id("cmp"))
    tenant: str = "acme-pharma"
    title: str
    prompt: str
    brand_kit: BrandKit
    channel: ChannelSpec = Field(default_factory=ChannelSpec)
    compliance_profile: str = "pharma-dtc-us"
    submitted_by: str = "unknown@example.com"
    submitted_at: datetime = Field(default_factory=_now)


# --------------------------------------------------------------------------
# Verdicts
# --------------------------------------------------------------------------


class CriterionOutcome(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    UNCERTAIN = "uncertain"
    NOT_APPLICABLE = "not_applicable"


class BoardDecision(StrEnum):
    VERIFIED = "verified"
    """Every blocking criterion passed. Eligible for certification."""

    REJECTED = "rejected"
    """At least one blocking criterion failed with confidence. Triggers a
    verdict-conditioned revision if iterations remain."""

    ESCALATED = "escalated"
    """Ambiguity, a malformed model response, an exhausted revision budget, or
    a perceptual FAIL the Board is not confident enough to assert. Goes to a
    human. NEVER ships on its own."""


class CriterionVerdict(Base):
    criterion: CriterionId
    outcome: CriterionOutcome
    kind: CheckKind
    severity: Severity
    rationale: str = ""
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    """None for deterministic checks -- a measurement does not have a
    confidence, it has a value. Populated for perceptual checks only."""

    measurement: dict[str, float | str | bool] | None = None
    """The computed evidence for a deterministic check, e.g.
    {"observed_ratio": 1.0, "expected_ratio": 1.777, "tolerance": 0.02}.
    This is what makes a deterministic FAIL independently reproducible."""

    evidence_frame: str | None = None
    """Storage key of the specific frame this criterion flagged."""

    @property
    def blocks_certification(self) -> bool:
        return (
            self.severity is Severity.BLOCKING
            and self.outcome in (CriterionOutcome.FAIL, CriterionOutcome.UNCERTAIN)
        )


class BoardVerdict(Base):
    """The Board's complete finding on one take.

    Serialized into `verdict.json` in the vault AND surfaced through the
    manifest via step metadata. See docs/ARCHITECTURE.md#sealing-the-verdict.
    """

    verdict_id: str = Field(default_factory=lambda: _new_id("vd"))
    run_id: str
    take_number: int = 1
    decision: BoardDecision
    criteria: list[CriterionVerdict]
    summary: str = ""

    reviewed_at: datetime = Field(default_factory=_now)
    vision_model: str | None = None
    vision_provider: str | None = None
    frames_reviewed: list[str] = Field(default_factory=list)

    tokens_in: int | None = None
    tokens_out: int | None = None
    estimated_cost_usd: float | None = None
    """chat() always returns cost_usd=None. Notary computes this from the
    token counts and the ModelRegistry price. Verified in SPIKES.md #3."""

    human_review: HumanSignoff | None = None

    @property
    def failures(self) -> list[CriterionVerdict]:
        return [c for c in self.criteria if c.outcome is CriterionOutcome.FAIL]

    @property
    def blocking_failures(self) -> list[CriterionVerdict]:
        return [c for c in self.criteria if c.blocks_certification]

    @property
    def deterministic_failures(self) -> list[CriterionVerdict]:
        return [c for c in self.failures if c.kind is CheckKind.DETERMINISTIC]

    def revision_guidance(self) -> str:
        """Turn this verdict into prompt language for the next attempt.

        This is the mechanism that makes the before/after *causal* rather than
        lucky: the next take is conditioned on the specific reasons this one
        failed, not on a reroll of the same prompt.
        """
        lines: list[str] = []
        for c in self.blocking_failures:
            if c.rationale:
                lines.append(f"- {c.criterion.value}: {c.rationale}")
        if not lines:
            return ""
        return (
            "The previous take was rejected by compliance review for the "
            "following specific reasons. Correct every one of them while "
            "keeping the original creative intent:\n" + "\n".join(lines)
        )


class HumanSignoff(Base):
    reviewer: str
    decision: Literal["approved", "rejected"]
    note: str = ""
    signed_at: datetime = Field(default_factory=_now)


# --------------------------------------------------------------------------
# Takes, runs, lineage
# --------------------------------------------------------------------------


class TakeStatus(StrEnum):
    GENERATING = "generating"
    REVIEWING = "reviewing"
    REJECTED = "rejected"
    ESCALATED = "escalated"
    CERTIFIED = "certified"
    FAILED = "failed"


class Take(Base):
    """One generated attempt and everything known about it."""

    take_id: str = Field(default_factory=lambda: _new_id("take"))
    run_id: str
    parent_run_id: str | None = None
    take_number: int = 1
    status: TakeStatus = TakeStatus.GENERATING

    prompt: str
    revision_guidance: str | None = None

    image_provider: str | None = None
    image_model: str | None = None
    video_provider: str | None = None
    video_model: str | None = None
    used_fallback: bool = False
    fallback_reason: str | None = None

    asset_key: str | None = None
    asset_url: str | None = None
    thumbnail_url: str | None = None
    frame_keys: list[str] = Field(default_factory=list)
    sha256: str | None = None

    verdict: BoardVerdict | None = None
    created_at: datetime = Field(default_factory=_now)
    duration_seconds: float | None = None
    cost_usd: float | None = None


class ReviewSession(Base):
    """A campaign brief driven through the Board to a terminal state.

    Maps 1:1 onto one genblaze AgentLoop invocation.
    """

    session_id: str = Field(default_factory=lambda: _new_id("sess"))
    brief: CampaignBrief
    takes: list[Take] = Field(default_factory=list)
    status: TakeStatus = TakeStatus.GENERATING
    certificate_id: str | None = None
    started_at: datetime = Field(default_factory=_now)
    finished_at: datetime | None = None
    total_cost_usd: float = 0.0

    @property
    def current_take(self) -> Take | None:
        return self.takes[-1] if self.takes else None

    def lineage(self) -> list[dict[str, object]]:
        return [
            {
                "run_id": t.run_id,
                "parent_run_id": t.parent_run_id,
                "take_number": t.take_number,
                "status": t.status.value,
                "decision": t.verdict.decision.value if t.verdict else None,
                "used_fallback": t.used_fallback,
                "created_at": t.created_at.isoformat(),
            }
            for t in self.takes
        ]


# --------------------------------------------------------------------------
# Certificate
# --------------------------------------------------------------------------


class SignatureBlock(Base):
    """Genblaze Trust Mode 2. See docs/TRUST-MODEL.md.

    The SDK reserves `signature` on Manifest and excludes it from the canonical
    hash for exactly this purpose. Notary fills it in.
    """

    algorithm: Literal["Ed25519"] = "Ed25519"
    key_id: str
    public_key: str = Field(description="Base64 raw 32-byte Ed25519 public key.")
    signature: str = Field(description="Base64 signature over the canonical hash.")
    signed_at: datetime
    canonical_hash: str = Field(description="The manifest hash that was signed.")


class Certificate(Base):
    certificate_id: str = Field(default_factory=lambda: _new_id("cert"))
    asset_id: str
    campaign_id: str
    tenant: str
    run_id: str

    asset_key: str
    asset_url: str
    manifest_key: str
    verdict_key: str
    thumbnail_url: str | None = None

    sha256: str
    manifest_hash: str
    signature: SignatureBlock | None = None

    provider: str
    model: str
    prompt: str
    parameters: dict[str, object] = Field(default_factory=dict)

    verdict: BoardVerdict
    lineage: list[dict[str, object]] = Field(default_factory=list)

    certified_at: datetime = Field(default_factory=_now)
    retention_until: datetime
    object_lock_mode: Literal["COMPLIANCE", "GOVERNANCE", "NONE"] = "COMPLIANCE"

    @property
    def trust_mode(self) -> int:
        return 2 if self.signature else 1

    @property
    def is_sealed(self) -> bool:
        return self.object_lock_mode != "NONE" and self.retention_until > _now()


class VerificationCheck(Base):
    name: str
    passed: bool
    detail: str
    expected: str | None = None
    observed: str | None = None


class VerificationReport(Base):
    """Result of re-verifying a certificate against live bytes in B2.

    Deliberately a list of independent checks rather than one boolean, because
    the interesting failure is partial: bytes intact but signature invalid means
    something very different from signature valid but bytes changed.
    """

    certificate_id: str
    verified_at: datetime = Field(default_factory=_now)
    checks: list[VerificationCheck]
    bytes_hashed: int = 0
    source: str = Field(description="Where the bytes came from, e.g. the B2 URL.")

    @property
    def passed(self) -> bool:
        return all(c.passed for c in self.checks)


CriterionVerdictList = Annotated[list[CriterionVerdict], Field(min_length=1)]

BoardVerdict.model_rebuild()
