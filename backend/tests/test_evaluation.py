"""Tests for the evaluation harness.

The harness is what backs a public accuracy claim, so it needs its own tests:
a scorer that quietly miscounts would produce a confident, wrong number in the
documentation, which is worse than publishing nothing.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from notary.evaluation import (
    ConfusionMatrix,
    build_palette_corpus,
    build_text_corpus,
    evaluate,
    prove_budget_never_approves,
    prove_safety_invariant,
)
from notary.models import BrandKit


def test_confusion_matrix_counts_each_quadrant():
    m = ConfusionMatrix()
    m.record("fail", "fail")   # tp
    m.record("pass", "fail")   # fp
    m.record("pass", "pass")   # tn
    m.record("fail", "pass")   # fn

    assert (m.true_positive, m.false_positive, m.true_negative, m.false_negative) == (
        1, 1, 1, 1,
    )
    assert m.precision == 0.5
    assert m.recall == 0.5
    assert m.accuracy == 0.5


def test_correct_abstention_counts_as_correct():
    """NOT_APPLICABLE on a neutral frame is the right answer, not an error.

    Bucketing it as 'other' would have understated a check that made zero
    mistakes -- which is exactly the bug this test was written to lock down.
    """
    m = ConfusionMatrix()
    m.record("not_applicable", "not_applicable")
    m.record("pass", "pass")

    assert m.correct_abstain == 1
    assert m.incorrect_abstain == 0
    assert m.accuracy == 1.0


def test_wrong_abstention_counts_as_incorrect():
    m = ConfusionMatrix()
    m.record("fail", "uncertain")
    assert m.incorrect_abstain == 1
    assert m.accuracy == 0.0


def test_precision_and_recall_are_none_without_positives():
    m = ConfusionMatrix()
    m.record("pass", "pass")
    assert m.precision is None
    assert m.recall is None


def test_palette_corpus_ground_truth_is_exact():
    """Constructed coverage must match the label, or every score is garbage."""
    brand = BrandKit(name="t", palette=["#0b5fff"], palette_min_coverage=0.55)
    with tempfile.TemporaryDirectory() as tmp:
        samples = build_palette_corpus(Path(tmp), brand)

    assert samples
    for sample in samples:
        assert sample.true_coverage is not None
        expected = "pass" if sample.true_coverage >= 0.55 else "fail"
        assert sample.expected_outcome == expected, sample.note


def test_corpus_concentrates_near_the_threshold():
    """A corpus of obvious cases would score ~100% and mean nothing."""
    brand = BrandKit(name="t", palette=["#0b5fff"], palette_min_coverage=0.55)
    with tempfile.TemporaryDirectory() as tmp:
        samples = build_palette_corpus(Path(tmp), brand)

    near = [s for s in samples if s.near_boundary]
    assert len(near) / len(samples) > 0.5


def test_text_corpus_includes_substring_traps():
    """'secure' containing 'cure' is the trap a naive check falls into."""
    cases = {case[0] for case in build_text_corpus()}
    assert {"ban-secure", "ban-cureless", "ban-miracles"} <= cases


def test_safety_invariant_holds_exhaustively():
    """The headline claim. If this ever fails, the product is unsafe."""
    report = prove_safety_invariant()
    assert report.combinations_checked > 100_000
    assert report.holds, f"unsafe certifications reachable: {report.violations[:3]}"


def test_exhausted_budget_never_approves():
    report = prove_budget_never_approves()
    assert report.holds, f"budget exhaustion approved: {report.violations[:3]}"


def test_full_evaluation_reports_the_unmeasured_gap():
    """The report must name what it did not evaluate.

    A report that silently scored only the tractable half and presented it as
    'the Board's accuracy' would be the exact overclaim this project avoids.
    """
    with tempfile.TemporaryDirectory() as tmp:
        report = evaluate(Path(tmp))

    assert report["not_evaluated"]["perceptual_criteria"]
    assert "human labels" in report["not_evaluated"]["reason"]
    assert report["invariants"]["no_unsafe_certification"]["holds"] is True

    for entry in report["deterministic"].values():
        assert entry["overall"]["n"] > 0
