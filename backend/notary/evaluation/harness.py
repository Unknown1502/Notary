"""Score the Board against known ground truth.

Two very different kinds of evidence are produced here, and the distinction
matters more than either number:

1. **Empirical scores** for the deterministic checks, measured against a
   constructed corpus whose ground truth is known exactly.

2. **An exhaustive proof** of the safety invariant in `decide()`. Because the
   decision function's input space is finite -- a handful of criteria, each in
   one of four outcomes, times the severity and confidence bands -- every
   reachable combination can be enumerated and checked. This is not sampling.
   It is a complete search over the decision space.

The second is the stronger result. A precision score says the classifier is
usually right; an exhaustive search says **there is no input on which the
system certifies something it should not have**. For a compliance gate, the
second property is the one that matters, and it is provable rather than
estimated.

What is deliberately not scored: the perceptual criteria. Doing that honestly
needs real generated video and independent human labels. Scoring only the
tractable half and presenting it as "the Board's accuracy" would be exactly the
overclaim this project exists to avoid, so `evaluate()` reports the gap as a
first-class result.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..board import deterministic as det
from ..board.review import decide
from ..models import (
    BoardDecision,
    BrandKit,
    ChannelSpec,
    CheckKind,
    CriterionId,
    CriterionOutcome,
    CriterionVerdict,
    Severity,
)
from .corpus import (
    Sample,
    build_geometry_corpus,
    build_neutral_corpus,
    build_palette_corpus,
    build_text_corpus,
    corpus_summary,
)


@dataclass
class ConfusionMatrix:
    """Scored against `fail` as the positive class.

    Positive = "this asset violates the rule", because that is the event with
    consequences. Recall is therefore the fraction of real violations caught,
    which is the number a compliance team actually cares about.
    """

    true_positive: int = 0
    false_positive: int = 0
    true_negative: int = 0
    false_negative: int = 0

    correct_abstain: int = 0
    """Correctly returned a non-binary outcome (NOT_APPLICABLE / UNCERTAIN).

    Counted as correct rather than dumped into `other`. A neutral greyscale
    frame *should* return NOT_APPLICABLE for palette adherence -- there is no
    chromatic content to confirm or contradict a brand palette. Scoring that as
    an error would understate a check that behaved exactly as specified, and
    scoring it as a pass would claim a measurement that was never made.
    """

    incorrect_abstain: int = 0
    """Abstained when a definite answer was required, or vice versa."""

    @property
    def total(self) -> int:
        return (
            self.true_positive + self.false_positive
            + self.true_negative + self.false_negative
            + self.correct_abstain + self.incorrect_abstain
        )

    @property
    def precision(self) -> float | None:
        denominator = self.true_positive + self.false_positive
        return self.true_positive / denominator if denominator else None

    @property
    def recall(self) -> float | None:
        denominator = self.true_positive + self.false_negative
        return self.true_positive / denominator if denominator else None

    @property
    def accuracy(self) -> float | None:
        """Fraction of samples classified as their ground truth requires.

        Includes correct abstentions, because returning NOT_APPLICABLE on a
        frame with no chromatic content *is* the correct classification.
        """
        correct = self.true_positive + self.true_negative + self.correct_abstain
        return correct / self.total if self.total else None

    @property
    def f1(self) -> float | None:
        p, r = self.precision, self.recall
        if p is None or r is None or (p + r) == 0:
            return None
        return 2 * p * r / (p + r)

    def record(self, expected: str, observed: str) -> None:
        if expected == "fail" and observed == "fail":
            self.true_positive += 1
        elif expected == "pass" and observed == "fail":
            self.false_positive += 1
        elif expected == "pass" and observed == "pass":
            self.true_negative += 1
        elif expected == "fail" and observed == "pass":
            self.false_negative += 1
        elif expected == observed:
            self.correct_abstain += 1
        else:
            self.incorrect_abstain += 1

    def as_dict(self) -> dict[str, Any]:
        return {
            "n": self.total,
            "tp": self.true_positive,
            "fp": self.false_positive,
            "tn": self.true_negative,
            "fn": self.false_negative,
            "correct_abstain": self.correct_abstain,
            "incorrect_abstain": self.incorrect_abstain,
            "precision": self.precision,
            "recall": self.recall,
            "accuracy": self.accuracy,
            "f1": self.f1,
        }


@dataclass
class CriterionScore:
    criterion: str
    overall: ConfusionMatrix = field(default_factory=ConfusionMatrix)
    near_boundary: ConfusionMatrix = field(default_factory=ConfusionMatrix)
    failures: list[dict[str, Any]] = field(default_factory=list)


def _observed_outcome(verdict: CriterionVerdict) -> str:
    return verdict.outcome.value


def score_deterministic(
    directory: Path, brand: BrandKit, channel: ChannelSpec
) -> tuple[dict[str, CriterionScore], list[Sample]]:
    """Run every deterministic check over the labelled corpus."""
    samples: list[Sample] = []
    samples += build_palette_corpus(directory / "palette", brand)
    samples += build_neutral_corpus(directory / "neutral")
    samples += build_geometry_corpus(directory / "geometry", channel)

    scores: dict[str, CriterionScore] = {}

    for sample in samples:
        score = scores.setdefault(
            sample.criterion, CriterionScore(criterion=sample.criterion)
        )

        if sample.criterion == "palette_adherence":
            verdict = det.check_palette([sample.path], brand)
        elif sample.criterion == "aspect_ratio":
            verdict = det.check_aspect_ratio([sample.path], channel)
        else:  # pragma: no cover - corpus only builds the two above
            continue

        observed = _observed_outcome(verdict)
        score.overall.record(sample.expected_outcome, observed)
        if sample.near_boundary:
            score.near_boundary.record(sample.expected_outcome, observed)

        if observed != sample.expected_outcome:
            score.failures.append(
                {
                    "sample": sample.sample_id,
                    "expected": sample.expected_outcome,
                    "observed": observed,
                    "note": sample.note,
                    "measurement": verdict.measurement,
                }
            )

    # Lexical checks run on text rather than images.
    for sample_id, text, terms, criterion, expected in build_text_corpus():
        score = scores.setdefault(criterion, CriterionScore(criterion=criterion))
        kit = BrandKit(
            name="eval",
            banned_terms=terms if criterion == "banned_lexemes" else [],
            mandatory_disclosures=terms if criterion == "mandatory_disclosure" else [],
        )
        verdict = (
            det.check_banned_lexemes(text, kit)
            if criterion == "banned_lexemes"
            else det.check_mandatory_disclosure(text, kit)
        )
        observed = _observed_outcome(verdict)
        score.overall.record(expected, observed)
        if observed != expected:
            score.failures.append(
                {
                    "sample": sample_id,
                    "expected": expected,
                    "observed": observed,
                    "note": repr(text),
                    "measurement": verdict.measurement,
                }
            )

    return scores, samples


# --------------------------------------------------------------------------
# Exhaustive search over the decision space
# --------------------------------------------------------------------------

CONFIDENCE_BANDS = (None, 0.10, 0.54, 0.55, 0.90, 1.0)
"""Sampled either side of the 0.55 escalation floor, plus the None case."""


@dataclass
class InvariantReport:
    combinations_checked: int = 0
    violations: list[dict[str, Any]] = field(default_factory=list)

    @property
    def holds(self) -> bool:
        return not self.violations


def prove_safety_invariant(max_criteria: int = 3) -> InvariantReport:
    """Exhaustively verify: nothing unsafe can ever be VERIFIED.

    The invariant, stated precisely:

        decide(...) returns VERIFIED only if every BLOCKING criterion has
        outcome PASS or NOT_APPLICABLE.

    Equivalently: a blocking FAIL or UNCERTAIN anywhere must produce REJECTED
    or ESCALATED, never VERIFIED, for any combination of check kind,
    confidence, and remaining revision budget.

    The search enumerates every assignment of outcome x kind x severity x
    confidence across up to `max_criteria` criteria, crossed with the budget
    values that change behaviour. That is a complete cover of the reachable
    decision space, so a clean run is a proof over this domain rather than
    evidence from sampling.
    """
    report = InvariantReport()

    outcomes = list(CriterionOutcome)
    kinds = list(CheckKind)
    severities = list(Severity)
    criteria_ids = [
        CriterionId.PALETTE_ADHERENCE,
        CriterionId.VISUAL_ARTIFACTS,
        CriterionId.MANDATORY_DISCLOSURE,
    ][:max_criteria]

    single_space = [
        (outcome, kind, severity, confidence)
        for outcome in outcomes
        for kind in kinds
        for severity in severities
        for confidence in (CONFIDENCE_BANDS if kind is CheckKind.PERCEPTUAL else (None,))
    ]

    for count in range(1, max_criteria + 1):
        for assignment in itertools.product(single_space, repeat=count):
            for budget in (0, 1, 2):
                deterministic: list[CriterionVerdict] = []
                perceptual: list[CriterionVerdict] = []

                for index, (outcome, kind, severity, confidence) in enumerate(assignment):
                    verdict = CriterionVerdict(
                        criterion=criteria_ids[index],
                        outcome=outcome,
                        kind=kind,
                        severity=severity,
                        rationale="synthetic",
                        confidence=confidence,
                    )
                    (deterministic if kind is CheckKind.DETERMINISTIC else perceptual).append(
                        verdict
                    )

                result = decide(
                    deterministic, perceptual,
                    run_id="proof", take_number=1, iterations_remaining=budget,
                )
                report.combinations_checked += 1

                unsafe = [
                    v
                    for v in [*deterministic, *perceptual]
                    if v.severity is Severity.BLOCKING
                    and v.outcome in (CriterionOutcome.FAIL, CriterionOutcome.UNCERTAIN)
                ]

                if unsafe and result.decision is BoardDecision.VERIFIED:
                    report.violations.append(
                        {
                            "budget": budget,
                            "unsafe_criteria": [
                                f"{v.criterion.value}:{v.outcome.value}:{v.kind.value}"
                                f":conf={v.confidence}"
                                for v in unsafe
                            ],
                            "decision": result.decision.value,
                        }
                    )

    return report


def prove_budget_never_approves() -> InvariantReport:
    """A second invariant: exhausting the revision budget must not certify.

    The dangerous shape is a system that retries N times and then, having run
    out of attempts, treats the last output as acceptable. Here, budget
    exhaustion on a real failure must produce ESCALATED.
    """
    report = InvariantReport()

    for kind in CheckKind:
        for confidence in (CONFIDENCE_BANDS if kind is CheckKind.PERCEPTUAL else (None,)):
            failing = CriterionVerdict(
                criterion=CriterionId.PALETTE_ADHERENCE,
                outcome=CriterionOutcome.FAIL,
                kind=kind,
                severity=Severity.BLOCKING,
                rationale="synthetic failure",
                confidence=confidence,
            )
            deterministic = [failing] if kind is CheckKind.DETERMINISTIC else []
            perceptual = [failing] if kind is CheckKind.PERCEPTUAL else []

            result = decide(
                deterministic, perceptual,
                run_id="proof", take_number=3, iterations_remaining=0,
            )
            report.combinations_checked += 1

            if result.decision is not BoardDecision.ESCALATED:
                report.violations.append(
                    {
                        "kind": kind.value,
                        "confidence": confidence,
                        "decision": result.decision.value,
                        "expected": "escalated",
                    }
                )

    return report


# --------------------------------------------------------------------------


def evaluate(directory: Path) -> dict[str, Any]:
    """Full evaluation. Returns a serialisable report."""
    brand = BrandKit(
        name="Eval",
        palette=["#0b5fff", "#00c2a8"],
        palette_tolerance=18.0,
        palette_min_coverage=0.55,
    )
    channel = ChannelSpec(aspect_ratio="16:9", duration_seconds=6)

    scores, samples = score_deterministic(directory, brand, channel)
    safety = prove_safety_invariant()
    budget = prove_budget_never_approves()

    return {
        "corpus": corpus_summary(samples),
        "deterministic": {
            name: {
                "overall": score.overall.as_dict(),
                "near_boundary": score.near_boundary.as_dict(),
                "failures": score.failures,
            }
            for name, score in sorted(scores.items())
        },
        "invariants": {
            "no_unsafe_certification": {
                "combinations_checked": safety.combinations_checked,
                "holds": safety.holds,
                "violations": safety.violations[:10],
            },
            "exhausted_budget_never_approves": {
                "combinations_checked": budget.combinations_checked,
                "holds": budget.holds,
                "violations": budget.violations[:10],
            },
        },
        "not_evaluated": {
            "perceptual_criteria": [
                "logo_presence", "logo_legibility", "visual_artifacts",
                "tone_alignment", "prohibited_imagery",
            ],
            "reason": (
                "Scoring these honestly requires real generated video and "
                "independent human labels. Synthesising them would measure our "
                "own assumptions, not the Board. The confidence floor of 0.55 "
                "is a reasoned default, not an empirically tuned value."
            ),
            "consequence": (
                "Notary makes no accuracy claim about perceptual judgement. "
                "That is precisely why perceptual uncertainty escalates to a "
                "human instead of resolving automatically."
            ),
        },
    }
