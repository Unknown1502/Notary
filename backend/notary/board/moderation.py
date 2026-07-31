"""The deterministic screen, as a Genblaze `ModerationHook`.

Why a ModerationHook rather than a post-hoc check
-------------------------------------------------
Genblaze runs moderation at two points in a step's lifecycle -- `check_prompt()`
before cache lookup or any provider call, and `check_output()` after generation
but before caching. A failure sets `step.status=FAILED`,
`error_code=INVALID_INPUT`, and populates `step.metadata["moderation"]` with
`stage`, `reason`, and `flagged_categories`.

That last detail is the reason this is a hook and not a function Notary calls
afterwards: **step metadata is part of the manifest.** Running the screen here
means the compliance finding is sealed by the same canonical hash that seals
the asset, rather than living in a sidecar file that a skeptic could point at
and ask "what stops you rewriting that?"

The split of labour:

    check_prompt()   Screens the brief BEFORE spending money. A brief missing
                     its mandatory safety disclosure cannot produce a compliant
                     asset no matter how good the render is, so failing here
                     saves a full Kling render -- minutes and real dollars.

    check_output()   Screens the rendered frames. Palette adherence and frame
                     geometry, measured from pixels.

Only deterministic criteria live here. Perceptual judgement is a separate
pipeline step (board/review.py) because it needs a model, costs money, and is
allowed to be wrong -- three properties that should never be hidden inside a
hook that reads like a validation function.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from ..genblaze_compat import ModerationHook, ModerationResult
from ..models import (
    CampaignBrief,
    CheckKind,
    CriterionId,
    CriterionOutcome,
    CriterionVerdict,
)
from . import deterministic as det
from .rubric import get_profile

log = logging.getLogger(__name__)


class BrandGuardrailHook(ModerationHook):
    """Deterministic compliance screening bound to one campaign brief.

    Stateful by design: it accumulates the CriterionVerdicts it produced so the
    Board can fold them into the complete verdict without re-measuring. One
    instance per run -- never share across runs.
    """

    def __init__(self, brief: CampaignBrief) -> None:
        super().__init__()
        self.brief = brief
        self.profile = get_profile(brief.compliance_profile)
        self._active: set[CriterionId] = {
            c.id for c in self.profile if c.kind is CheckKind.DETERMINISTIC
        }
        self.verdicts: list[CriterionVerdict] = []

        # Populated by the runner before the output stage, because the SDK
        # hands check_output() the generated asset, not our extracted frames.
        self.frame_paths: list[Path] = []
        self.frame_keys: list[str] = []
        self.observed_duration: float | None = None

    # ------------------------------------------------------------------ util

    def _record(self, verdict: CriterionVerdict) -> CriterionVerdict:
        self.verdicts = [v for v in self.verdicts if v.criterion != verdict.criterion]
        self.verdicts.append(verdict)
        return verdict

    def _enabled(self, criterion: CriterionId) -> bool:
        return criterion in self._active

    @staticmethod
    def _blocking(verdicts: list[CriterionVerdict]) -> list[CriterionVerdict]:
        return [v for v in verdicts if v.blocks_certification]

    def _result(self, produced: list[CriterionVerdict], stage: str) -> Any:
        """Collapse criterion verdicts into the SDK's ModerationResult."""
        failures = self._blocking(produced)
        if not failures:
            return ModerationResult(allowed=True)

        reason = " ".join(f"[{f.criterion.value}] {f.rationale}" for f in failures)
        log.info(
            "moderation blocked at %s for run on campaign %s: %s",
            stage,
            self.brief.campaign_id,
            ", ".join(f.criterion.value for f in failures),
        )
        return ModerationResult(
            allowed=False,
            reason=reason,
            flagged_categories=[f.criterion.value for f in failures],
        )

    # ------------------------------------------------------- SDK entry points

    def check_prompt(self, prompt: str | None, params: dict[str, Any] | None = None):
        """Pre-step screen. Runs before any provider is billed."""
        produced: list[CriterionVerdict] = []
        copy_under_review = self._copy_corpus(prompt)

        if self._enabled(CriterionId.BANNED_LEXEMES):
            produced.append(
                self._record(
                    det.check_banned_lexemes(copy_under_review, self.brief.brand_kit)
                )
            )

        if self._enabled(CriterionId.MANDATORY_DISCLOSURE):
            produced.append(
                self._record(
                    det.check_mandatory_disclosure(
                        copy_under_review, self.brief.brand_kit
                    )
                )
            )

        return self._result(produced, stage="prompt")

    def check_output(self, asset: Any = None, step: Any = None, **_: Any):
        """Post-step screen over the frames extracted from the generated asset."""
        produced: list[CriterionVerdict] = []

        if not self.frame_paths:
            # Nothing to measure. Explicitly NOT a pass -- the Board turns a
            # missing measurement into UNCERTAIN, which escalates to a human.
            log.warning(
                "check_output called with no extracted frames; "
                "deterministic visual criteria will report UNCERTAIN."
            )

        if self._enabled(CriterionId.ASPECT_RATIO):
            produced.append(
                self._record(
                    det.check_aspect_ratio(self.frame_paths, self.brief.channel)
                )
            )

        if self._enabled(CriterionId.DURATION):
            produced.append(
                self._record(
                    det.check_duration(self.observed_duration, self.brief.channel)
                )
            )

        if self._enabled(CriterionId.PALETTE_ADHERENCE):
            produced.append(
                self._record(
                    det.check_palette(
                        self.frame_paths,
                        self.brief.brand_kit,
                        frame_keys=self.frame_keys,
                    )
                )
            )

        return self._result(produced, stage="output")

    # ---------------------------------------------------------------- helpers

    def _copy_corpus(self, prompt: str | None) -> str:
        """Everything that counts as reviewable copy for this take.

        The prompt plus the mandatory disclosures the brief claims to carry.
        Disclosures are appended so that BANNED_LEXEMES screens them too -- a
        legally required phrase is not exempt from the prohibited-term list, and
        a collision between the two is precisely the kind of contradiction a
        human needs to resolve.
        """
        parts = [prompt or self.brief.prompt]
        parts.extend(self.brief.brand_kit.mandatory_disclosures)
        return "\n".join(p for p in parts if p)

    def attach_frames(
        self,
        frame_paths: list[Path],
        frame_keys: list[str],
        duration_seconds: float | None,
    ) -> None:
        """Runner hands the hook what it needs for the output stage."""
        self.frame_paths = frame_paths
        self.frame_keys = frame_keys
        self.observed_duration = duration_seconds

    def run_all(self, prompt: str | None = None) -> list[CriterionVerdict]:
        """Execute both stages directly.

        Used by the replay harness and the evaluation suite, where there is no
        live pipeline to host the hook but the measurements must be identical.
        Same code path, same thresholds, same numbers.
        """
        self.check_prompt(prompt)
        self.check_output()
        return list(self.verdicts)

    def unmeasured(self) -> list[CriterionVerdict]:
        """Deterministic criteria that never produced a verdict.

        Reported as UNCERTAIN rather than silently omitted. A criterion that
        did not run is not a criterion that passed, and the difference decides
        whether an asset ships.
        """
        seen = {v.criterion for v in self.verdicts}
        missing: list[CriterionVerdict] = []
        for criterion in self.profile:
            if criterion.kind is not CheckKind.DETERMINISTIC:
                continue
            if criterion.id in seen:
                continue
            missing.append(
                CriterionVerdict(
                    criterion=criterion.id,
                    outcome=CriterionOutcome.UNCERTAIN,
                    kind=CheckKind.DETERMINISTIC,
                    severity=criterion.severity,
                    rationale="Check did not execute; escalating rather than assuming pass.",
                )
            )
        return missing

    def snapshot(self) -> dict[str, Any]:
        """Structured record for `step.metadata['moderation']`."""
        return {
            "profile": self.brief.compliance_profile,
            "campaign_id": self.brief.campaign_id,
            "criteria": [
                {
                    "id": v.criterion.value,
                    "outcome": v.outcome.value,
                    "severity": v.severity.value,
                    "rationale": v.rationale,
                    "measurement": v.measurement,
                }
                for v in self.verdicts
            ],
            "blocking_failures": [
                v.criterion.value for v in self._blocking(self.verdicts)
            ],
        }
