"""Tests for the Board.

The tests that matter most here are the ones asserting Notary **cannot**
accidentally approve something. A false REJECTED costs a render. A false
VERIFIED ships a non-compliant asset with a signed certificate attesting that
it was reviewed — which is the one failure this product must not have.

So: every path that could plausibly produce a pass under degraded conditions
(malformed model output, a missing check, low confidence, an exhausted budget)
is asserted to escalate instead.
"""

from __future__ import annotations

import numpy as np
import pytest
from PIL import Image

from notary.board import deterministic as det
from notary.board.review import decide, parse_verdict_json
from notary.models import (
    BoardDecision,
    BrandKit,
    CampaignBrief,
    ChannelSpec,
    CheckKind,
    CriterionId,
    CriterionOutcome,
    CriterionVerdict,
    Severity,
)


@pytest.fixture
def brand() -> BrandKit:
    return BrandKit(
        name="Test",
        palette=["#0b5fff", "#00c2a8"],
        palette_tolerance=18.0,
        palette_min_coverage=0.55,
        banned_terms=["cure", "guaranteed"],
        mandatory_disclosures=["Important Safety Information"],
    )


@pytest.fixture
def brief(brand: BrandKit) -> CampaignBrief:
    return CampaignBrief(
        title="Test campaign",
        prompt="A calm coastal scene.",
        brand_kit=brand,
        channel=ChannelSpec(aspect_ratio="16:9", duration_seconds=6),
        compliance_profile="pharma-dtc-us",
    )


def frame(tmp_path, rgb, size=(320, 180), name="f.png"):
    path = tmp_path / name
    Image.new("RGB", size, rgb).save(path)
    return path


# --------------------------------------------------------------------------
# Colour science
# --------------------------------------------------------------------------


def test_lab_conversion_matches_reference_values():
    """Anchor the conversion against published sRGB->Lab values.

    If this drifts, every palette measurement silently changes meaning and
    previously issued rejections stop being reproducible.
    """
    assert det.rgb_to_lab(np.array([255, 255, 255]))[0] == pytest.approx(100.0, abs=0.01)
    assert det.rgb_to_lab(np.array([0, 0, 0]))[0] == pytest.approx(0.0, abs=0.01)

    red = det.rgb_to_lab(np.array([255, 0, 0]))
    assert red[0] == pytest.approx(53.24, abs=0.05)
    assert red[1] == pytest.approx(80.09, abs=0.05)
    assert red[2] == pytest.approx(67.20, abs=0.05)


def test_delta_e_is_zero_for_identical_colours():
    lab = det.rgb_to_lab(np.array([11, 95, 255]))
    assert float(det.delta_e_76(lab, lab)) == pytest.approx(0.0, abs=1e-9)


def test_palette_measurement_is_deterministic(tmp_path, brand):
    """The same bytes must always yield the same number.

    Stride sampling rather than random sampling is what guarantees this, and a
    rejection that cannot be re-derived is not evidence.
    """
    path = frame(tmp_path, (240, 140, 60), size=(900, 500))
    first = det.measure_palette([path], brand.palette, tolerance=18.0)
    second = det.measure_palette([path], brand.palette, tolerance=18.0)
    assert first.coverage == second.coverage
    assert first.mean_delta_e == second.mean_delta_e


# --------------------------------------------------------------------------
# Deterministic checks
# --------------------------------------------------------------------------


def test_on_palette_frame_passes(tmp_path, brand):
    verdict = det.check_palette([frame(tmp_path, (11, 95, 255))], brand)
    assert verdict.outcome is CriterionOutcome.PASS
    assert verdict.measurement["coverage"] > 0.9


def test_off_palette_frame_fails_with_evidence(tmp_path, brand):
    verdict = det.check_palette([frame(tmp_path, (255, 140, 0))], brand)
    assert verdict.outcome is CriterionOutcome.FAIL
    assert verdict.measurement["mean_delta_e"] > brand.palette_tolerance
    # The rationale must name the actual palette so it can drive a revision.
    assert "#0b5fff" in verdict.rationale


