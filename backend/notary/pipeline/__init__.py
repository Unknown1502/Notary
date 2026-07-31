"""Generation orchestration.

`factory.py` builds the chained image -> video Pipeline for one iteration,
folding the Board's rationale into the prompt when revising.

`runner.py` drives the whole session: the pre-spend brief screen, the
Genblaze AgentLoop, the two-tier failure model, and certification into the
Object-Locked vault.
"""

from .factory import (
    ProviderBundle,
    build_pipeline,
    compose_storyboard_prompt,
    compose_video_prompt,
    make_sink,
    resolve_providers,
)
from .runner import ReviewRunner, run_review

__all__ = [
    "ProviderBundle",
    "ReviewRunner",
    "build_pipeline",
    "compose_storyboard_prompt",
    "compose_video_prompt",
    "make_sink",
    "resolve_providers",
    "run_review",
]
