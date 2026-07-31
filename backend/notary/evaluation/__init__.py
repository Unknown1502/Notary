"""Measuring the Board.

Produces two kinds of evidence:

  * empirical precision/recall for the deterministic checks, scored against a
    corpus whose ground truth is constructed rather than labelled;
  * an exhaustive search over `decide()`'s finite input space proving that no
    combination of findings can certify something unsafe.

The second is the stronger claim, and it is the one a compliance gate needs.
"""

from .corpus import (
    Sample,
    build_geometry_corpus,
    build_neutral_corpus,
    build_palette_corpus,
    build_text_corpus,
    corpus_summary,
)
from .harness import (
    ConfusionMatrix,
    CriterionScore,
    InvariantReport,
    evaluate,
    prove_budget_never_approves,
    prove_safety_invariant,
    score_deterministic,
)

__all__ = [
    "ConfusionMatrix",
    "CriterionScore",
    "InvariantReport",
    "Sample",
    "build_geometry_corpus",
    "build_neutral_corpus",
    "build_palette_corpus",
    "build_text_corpus",
    "corpus_summary",
    "evaluate",
    "prove_budget_never_approves",
    "prove_safety_invariant",
    "score_deterministic",
]