def test_neutral_frame_is_not_applicable_not_a_failure(tmp_path, brand):
    """A greyscale frame has no chromatic content to contradict the palette."""
    verdict = det.check_palette([frame(tmp_path, (128, 128, 128))], brand)
    assert verdict.outcome is CriterionOutcome.NOT_APPLICABLE


def test_aspect_ratio_detects_mismatch(tmp_path):
    square = frame(tmp_path, (11, 95, 255), size=(400, 400))
    assert det.check_aspect_ratio([square], ChannelSpec(aspect_ratio="1:1")).outcome is (
        CriterionOutcome.PASS
    )
    assert det.check_aspect_ratio([square], ChannelSpec(aspect_ratio="16:9")).outcome is (
        CriterionOutcome.FAIL
    )


def test_missing_frames_are_uncertain_never_pass():
    verdict = det.check_aspect_ratio([], ChannelSpec())
    assert verdict.outcome is CriterionOutcome.UNCERTAIN


def test_unprobeable_duration_is_uncertain_never_pass():
    assert det.check_duration(None, ChannelSpec()).outcome is CriterionOutcome.UNCERTAIN


def test_banned_terms_respect_word_boundaries():
    """'secure' must not trip the 'cure' rule.

    A substring match here would make the prohibited-term check unusable in the
    financial profile, where 'secure' appears in nearly every brief.
    """
    assert det.find_banned_terms("a cure for it", ["cure"]) == ["cure"]
    assert det.find_banned_terms("a secure investment", ["cure"]) == []
    assert det.find_banned_terms("CURE", ["cure"]) == ["cure"]


def test_disclosure_matching_tolerates_whitespace_and_case():
    assert det.find_missing_disclosures("Ask your doctor.", ["Important Safety Information"])
    assert not det.find_missing_disclosures(
        "See   IMPORTANT   safety information below", ["Important Safety Information"]
    )


# --------------------------------------------------------------------------
# Verdict parsing — the fail-closed contract
# --------------------------------------------------------------------------


def test_unparseable_model_output_yields_uncertain(brief):
    criteria, _, failed = parse_verdict_json("I think it looks great!", brief)
    assert failed is True
    assert criteria, "must still emit a verdict per perceptual criterion"
    assert all(c.outcome is CriterionOutcome.UNCERTAIN for c in criteria)


def test_json_in_a_code_fence_is_parsed(brief):
    payload = """Here you go:
```json
{"criteria": [{"id": "logo_presence", "outcome": "pass", "confidence": 0.9,
 "rationale": "Visible in frame 2."}], "summary": "ok"}
```"""
    criteria, summary, failed = parse_verdict_json(payload, brief)
    assert failed is False
    assert summary == "ok"
    logo = next(c for c in criteria if c.criterion is CriterionId.LOGO_PRESENCE)
    assert logo.outcome is CriterionOutcome.PASS


def test_criteria_omitted_by_the_model_become_uncertain(brief):
    payload = '{"criteria": [{"id": "logo_presence", "outcome": "pass", ' \
              '"confidence": 0.9, "rationale": "seen"}], "summary": ""}'
    criteria, _, _ = parse_verdict_json(payload, brief)
    artifacts = next(c for c in criteria if c.criterion is CriterionId.VISUAL_ARTIFACTS)
    assert artifacts.outcome is CriterionOutcome.UNCERTAIN


def test_invented_criteria_are_ignored(brief):
    payload = '{"criteria": [{"id": "vibes", "outcome": "pass", "confidence": 1.0, ' \
              '"rationale": "great"}], "summary": ""}'
    criteria, _, _ = parse_verdict_json(payload, brief)
    assert all(c.criterion.value != "vibes" for c in criteria)


def test_fail_without_rationale_degrades_to_uncertain(brief):
    """An unexplained failure cannot drive a revision, so it asks a human."""
    payload = '{"criteria": [{"id": "visual_artifacts", "outcome": "fail", ' \
              '"confidence": 0.95, "rationale": ""}], "summary": ""}'
    criteria, _, _ = parse_verdict_json(payload, brief)
    artifacts = next(c for c in criteria if c.criterion is CriterionId.VISUAL_ARTIFACTS)
    assert artifacts.outcome is CriterionOutcome.UNCERTAIN


