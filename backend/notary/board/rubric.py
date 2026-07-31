"""Compliance profiles: the rules the Board screens against.

These are not invented for a demo. Each profile encodes constraints that a real
review function in that vertical actually applies, and each criterion carries
the `rationale` that a reviewer would give if you asked them why the rule
exists. That rationale is rendered in the UI next to the finding.

Scope, stated honestly: this is a *screening* rubric, not a substitute for
statutory review. Notary's pharma profile catches the mechanical failures that
waste an MLR reviewer's time (missing ISI, absent fair-balance signal,
superiority language, off-spec deliverable). It does not adjudicate whether a
claim is substantiated -- that is a human judgement and it routes to a human.

References that shaped the pharma profile:
  21 CFR 202.1  -- prescription drug advertising, fair balance and brief summary
  FDA OPDP guidance on consumer-directed broadcast ads (adequate provision)
  FTC Act s.5    -- deceptive advertising, applied to the financial profile
  FINRA Rule 2210 -- communications with the public, retail comms review
"""

from __future__ import annotations

from ..models import CheckKind, Criterion, CriterionId, Severity

# --------------------------------------------------------------------------
# Shared channel/brand criteria -- every profile screens these
# --------------------------------------------------------------------------

_CHANNEL_CRITERIA: list[Criterion] = [
    Criterion(
        id=CriterionId.ASPECT_RATIO,
        label="Aspect ratio",
        description="Rendered frame geometry matches the channel spec.",
        kind=CheckKind.DETERMINISTIC,
        severity=Severity.BLOCKING,
        rationale=(
            "An off-spec ratio is rejected by the ad platform on upload, so it "
            "fails before a human ever sees it. Cheapest possible catch."
        ),
    ),
    Criterion(
        id=CriterionId.DURATION,
        label="Duration",
        description="Clip length is within tolerance of the requested duration.",
        kind=CheckKind.DETERMINISTIC,
        severity=Severity.BLOCKING,
        rationale=(
            "Broadcast and paid-social placements are sold in fixed slots. A "
            "6.4s asset cannot run in a 6s slot."
        ),
    ),
    Criterion(
        id=CriterionId.PALETTE_ADHERENCE,
        label="Brand palette",
        description=(
            "Sampled frames sit within the brand palette's color tolerance."
        ),
        kind=CheckKind.DETERMINISTIC,
        severity=Severity.BLOCKING,
        rationale=(
            "Generative models drift off-palette constantly and it is the most "
            "common reason brand teams reject an otherwise usable take."
        ),
    ),
    Criterion(
        id=CriterionId.BANNED_LEXEMES,
        label="Prohibited terms",
        description="No prohibited term appears in the prompt or on-screen text.",
        kind=CheckKind.DETERMINISTIC,
        severity=Severity.BLOCKING,
        rationale=(
            "Legal maintains an explicit prohibited-term list per market. These "
            "are exact-match rules, so an exact-match check is the correct tool "
            "-- asking a model to spot them would be strictly worse."
        ),
    ),
    Criterion(
        id=CriterionId.LOGO_PRESENCE,
        label="Logo present",
        description="The brand logo appears in at least one reviewed frame.",
        kind=CheckKind.PERCEPTUAL,
        severity=Severity.BLOCKING,
        rationale="An unbranded asset cannot run. Requires visual judgement.",
    ),
    Criterion(
        id=CriterionId.LOGO_LEGIBILITY,
        label="Logo legibility",
        description="Where present, the logo is unobstructed and legible.",
        kind=CheckKind.PERCEPTUAL,
        severity=Severity.ADVISORY,
        rationale=(
            "Advisory: legibility is genuinely subjective at the margin, so a "
            "finding here informs a reviewer rather than blocking on its own."
        ),
    ),
    Criterion(
        id=CriterionId.VISUAL_ARTIFACTS,
        label="Generation artifacts",
        description=(
            "No malformed hands, warped text, morphing faces, or temporal "
            "flicker of the kind diffusion models produce."
        ),
        kind=CheckKind.PERCEPTUAL,
        severity=Severity.BLOCKING,
        rationale=(
            "The single most reliable way an AI-generated asset embarrasses a "
            "brand in public. No deterministic check exists for it."
        ),
    ),
    Criterion(
        id=CriterionId.TONE_ALIGNMENT,
        label="Tone",
        description="Register and mood match the brand's tone guidance.",
        kind=CheckKind.PERCEPTUAL,
        severity=Severity.ADVISORY,
        rationale=(
            "Advisory by design. Tone is the criterion Notary is least "
            "qualified to assert, so it surfaces an opinion and defers."
        ),
    ),
]


