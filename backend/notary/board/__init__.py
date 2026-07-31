"""The Creative Review Board.

Two stages, deliberately built from different materials:

    moderation.py    DETERMINISTIC. A genblaze ModerationHook running computed
                     checks -- geometry, duration, palette distance in CIE Lab,
                     exact-match lexemes. No model. Findings land in step
                     metadata, therefore in the manifest.

    review.py        PERCEPTUAL. A vision model wrapped as a genblaze
                     SyncProvider so its verdict is a real manifest step with a
                     content-addressed asset. Allowed to be wrong, which is why
                     uncertainty escalates instead of shipping.

    evaluator.py     Bridges the combined decision into genblaze AgentLoop, so
                     a rejection becomes a verdict-conditioned revision with
                     automatic parent_run_id lineage.

The Board screens. It does not have final authority: anything it cannot clear
with confidence goes to a human. See docs/TRUST-MODEL.md.
"""

from .deterministic import (
    check_aspect_ratio,
    check_banned_lexemes,
    check_duration,
    check_mandatory_disclosure,
    check_palette,
    delta_e_76,
    measure_palette,
    rgb_to_lab,
)
from .evaluator import BoardEvaluator
from .moderation import BrandGuardrailHook
from .review import (
    BoardReviewProvider,
    VisionReviewOutput,
    decide,
    parse_verdict_json,
    run_vision_review,
)
from .rubric import PROFILES, describe_profiles, get_profile

__all__ = [
    "PROFILES",
    "BoardEvaluator",
    "BoardReviewProvider",
    "BrandGuardrailHook",
    "VisionReviewOutput",
    "check_aspect_ratio",
    "check_banned_lexemes",
    "check_duration",
    "check_mandatory_disclosure",
    "check_palette",
    "decide",
    "delta_e_76",
    "describe_profiles",
    "get_profile",
    "measure_palette",
    "parse_verdict_json",
    "rgb_to_lab",
    "run_vision_review",
]