# --------------------------------------------------------------------------
# decide() — precedence
# --------------------------------------------------------------------------


def _cv(criterion, outcome, kind, *, confidence=None, severity=Severity.BLOCKING):
    return CriterionVerdict(
        criterion=criterion, outcome=outcome, kind=kind,
        severity=severity, rationale="because", confidence=confidence,
    )


def test_all_passing_verifies():
    verdict = decide(
        [_cv(CriterionId.ASPECT_RATIO, CriterionOutcome.PASS, CheckKind.DETERMINISTIC)],
        [_cv(CriterionId.LOGO_PRESENCE, CriterionOutcome.PASS, CheckKind.PERCEPTUAL,
             confidence=0.9)],
        run_id="r", take_number=1, iterations_remaining=2,
    )
    assert verdict.decision is BoardDecision.VERIFIED


def test_measured_failure_rejects_when_budget_remains():
    verdict = decide(
        [_cv(CriterionId.PALETTE_ADHERENCE, CriterionOutcome.FAIL, CheckKind.DETERMINISTIC)],
        [],
        run_id="r", take_number=1, iterations_remaining=2,
    )
    assert verdict.decision is BoardDecision.REJECTED
    assert "palette_adherence" in verdict.revision_guidance()


def test_exhausted_budget_escalates_rather_than_shipping():
    """The revision cap must never quietly become an approval."""
    verdict = decide(
        [_cv(CriterionId.PALETTE_ADHERENCE, CriterionOutcome.FAIL, CheckKind.DETERMINISTIC)],
        [],
        run_id="r", take_number=3, iterations_remaining=0,
    )
    assert verdict.decision is BoardDecision.ESCALATED


def test_any_uncertain_blocking_criterion_escalates():
    verdict = decide(
        [_cv(CriterionId.ASPECT_RATIO, CriterionOutcome.PASS, CheckKind.DETERMINISTIC)],
        [_cv(CriterionId.VISUAL_ARTIFACTS, CriterionOutcome.UNCERTAIN, CheckKind.PERCEPTUAL)],
        run_id="r", take_number=1, iterations_remaining=2,
    )
    assert verdict.decision is BoardDecision.ESCALATED


def test_low_confidence_failure_escalates_instead_of_revising():
    """Below the floor, asking a person beats spending a render on a guess."""
    verdict = decide(
        [],
        [_cv(CriterionId.VISUAL_ARTIFACTS, CriterionOutcome.FAIL, CheckKind.PERCEPTUAL,
             confidence=0.30)],
        run_id="r", take_number=1, iterations_remaining=2,
    )
    assert verdict.decision is BoardDecision.ESCALATED


def test_confident_perceptual_failure_rejects_and_revises():
    verdict = decide(
        [],
        [_cv(CriterionId.VISUAL_ARTIFACTS, CriterionOutcome.FAIL, CheckKind.PERCEPTUAL,
             confidence=0.92)],
        run_id="r", take_number=1, iterations_remaining=2,
    )
    assert verdict.decision is BoardDecision.REJECTED


def test_advisory_failure_alone_does_not_block():
    verdict = decide(
        [_cv(CriterionId.ASPECT_RATIO, CriterionOutcome.PASS, CheckKind.DETERMINISTIC)],
        [_cv(CriterionId.TONE_ALIGNMENT, CriterionOutcome.FAIL, CheckKind.PERCEPTUAL,
             confidence=0.9, severity=Severity.ADVISORY)],
        run_id="r", take_number=1, iterations_remaining=2,
    )
    assert verdict.decision is BoardDecision.VERIFIED


def test_measured_failure_outranks_a_confident_perceptual_pass():
    """A measurement beats an opinion when they disagree."""
    verdict = decide(
        [_cv(CriterionId.DURATION, CriterionOutcome.FAIL, CheckKind.DETERMINISTIC)],
        [_cv(CriterionId.LOGO_PRESENCE, CriterionOutcome.PASS, CheckKind.PERCEPTUAL,
             confidence=0.99)],
        run_id="r", take_number=1, iterations_remaining=1,
    )
    assert verdict.decision is BoardDecision.REJECTED