# --------------------------------------------------------------------------
# Profiles
# --------------------------------------------------------------------------

PHARMA_DTC_US: list[Criterion] = [
    *_CHANNEL_CRITERIA,
    Criterion(
        id=CriterionId.MANDATORY_DISCLOSURE,
        label="Important Safety Information",
        description=(
            "Required safety disclosure text is present and on screen, per the "
            "fair-balance obligation for prescription drug promotion."
        ),
        kind=CheckKind.DETERMINISTIC,
        severity=Severity.BLOCKING,
        rationale=(
            "21 CFR 202.1 requires a fair balance between efficacy and risk "
            "information. A DTC spot that presents benefit without the "
            "accompanying safety disclosure is misbranded. This is the single "
            "highest-consequence mechanical failure in pharma creative, and it "
            "is exactly checkable, so it is checked exactly."
        ),
    ),
    Criterion(
        id=CriterionId.PROHIBITED_IMAGERY,
        label="Prohibited depiction",
        description=(
            "No depiction implying guaranteed outcome, cure, or use by a "
            "population outside the approved indication."
        ),
        kind=CheckKind.PERCEPTUAL,
        severity=Severity.BLOCKING,
        rationale=(
            "Imagery can imply an unapproved claim without a word being said -- "
            "e.g. depicting a visibly pediatric patient for an adult-only "
            "indication. Perceptual, blocking, and escalated when uncertain."
        ),
    ),
]

FINANCIAL_SERVICES_US: list[Criterion] = [
    *_CHANNEL_CRITERIA,
    Criterion(
        id=CriterionId.MANDATORY_DISCLOSURE,
        label="Risk disclosure",
        description="Required risk/performance disclaimer is present.",
        kind=CheckKind.DETERMINISTIC,
        severity=Severity.BLOCKING,
        rationale=(
            "FINRA Rule 2210 requires retail communications to be fair and "
            "balanced, and past-performance claims to carry the standard "
            "disclaimer. Omission is a supervisable violation."
        ),
    ),
    Criterion(
        id=CriterionId.PROHIBITED_IMAGERY,
        label="Implied guarantee",
        description="No imagery implying guaranteed or risk-free return.",
        kind=CheckKind.PERCEPTUAL,
        severity=Severity.BLOCKING,
        rationale=(
            "Visual shorthand for guaranteed returns -- an always-up chart, "
            "cash imagery over a performance claim -- is a classic 2210 finding."
        ),
    ),
]

GENERAL_BRAND: list[Criterion] = list(_CHANNEL_CRITERIA)


PROFILES: dict[str, list[Criterion]] = {
    "pharma-dtc-us": PHARMA_DTC_US,
    "financial-services-us": FINANCIAL_SERVICES_US,
    "general-brand": GENERAL_BRAND,
}

PROFILE_LABELS: dict[str, str] = {
    "pharma-dtc-us": "Pharma — US direct-to-consumer",
    "financial-services-us": "Financial services — US retail",
    "general-brand": "General brand guidelines",
}


def get_profile(name: str) -> list[Criterion]:
    try:
        return PROFILES[name]
    except KeyError:
        raise ValueError(
            f"unknown compliance profile {name!r}; "
            f"available: {', '.join(sorted(PROFILES))}"
        ) from None


def criteria_by_kind(name: str, kind: CheckKind) -> list[Criterion]:
    return [c for c in get_profile(name) if c.kind is kind]


def describe_profiles() -> list[dict[str, object]]:
    """Profile catalogue for the brief-intake screen."""
    out: list[dict[str, object]] = []
    for key, criteria in PROFILES.items():
        out.append(
            {
                "id": key,
                "label": PROFILE_LABELS[key],
                "criteria": [
                    {
                        "id": c.id.value,
                        "label": c.label,
                        "kind": c.kind.value,
                        "severity": c.severity.value,
                        "description": c.description,
                        "rationale": c.rationale,
                    }
                    for c in criteria
                ],
                "deterministic_count": sum(
                    1 for c in criteria if c.kind is CheckKind.DETERMINISTIC
                ),
                "perceptual_count": sum(
                    1 for c in criteria if c.kind is CheckKind.PERCEPTUAL
                ),
            }
        )
    return out
