"""Bridge between the Board and Genblaze's `AgentLoop`.

`AgentLoop` runs generate -> evaluate -> refine until an evaluator passes, and
"every iteration after the first calls Pipeline.from_result(prev) automatically,
so each manifest carries parent_run_id pointing back to the previous attempt."

That single sentence replaces the entire hand-rolled retry-and-lineage layer
Notary would otherwise need: the revision cap, the parent linking, the
per-iteration streaming, and cost aggregation all come from the SDK. What
Notary supplies is the part the SDK cannot know -- what "good" means.

The evaluator is where the Board plugs in. It returns `passed` plus `feedback`,
and the feedback string is fed to the next iteration's pipeline factory, which
is precisely the verdict-conditioned revision: the next take is generated with
the reasons the last one failed, not a reroll of the same prompt.

One subtlety worth stating, because it decides correctness:

    ESCALATED must stop the loop, and it must not stop it by passing.

`AgentLoop` halts on evaluator success or on the iteration cap. An escalation
is neither -- it is "stop, but do not certify". So the evaluator reports
`passed=False` for an escalation and the runner inspects the verdict after the
loop returns to distinguish "passed" from "gave up". Reporting an escalation as
`passed=True` to end the loop early would ship an unreviewed asset, which is
the exact failure this product exists to prevent.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from ..genblaze_compat import EvaluationResult
from ..models import BoardDecision, BoardVerdict

log = logging.getLogger(__name__)


@dataclass
class BoardEvaluator:
    """Callable evaluator for `CallableEvaluator(judge)`.

    Holds the verdict history so the runner can reconstruct the full review
    trail after the loop finishes, and so a later iteration can be compared
    against an earlier one for the before/after view.
    """

    review_fn: Any
    """Callable(result, iteration) -> BoardVerdict. Injected by the runner so
    this class stays free of storage, frame extraction, and provider concerns."""

    max_iterations: int
    verdicts: list[BoardVerdict] = field(default_factory=list)

    @property
    def iterations_used(self) -> int:
        return len(self.verdicts)

    @property
    def latest(self) -> BoardVerdict | None:
        return self.verdicts[-1] if self.verdicts else None

    @property
    def certified(self) -> bool:
        v = self.latest
        return v is not None and v.decision is BoardDecision.VERIFIED

    @property
    def escalated(self) -> bool:
        v = self.latest
        return v is not None and v.decision is BoardDecision.ESCALATED

    def __call__(self, result: Any) -> Any:
        """Judge one iteration's output."""
        iteration = self.iterations_used + 1
        verdict = self.review_fn(result, iteration)
        self.verdicts.append(verdict)

        log.info(
            "Board iteration %d/%d -> %s (%s)",
            iteration,
            self.max_iterations,
            verdict.decision.value,
            verdict.summary,
        )

        if verdict.decision is BoardDecision.VERIFIED:
            return _evaluation(True, verdict.summary, score=1.0)

        if verdict.decision is BoardDecision.ESCALATED:
            # Not a pass. The runner detects this after the loop and routes to
            # the human queue rather than certifying.
            return _evaluation(False, verdict.summary, score=0.5)

        guidance = verdict.revision_guidance() or verdict.summary
        return _evaluation(False, guidance, score=0.0)

    def should_continue(self) -> bool:
        """Whether another revision is worth attempting.

        The runner consults this before letting the loop spend another render.
        An escalation is terminal: more attempts will not resolve ambiguity that
        a model already declined to resolve, and burning a Kling render to
        re-ask the same unanswerable question is waste.
        """
        if self.escalated or self.certified:
            return False
        return self.iterations_used < self.max_iterations

    def iterations_remaining(self) -> int:
        return max(0, self.max_iterations - self.iterations_used)


def _evaluation(passed: bool, feedback: str, *, score: float | None = None) -> Any:
    """Construct the SDK's EvaluationResult, tolerating signature variation."""
    try:
        return EvaluationResult(passed=passed, feedback=feedback, score=score)
    except TypeError:
        try:
            return EvaluationResult(passed=passed, feedback=feedback)
        except TypeError:  # pragma: no cover - last resort
            return _PlainEvaluation(passed=passed, feedback=feedback, score=score)


@dataclass
class _PlainEvaluation:
    passed: bool
    feedback: str
    score: float | None = None
