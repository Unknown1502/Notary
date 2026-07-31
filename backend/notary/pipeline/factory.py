"""Building the generation pipeline for one iteration.

This is the function `AgentLoop` calls on every attempt. It receives an
`AgentContext` carrying the iteration number and the previous evaluation's
feedback, and returns a fresh `Pipeline`.

The chained shape is the canonical Genblaze idiom:

    storyboard image  ->  (chain=True)  ->  short video

Chaining rather than two independent runs matters for a reason beyond
tidiness: the video step consumes the image step's asset, so the manifest
records an actual dependency between them. The provenance record then shows
*which keyframe* produced *which video*, which is precisely the question a
compliance auditor asks when a frame is challenged.

Verdict-conditioned revision
----------------------------
The whole point of the loop is that iteration N+1 is not a reroll of iteration
N. `ctx.feedback` carries the Board's written rationale for the specific
criteria that failed, and `compose_prompt` folds it into the prompt as explicit
correction instructions. A rejection for an off-palette render produces a
revision that names the hex values; a rejection for a missing disclosure
produces one that carries the disclosure text.

That is what makes the before/after causal instead of lucky — and it is also
why a quality failure must never be "fixed" by switching providers. A different
model renders the same non-compliant brief just as non-compliantly.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from ..config import Settings
from ..genblaze_compat import GENBLAZE_AVAILABLE, Modality, Pipeline
from ..models import CampaignBrief

log = logging.getLogger(__name__)


@dataclass
class ProviderBundle:
    """The provider instances one run needs, resolved once."""

    image: Any
    video: Any
    video_fallbacks: list[str]
    image_provider_name: str
    video_provider_name: str


def resolve_providers(settings: Settings) -> ProviderBundle:
    """Instantiate providers from the installed genblaze provider packages.

    Imported lazily and by name so that a missing optional provider package
    degrades to "no fallback available" rather than breaking import of the
    whole application.
    """
    if not GENBLAZE_AVAILABLE:
        raise RuntimeError("genblaze is not installed; live generation unavailable")

    image_provider: Any = None
    video_provider: Any = None

    try:
        from genblaze_gmicloud import (  # type: ignore[import-not-found]
            GMICloudImageProvider,
            GMICloudVideoProvider,
        )

        image_provider = GMICloudImageProvider(api_key=settings.gmicloud_api_key)
        video_provider = GMICloudVideoProvider(api_key=settings.gmicloud_api_key)
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(
            f"GMI Cloud provider unavailable: {exc}. Install genblaze-gmicloud "
            "and set NOTARY_GMICLOUD_API_KEY."
        ) from exc

    fallbacks: list[str] = []
    if settings.video_fallback_model:
        fallbacks.append(settings.video_fallback_model)

    return ProviderBundle(
        image=image_provider,
        video=video_provider,
        video_fallbacks=fallbacks,
        image_provider_name="gmicloud",
        video_provider_name="gmicloud",
    )


def compose_storyboard_prompt(brief: CampaignBrief, guidance: str | None) -> str:
    """Prompt for the keyframe image step."""
    parts = [
        f"Advertising keyframe for {brief.brand_kit.name}.",
        brief.prompt,
    ]

    if brief.brand_kit.palette:
        parts.append(
            "Strictly use this brand color palette: "
            + ", ".join(brief.brand_kit.palette)
            + ". These exact hues must dominate the chromatic content of the frame."
        )
    if brief.brand_kit.tone_guidance:
        parts.append(f"Tone: {brief.brand_kit.tone_guidance}")
    if brief.brand_kit.logo_uri:
        parts.append(
            "Leave clean, uncluttered negative space in a lower corner for a "
            "brand logo lockup."
        )

    parts.append(
        f"Composition must be {brief.channel.aspect_ratio}. "
        "Photographic, professionally lit, no text overlays, no watermarks."
    )

    if guidance:
        parts.append("\n" + guidance)

    return "\n".join(parts)


def compose_video_prompt(brief: CampaignBrief, guidance: str | None) -> str:
    parts = [
        brief.prompt,
        f"Animate as a {brief.channel.duration_seconds}-second "
        f"{brief.channel.aspect_ratio} advertisement.",
        "Subtle, controlled camera motion. Preserve the source frame's "
        "composition, palette, and any branding.",
        "No morphing faces, no warped hands, no drifting text.",
    ]

    if brief.brand_kit.palette:
        parts.append("Maintain the brand palette: " + ", ".join(brief.brand_kit.palette))

    if guidance:
        parts.append("\n" + guidance)

    return "\n".join(parts)


def build_pipeline(
    brief: CampaignBrief,
    providers: ProviderBundle,
    settings: Settings,
    *,
    iteration: int = 1,
    guidance: str | None = None,
    sink: Any = None,
    moderation_hook: Any = None,
    previous_result: Any = None,
) -> Any:
    """Construct the chained image -> video pipeline for one iteration.

    When `previous_result` is supplied, `from_result()` links this run to its
    parent so the manifest carries `parent_run_id`. AgentLoop does this
    automatically between its own iterations; the explicit path exists for the
    cross-provider failover in runner.py, which launches a linked run outside
    the loop and must not lose the lineage.
    """
    name = f"notary-{brief.campaign_id}-take{iteration}"

    # `moderation` is a CONSTRUCTOR keyword, not a builder method. Verified
    # against genblaze-core 0.3.8:
    #   Pipeline(name, tenant_id=None, *, project_id=None, chain=False,
    #            structured_log=False, max_concurrency=None,
    #            moderation: ModerationHook | None = None, ...)
    #
    # An earlier version of this file tried `pipeline.moderation(hook)` through
    # a tolerant helper, which found no such method and silently continued --
    # meaning the deterministic screen never attached and its findings never
    # reached step metadata. The review still ran, so nothing looked broken;
    # only the manifest-integration claim was quietly false.
    pipeline = Pipeline(name, chain=True, moderation=moderation_hook)

    pipeline = pipeline.step(
        providers.image,
        model=settings.image_model,
        prompt=compose_storyboard_prompt(brief, guidance),
        modality=Modality.IMAGE,
        aspect_ratio=brief.channel.aspect_ratio,
    )

    video_kwargs: dict[str, Any] = {
        "model": settings.video_model,
        "prompt": compose_video_prompt(brief, guidance),
        "modality": Modality.VIDEO,
        "duration": brief.channel.duration_seconds,
        "aspect_ratio": brief.channel.aspect_ratio,
    }
    if providers.video_fallbacks:
        # Tier 1 of the failure model: a provider-side fault (MODEL_ERROR,
        # stall, timeout) retries on another model. This never fires for a
        # quality rejection -- that is tier 2, and it is a different remedy.
        video_kwargs["fallback_models"] = providers.video_fallbacks

    pipeline = pipeline.step(providers.video, **video_kwargs)

    if previous_result is not None:
        pipeline = _apply(pipeline, "from_result", previous_result)

    return pipeline


def _apply(pipeline: Any, method: str, argument: Any) -> Any:
    """Call an optional builder method, tolerating SDK surface variation.

    Chainable builders may return a new pipeline or mutate in place; both are
    handled. If the method is absent the pipeline is returned unchanged and the
    caller degrades — e.g. no hook attachment means the runner falls back to
    invoking the moderation checks directly, which it already supports.
    """
    fn = getattr(pipeline, method, None)
    if fn is None:
        log.info("Pipeline has no .%s(); continuing without it", method)
        return pipeline
    try:
        result = fn(argument)
    except TypeError as exc:
        log.warning("Pipeline.%s() rejected its argument (%s)", method, exc)
        return pipeline
    return result if result is not None else pipeline


def make_sink(settings: Settings) -> Any:
    """Genblaze ObjectStorageSink writing to the B2 workbench bucket.

    Generated assets land in the workbench first, always. Nothing is written to
    the Object-Locked vault until the Board has cleared it — because a vault
    write is irreversible, and writing drafts there would permanently store
    every rejected take at full retention.
    """
    if not GENBLAZE_AVAILABLE:
        return None

    from ..genblaze_compat import S3_AVAILABLE, ObjectStorageSink, S3StorageBackend

    if not S3_AVAILABLE:
        # Notary writes through its own boto3 client anyway, so a missing sink
        # costs streaming persistence during the run, not correctness.
        log.info("genblaze-s3 not installed; running without a pipeline sink")
        return None

    # Exact signature (verified against genblaze-s3):
    #   for_backblaze(bucket=None, *, region=None, key_id=None, app_key=None,
    #                 public_url_base=None, auto_lifecycle=False,
    #                 preflight=True)
    # Note `app_key`, not `application_key`, and there is no `endpoint`
    # parameter -- the endpoint is derived from the region.
    # `for_backblaze` runs a preflight HeadBucket by default, so this is a
    # network call that raises StorageError on bad credentials, a wrong region,
    # or a missing bucket. Failing the whole run for it would be wrong:
    # certification writes through Notary's own boto3 client, so the sink only
    # adds streaming persistence during generation. Degrade loudly instead.
    try:
        backend = S3StorageBackend.for_backblaze(
            bucket=settings.b2_bucket_workbench,
            region=settings.b2_region,
            key_id=settings.b2_key_id,
            app_key=settings.b2_application_key,
            public_url_base=settings.b2_public_vault_base,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "genblaze sink preflight failed (%s). Continuing without it; "
            "assets are still persisted by Notary's own B2 client. Check "
            "NOTARY_B2_REGION and that the workbench bucket exists.",
            exc,
        )
        return None

    return ObjectStorageSink(backend)
