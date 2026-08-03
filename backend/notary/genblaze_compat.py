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

Mp4Handler: Any = None

# EVERY name below is re-exported for the rest of the codebase and used nowhere
# in this module. Ruff's F401 autofix therefore considers them unused and will
# delete them -- which it did once, silently: GENBLAZE_AVAILABLE stayed True
# while Pipeline, AgentLoop, Modality and the storage classes were left bound
# to None, so live mode failed with "AgentLoop is unavailable" on a machine
# where the SDK was perfectly installed.
#
# `__all__` is what tells ruff these are deliberate re-exports. Do not remove
# it, and do not add an import here without adding it there.
__all__ = [
    "GENBLAZE_AVAILABLE",
    "GENBLAZE_IMPORT_ERROR",
    "S3_AVAILABLE",
    "AgentContext",
    "AgentLoop",
    "Asset",
    "CallableEvaluator",
    "EvaluationResult",
    "Manifest",
    "Modality",
    "ModerationHook",
    "ModerationResult",
    "Mp4Handler",
    "ObjectStorageSink",
    "Pipeline",
    "ProviderError",
    "S3StorageBackend",
    "Step",
    "SyncProvider",
    "CHAT_CONNECTORS",
    "CHAT_PROVIDER",
    "RunOutcome",
    "build_vision_messages",
    "chat",
    "extract_chat_text",
    "extract_token_usage",
    "is_provider_failure",
    "normalize_run_result",
    "provider_error_code",
    "resolve_chat",
    "runtime_report",
    "supported_kwargs",
    "supports_cross_provider_fallback",
    "vision_chat",
]

try:  # pragma: no cover - exercised by environment, not by tests
    # Verified against genblaze-core 0.3.8. Note what is NOT here: `chat` and
    # `Mp4Handler` are not top-level exports, and importing them from
    # genblaze_core raises ImportError -- which would have taken this whole
    # block down and silently disabled the SDK. They are resolved separately
    # below. See docs/SPIKES.md.
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
    )
    from genblaze_core.media import Mp4Handler  # type: ignore[import-not-found]

    GENBLAZE_AVAILABLE = True
except Exception as exc:  # noqa: BLE001 - any import failure degrades to replay
    GENBLAZE_IMPORT_ERROR = f"{type(exc).__name__}: {exc}"
    log.info(
        "genblaze SDK unavailable (%s). Live generation is disabled; "
        "replay mode is fully functional.",
        GENBLAZE_IMPORT_ERROR,
    )

# The S3 sink is a separate distribution. Its absence must not disable
# generation -- Notary can still run and write through its own boto3 client.
S3_AVAILABLE = False
try:  # pragma: no cover
    # These live in two different distributions, and neither is where the
    # obvious guess puts it (verified against genblaze-core 0.3.8):
    #   ObjectStorageSink -> genblaze_core.storage   (NOT genblaze_core.sinks,
    #                                                 NOT genblaze_s3)
    #   S3StorageBackend  -> genblaze_s3
    from genblaze_core.storage import ObjectStorageSink  # type: ignore[import-not-found]
    from genblaze_s3 import S3StorageBackend  # type: ignore[import-not-found]

    S3_AVAILABLE = True
except Exception as exc:  # noqa: BLE001
    log.info("genblaze-s3 sink unavailable (%s); using Notary's own B2 client", exc)


CHAT_CONNECTORS: dict[str, str] = {
    "google": "genblaze_google",
    "gmicloud": "genblaze_gmicloud",
    "openai": "genblaze_openai",
    "nvidia": "genblaze_nvidia",
}
"""Vision-capable connectors, in Notary's default preference order.

Google leads because Google AI Studio's free tier covers Gemini vision, and the
Board reviews *every* take including the rejected ones -- so per-call cost
decides whether the perceptual half can run at all on a hobby budget.
"""


def resolve_chat(preferred: str | None = None) -> tuple[Any, str | None]:
    """Locate a `chat()` callable, optionally from a named provider.

    It is **not** in genblaze_core -- that package exports only the chat
    *types* (ChatMessage, ImageURLContent, ...). The callable is defined once
    per connector, and the signatures differ between them, which is why
    `vision_chat` filters kwargs rather than assuming a shared surface.

    The presence of `ImageURLContent` among the core chat types is what
    confirms multimodal input is a first-class concept rather than something to
    be smuggled through `client=`. Resolves docs/SPIKES.md #3.
    """
    import importlib

    order = list(CHAT_CONNECTORS)
    if preferred and preferred in CHAT_CONNECTORS:
        order.remove(preferred)
        order.insert(0, preferred)

    for name in order:
        module_name = CHAT_CONNECTORS[name]
        try:
            module = importlib.import_module(module_name)
        except ImportError:
            continue
        fn = getattr(module, "chat", None)
        if not callable(fn):
            try:
                fn = getattr(importlib.import_module(f"{module_name}.chat"), "chat", None)
            except ImportError:
                fn = None
        if callable(fn):
            return fn, name

    return None, None


chat, CHAT_PROVIDER = resolve_chat()


def supported_kwargs(fn: Any, candidate: dict[str, Any]) -> dict[str, Any]:
    """Drop arguments the target `chat()` does not declare.

    Connector signatures genuinely differ -- GMI Cloud takes `response_format`
    but not `retry_on_rate_limit`; Google is the reverse. Both accept
    `**kwargs`, so passing an unknown one raises no TypeError: it is forwarded
    to the provider's HTTP layer, where it either does nothing or fails
    somewhere unhelpful. Filtering by the declared signature keeps a
    provider-specific option from silently becoming a wire parameter.
    """
    import inspect

    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):  # pragma: no cover - builtins
        return candidate

    return {k: v for k, v in candidate.items() if k in params}


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
    provider: str | None = None,
    max_tokens: int = 1600,
    temperature: float = 0.0,
    **kwargs: Any,
) -> Any:
    """Single vision call through a genblaze connector's `chat()`.

    `provider` selects the connector ("google", "gmicloud", "openai",
    "nvidia"); omitted, the default preference order applies.

    temperature=0.0 is deliberate. A compliance verdict that varies run to run
    on identical input is not a compliance verdict.
    """
    fn, resolved = (chat, CHAT_PROVIDER) if provider is None else resolve_chat(provider)

    if not GENBLAZE_AVAILABLE or fn is None:
        raise RuntimeError(
            "No genblaze chat connector is installed. Vision review needs one "
            "of: genblaze-google, genblaze-gmicloud, genblaze-openai, "
            "genblaze-nvidia."
        )
    if provider and resolved != provider:
        log.warning(
            "requested vision provider %r is not installed; using %r",
            provider,
            resolved,
        )

    # Connector signatures differ, so options are offered and filtered rather
    # than assumed. Verified:
    #   gmicloud  has response_format, no retry_on_rate_limit
    #   google    has retry_on_rate_limit, no response_format
    # Both declare **kwargs, so an unsupported option would not raise -- it
    # would be forwarded to the provider's HTTP layer and fail obscurely.
    optional = supported_kwargs(
        fn,
        {
            # Constrain output to JSON where the provider can guarantee it. An
            # unparseable verdict already escalates safely, but escalating for
            # a formatting slip wastes a reviewer's attention.
            "response_format": {"type": "json_object"},
            "retry_on_rate_limit": True,
            "client": client,
        },
    )

    return fn(
        model=model,
        messages=build_vision_messages(system_prompt, user_prompt, image_data_urls),
        temperature=temperature,
        max_tokens=max_tokens,
        api_key=api_key,
        **optional,
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
