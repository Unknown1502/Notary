"""The single place Notary touches the Genblaze SDK's import surface.

Why this file exists
--------------------
Two reasons, and the second matters more.

1. **Replay mode must run with zero dependencies.** A judge cloning this repo
   gets a working app without installing provider SDKs or holding an API key.
   That means every genblaze import has to degrade gracefully.

2. **Unverified API surface should live in one auditable place.** The Genblaze
   README shows two different return shapes for `Pipeline.run()` (`result.run`
   / `result.manifest` in one example, `run, manifest = ...` in another), and
   `chat()`'s multimodal support is not documented. Rather than sprinkle
   defensive `getattr` through the codebase, every such assumption is resolved
   here, named, and recorded in docs/SPIKES.md with the check that confirms it.

The rule for the rest of the codebase: **never import genblaze directly.**
Import from this module. That way the day a spike result changes, exactly one
file changes.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

log = logging.getLogger(__name__)

# --------------------------------------------------------------------------
# Import probing
# --------------------------------------------------------------------------

GENBLAZE_AVAILABLE = False
GENBLAZE_IMPORT_ERROR: str | None = None

Pipeline: Any = None
Step: Any = None
Manifest: Any = None
Modality: Any = None
chat: Any = None
AgentLoop: Any = None
AgentContext: Any = None
CallableEvaluator: Any = None
EvaluationResult: Any = None
ModerationHook: Any = None
ModerationResult: Any = None
SyncProvider: Any = None
ProviderError: Any = None
Asset: Any = None
ObjectStorageSink: Any = None
S3StorageBackend: Any = None
KeyStrategy: Any = None

try:  # pragma: no cover - exercised by environment, not by tests
    from genblaze_core import (  # type: ignore[import-not-found]
        AgentContext,
        AgentLoop,
        Asset,
        CallableEvaluator,
        EvaluationResult,
        Manifest,
        Modality,
        ModerationHook,
        ModerationResult,
        Pipeline,
        ProviderError,
        Step,
        SyncProvider,
        chat,
    )
    from genblaze_s3 import (  # type: ignore[import-not-found]
        KeyStrategy,
        ObjectStorageSink,
        S3StorageBackend,
    )

    GENBLAZE_AVAILABLE = True
except Exception as exc:  # noqa: BLE001 - any import failure degrades to replay
    GENBLAZE_IMPORT_ERROR = f"{type(exc).__name__}: {exc}"
    log.info(
        "genblaze SDK unavailable (%s). Live generation is disabled; "
        "replay mode is fully functional.",
        GENBLAZE_IMPORT_ERROR,
    )


# --------------------------------------------------------------------------
# Fallbacks so type annotations and subclassing work without the SDK
# --------------------------------------------------------------------------

if not GENBLAZE_AVAILABLE:

    class _StubBase:
        """Stand-in base class used only when the SDK is absent.

        Subclasses of this never execute -- live generation is gated on
        GENBLAZE_AVAILABLE -- but they must be *constructible as classes* so
        module import succeeds and replay mode boots.
        """

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self._stub_args = args
            self._stub_kwargs = kwargs

    class ModerationHook(_StubBase):  # type: ignore[no-redef]
        pass

    class SyncProvider(_StubBase):  # type: ignore[no-redef]
        pass

    class ProviderError(RuntimeError):  # type: ignore[no-redef]
        def __init__(self, message: str, error_code: str = "UNKNOWN") -> None:
            super().__init__(message)
            self.error_code = error_code

    @dataclass
    class ModerationResult:  # type: ignore[no-redef]
        allowed: bool
        reason: str = ""
        flagged_categories: list[str] | None = None

    @dataclass
    class EvaluationResult:  # type: ignore[no-redef]
        passed: bool
        feedback: str = ""
        score: float | None = None


# --------------------------------------------------------------------------
# SPIKE 1 -- Pipeline.run() return shape
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class RunOutcome:
    """Normalized view of whatever `Pipeline.run()` handed back.

    Resolves docs/SPIKES.md #1. The README shows both an object with `.run` /
    `.manifest` attributes and a `(run, manifest)` tuple. Rather than bet on
    one, `normalize_run_result` accepts either and everything downstream
    consumes this dataclass.
    """

    run: Any
    manifest: Any
    raw: Any

    @property
    def run_id(self) -> str | None:
        return _first_attr(self.run, "run_id", "id")

    @property
    def parent_run_id(self) -> str | None:
        return _first_attr(self.run, "parent_run_id")

    @property
    def steps(self) -> list[Any]:
        steps = _first_attr(self.run, "steps") or []
        return list(steps)

    @property
    def canonical_hash(self) -> str | None:
        return _first_attr(
            self.manifest, "canonical_hash", "hash", "content_hash", "digest"
        )


def _first_attr(obj: Any, *names: str) -> Any:
    for name in names:
        value = getattr(obj, name, None)
        if value is not None:
            return value
    if isinstance(obj, dict):
        for name in names:
            if name in obj:
                return obj[name]
    return None


def normalize_run_result(result: Any) -> RunOutcome:
    """Accept object-with-attributes OR (run, manifest) tuple. See SPIKES #1."""
    if isinstance(result, tuple) and len(result) == 2:
        run, manifest = result
        return RunOutcome(run=run, manifest=manifest, raw=result)

    run = _first_attr(result, "run")
    manifest = _first_attr(result, "manifest")

    if run is None and manifest is None:
        # Some shapes return the run itself with the manifest hanging off it.
        run = result
        manifest = _first_attr(result, "manifest")

    return RunOutcome(run=run, manifest=manifest, raw=result)


# --------------------------------------------------------------------------
# SPIKE 3 -- chat() multimodal input
# --------------------------------------------------------------------------


@runtime_checkable
class VisionCaller(Protocol):
    def __call__(self, **kwargs: Any) -> Any: ...


def build_vision_messages(
    system_prompt: str,
    user_prompt: str,
    image_data_urls: list[str],
) -> list[dict[str, Any]]:
    """Compose an OpenAI-style multimodal message list.

    Resolves docs/SPIKES.md #3. `chat()`'s documented signature accepts
    `messages: list[ChatMessage] | list[dict]`, and every provider Notary
    targets for vision (GMI Cloud's Qwen-VL deployment, OpenAI GPT-4o) speaks
    the OpenAI content-parts format. So the standard shape is emitted directly.

    If a future provider rejects this shape, the `client=` escape hatch on
    `chat()` is the documented way to hand-shape the request -- that is why
    `vision_chat` below threads `client` through rather than hiding it.
    """
    content: list[dict[str, Any]] = [{"type": "text", "text": user_prompt}]
    for url in image_data_urls:
        content.append({"type": "image_url", "image_url": {"url": url}})

    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": content},
    ]


def vision_chat(
    *,
    model: str,
    system_prompt: str,
    user_prompt: str,
    image_data_urls: list[str],
    api_key: str | None = None,
    client: Any = None,
    max_tokens: int = 1600,
    temperature: float = 0.0,
    **kwargs: Any,
) -> Any:
    """Single vision call through genblaze `chat()`.

    temperature=0.0 is deliberate. A compliance verdict that varies run to run
    on identical input is not a compliance verdict.
    """
    if not GENBLAZE_AVAILABLE or chat is None:
        raise RuntimeError(
            "genblaze is not installed; vision review requires live mode."
        )

    return chat(
        model=model,
        messages=build_vision_messages(system_prompt, user_prompt, image_data_urls),
        temperature=temperature,
        max_tokens=max_tokens,
        api_key=api_key,
        client=client,
        retry_on_rate_limit=True,
        **kwargs,
    )


def extract_chat_text(response: Any) -> str:
    """Pull assistant text out of a ChatResponse across plausible shapes."""
    for name in ("text", "content", "message", "output_text"):
        value = getattr(response, name, None)
        if isinstance(value, str) and value.strip():
            return value
    choices = getattr(response, "choices", None)
    if choices:
        first = choices[0]
        message = getattr(first, "message", None) or first
        content = getattr(message, "content", None)
        if isinstance(content, str):
            return content
    if isinstance(response, str):
        return response
    return ""


def extract_token_usage(response: Any) -> tuple[int | None, int | None]:
    """Return (tokens_in, tokens_out).

    `chat()` always reports `cost_usd=None` (documented). Notary derives cost
    from these counts and the model's registry price -- see board/review.py.
    """
    tin = _first_attr(response, "tokens_in", "prompt_tokens", "input_tokens")
    tout = _first_attr(response, "tokens_out", "completion_tokens", "output_tokens")

    if tin is None or tout is None:
        usage = _first_attr(response, "usage")
        if usage is not None:
            tin = tin if tin is not None else _first_attr(
                usage, "prompt_tokens", "input_tokens", "tokens_in"
            )
            tout = tout if tout is not None else _first_attr(
                usage, "completion_tokens", "output_tokens", "tokens_out"
            )

    return (
        int(tin) if isinstance(tin, (int, float)) else None,
        int(tout) if isinstance(tout, (int, float)) else None,
    )


# --------------------------------------------------------------------------
# SPIKE 2 -- cross-provider fallback
# --------------------------------------------------------------------------


def supports_cross_provider_fallback() -> bool:
    """Whether `fallback_models` may name a model on a different provider.

    Resolves docs/SPIKES.md #2, which the SDK docs do not answer:
    `docs/features/retry-policy.md` covers transient retry within the
    submit/poll/fetch lifecycle and says nothing about fallback routing.

    Notary does not depend on the answer. `pipeline/factory.py` configures
    `fallback_models` for same-provider resilience, and `pipeline/runner.py`
    additionally catches a terminal ProviderError and launches an explicitly
    parent-linked run on the second provider. That second path is provider-
    agnostic, so cross-provider failover works either way -- and it is visible
    in the lineage, which the built-in path would not be.
    """
    return bool(GENBLAZE_AVAILABLE)


def provider_error_code(exc: BaseException) -> str:
    code = getattr(exc, "error_code", None)
    if isinstance(code, str) and code:
        return code
    return type(exc).__name__.upper()


def is_provider_failure(exc: BaseException) -> bool:
    """Provider-side fault (retry elsewhere) vs. our fault (do not retry).

    This distinction is the whole of Notary's two-tier failure model: a
    provider fault earns a different provider, a quality fault earns a better
    prompt. Conflating them is the tell of a shallow integration.
    """
    code = provider_error_code(exc)
    return code in {
        "MODEL_ERROR",
        "PROVIDER_ERROR",
        "TIMEOUT",
        "RATE_LIMITED",
        "SERVICE_UNAVAILABLE",
        "UPSTREAM_ERROR",
        "CONNECTION_ERROR",
    }


def runtime_report() -> dict[str, object]:
    return {
        "genblaze_available": GENBLAZE_AVAILABLE,
        "import_error": GENBLAZE_IMPORT_ERROR,
        "cross_provider_fallback": supports_cross_provider_fallback(),
    }
