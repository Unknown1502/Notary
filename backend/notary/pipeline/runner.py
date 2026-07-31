"""Orchestration: brief in, sealed certificate or human escalation out.

The whole product in one function signature. What happens between:

    1. Deterministic screen of the brief          (before spending money)
    2. AgentLoop:  generate -> review -> revise    (Genblaze drives the loop)
         each iteration:  chained image->video render
                          extract keyframes
                          deterministic screen of the output
                          perceptual review as a manifest step
                          decide: VERIFIED | REJECTED | ESCALATED
    3. VERIFIED   -> promote into the Object-Locked vault, sign, seal
       ESCALATED  -> human queue; nothing ships
       REJECTED with budget left -> loop continues with the verdict as guidance

The two-tier failure model, kept apart on purpose
-------------------------------------------------
    Provider failure (MODEL_ERROR, stall, timeout)
        -> retry on another model/provider. `fallback_models` handles the
           same-provider case inside the SDK; `_run_with_failover` handles the
           cross-provider case with an explicitly parent-linked run, so the
           switch is visible in the lineage instead of hidden inside a retry.

    Quality failure (Board REJECTED)
        -> a verdict-conditioned revision. Never a provider switch. A different
           model renders a non-compliant brief just as non-compliantly; the
           defect is in the prompt, so the prompt is what changes.

Collapsing these two into one retry path is the single most common way an
orchestration integration reads as shallow. They are different faults with
different remedies and they are logged, streamed, and rendered differently.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import tempfile
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..board import BoardEvaluator, BoardReviewProvider, BrandGuardrailHook, decide
from ..board.review import run_vision_review
from ..config import RunMode, Settings, get_settings
from ..events import Event, EventType, Recorder, get_bus
from ..genblaze_compat import (
    GENBLAZE_AVAILABLE,
    AgentLoop,
    CallableEvaluator,
    is_provider_failure,
    normalize_run_result,
    provider_error_code,
)
from ..media import extract_frames, make_thumbnail, probe
from ..models import (
    BoardDecision,
    BoardVerdict,
    CampaignBrief,
    ReviewSession,
    Take,
    TakeStatus,
)
from ..provenance import (
    EmbeddingError,
    build_certificate,
    build_verdict_document,
    certificate_document,
    embed_bytes,
    hash_payload,
    load_or_create,
    verify_bytes,
)
from ..provenance.signing import SigningIdentity, SigningUnavailable
from ..storage import (
    get_storage,
    vault_asset_key,
    vault_certificate_key,
    vault_manifest_key,
    vault_thumbnail_key,
    vault_verdict_key,
    workbench_frame_key,
    workbench_take_key,
)
from ..store import get_store
from .factory import build_pipeline, make_sink, resolve_providers

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class _SealedMedia:
    """The artifact actually written to the Object-Locked vault."""

    sha256: str
    """Digest of the sealed bytes, manifest box included. This is what a
    downloader who hashes the file they fetched will compute."""

    url: str
    size: int
    embedded_by: str
    """'notary', 'genblaze', or 'none'. Surfaced on the certificate so it
    never claims an embedding that did not happen."""


class ReviewRunner:
    """Drives one campaign brief through the Board to a terminal state."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.bus = get_bus()
        self.store = get_store()
        self.storage = get_storage()

    # ------------------------------------------------------------ public API

    async def run(
        self, brief: CampaignBrief, *, session_id: str | None = None
    ) -> ReviewSession:
        """Drive a brief to a terminal state.

        `session_id` is supplied by the caller so the HTTP handler can return a
        stream URL *before* the run begins. Letting the runner mint its own id
        would hand the client a stream that never emits.
        """
        session = ReviewSession(brief=brief)
        if session_id:
            session.session_id = session_id
        self.store.put_session(session)

        recorder: Recorder | None = None
        if self.settings.mode is RunMode.LIVE:
            recorder = Recorder(
                self.settings.seed_dir, session.session_id, title=brief.title
            )

        await self._emit(
            session, recorder, EventType.SESSION_STARTED,
            campaign_id=brief.campaign_id,
            title=brief.title,
            tenant=brief.tenant,
            compliance_profile=brief.compliance_profile,
            max_iterations=self.settings.max_board_iterations,
            mode=self.settings.mode.value,
        )

        try:
            await self._execute(session, recorder)
        except Exception as exc:  # noqa: BLE001
            log.exception("session %s failed", session.session_id)
            session.status = TakeStatus.FAILED
            session.finished_at = datetime.now(UTC)
            await self._emit(
                session, recorder, EventType.SESSION_FAILED,
                error=str(exc), error_type=type(exc).__name__,
            )

        return session

    # -------------------------------------------------------------- internals

    async def _execute(self, session: ReviewSession, recorder: Recorder | None) -> None:
        brief = session.brief
        hook = BrandGuardrailHook(brief)

        # --- Stage 1: screen the brief before spending anything -------------
        await self._emit(session, recorder, EventType.BOARD_CONVENED, stage="prompt")
        prompt_result = hook.check_prompt(brief.prompt)

        if not getattr(prompt_result, "allowed", True):
            verdict = decide(
                hook.verdicts, [],
                run_id=session.session_id, take_number=0, iterations_remaining=0,
            )
            for criterion in verdict.criteria:
                await self._emit_criterion(session, recorder, criterion)

            session.status = TakeStatus.REJECTED
            session.finished_at = datetime.now(UTC)
            await self._emit(
                session, recorder, EventType.BOARD_VERDICT,
                decision=verdict.decision.value,
                summary=verdict.summary,
                stage="prompt",
                take_number=0,
            )
            await self._emit(
                session, recorder, EventType.SESSION_COMPLETED,
                outcome="rejected_before_generation",
                saved_a_render=True,
                summary=(
                    "The brief itself fails compliance screening. Rejected "
                    "before any provider was billed."
                ),
            )
            return

        # --- Stage 2: the AgentLoop ----------------------------------------
        if not (GENBLAZE_AVAILABLE and self.settings.generates_for_real):
            raise RuntimeError(
                "Live generation requires the genblaze SDK and NOTARY_MODE=live. "
                "Use the replay endpoints for a credential-free demo."
            )

        providers = resolve_providers(self.settings)
        sink = make_sink(self.settings)

        evaluator = BoardEvaluator(
            review_fn=lambda result, iteration: self._review_iteration(
                session, recorder, hook, result, iteration
            ),
            max_iterations=self.settings.max_board_iterations,
        )

        def pipeline_factory(ctx: Any = None) -> Any:
            # AgentContext is a dataclass with exactly three fields (verified
            # against genblaze-core 0.3.8):
            #     iteration: int
            #     prior_results: list[PipelineResult]
            #     last_evaluation: EvaluationResult | None
            #
            # The guidance text lives on `.last_evaluation.feedback`, not on
            # the context itself. Reading `last_evaluation` directly would
            # stringify the dataclass repr into the prompt.
            iteration = int(getattr(ctx, "iteration", len(session.takes) + 1) or 1)
            last = getattr(ctx, "last_evaluation", None)
            guidance = getattr(last, "feedback", None) if last is not None else None
            if iteration > 1:
                asyncio.run_coroutine_threadsafe(
                    self._emit(
                        session, recorder, EventType.REVISION_STARTED,
                        take_number=iteration,
                        guidance=str(guidance or "")[:2000],
                    ),
                    self._loop,
                )
            return build_pipeline(
                session.brief, providers, self.settings,
                iteration=iteration,
                guidance=str(guidance) if guidance else None,
                sink=sink,
                moderation_hook=hook,
            )

        self._loop = asyncio.get_running_loop()

        await self._emit(
            session, recorder, EventType.TAKE_STARTED,
            take_number=1,
            image_model=self.settings.image_model,
            video_model=self.settings.video_model,
            fallback_model=self.settings.video_fallback_model,
        )

        await asyncio.to_thread(
            self._drive_agent_loop, session, pipeline_factory, evaluator, sink
        )

        # --- Stage 3: terminal routing --------------------------------------
        final = evaluator.latest
        if final is None:
            raise RuntimeError("the review loop produced no verdict")

        if final.decision is BoardDecision.VERIFIED:
            await self._certify(session, recorder, final)
        else:
            reason = (
                final.summary
                if final.decision is BoardDecision.ESCALATED
                else f"Rejected after {evaluator.iterations_used} attempt(s): {final.summary}"
            )
            session.status = TakeStatus.ESCALATED
            session.finished_at = datetime.now(UTC)
            if take := session.current_take:
                take.status = TakeStatus.ESCALATED

            self.store.enqueue(session.session_id, reason)
            await self._emit(
                session, recorder, EventType.ESCALATED,
                reason=reason,
                take_number=final.take_number,
                queue_depth=self.store.queue_depth(),
            )
            await self._emit(
                session, recorder, EventType.SESSION_COMPLETED,
                outcome="escalated",
                summary=(
                    "The Board could not clear this take with confidence. It is "
                    "waiting for a human reviewer. Nothing was published."
                ),
            )

    def _drive_agent_loop(
        self, session: ReviewSession, factory: Any, evaluator: BoardEvaluator, sink: Any
    ) -> None:
        """Run genblaze AgentLoop on a worker thread.

        The SDK is synchronous and a render blocks for minutes, so it cannot run
        on the event loop -- doing so would freeze every SSE stream in the
        process, including this run's own.
        """
        if AgentLoop is None or CallableEvaluator is None:
            raise RuntimeError("genblaze AgentLoop is unavailable")

        loop = AgentLoop(
            factory,
            CallableEvaluator(evaluator),
            max_iterations=self.settings.max_board_iterations,
        )

        try:
            result = loop.run(sink=sink, timeout=self.settings.step_timeout_seconds)
        except TypeError:
            result = loop.run()
        except Exception as exc:  # noqa: BLE001
            if not is_provider_failure(exc):
                raise
            log.warning(
                "AgentLoop terminated on a provider fault (%s); "
                "attempting cross-provider failover",
                provider_error_code(exc),
            )
            result = self._run_with_failover(session, factory, sink, exc)

        session.total_cost_usd = float(
            getattr(result, "total_cost_usd", 0.0)
            or getattr(result, "cost_usd", 0.0)
            or 0.0
        )

    def _run_with_failover(
        self, session: ReviewSession, factory: Any, sink: Any, original: BaseException
    ) -> Any:
        """Cross-provider failover as an explicitly parent-linked run.

        `fallback_models` covers the same-provider case inside the SDK. Whether
        it can also name a model on a *different* provider is undocumented
        (docs/SPIKES.md #2), so Notary does not depend on the answer: on a
        terminal provider fault it launches a fresh run against the secondary
        provider and links it to the failed parent.

        Doing it this way is arguably better than relying on the built-in path
        regardless, because the provider switch becomes a visible edge in the
        lineage graph rather than an invisible retry.
        """
        take = session.current_take
        if take:
            take.used_fallback = True
            take.fallback_reason = (
                f"{provider_error_code(original)}: primary provider failed"
            )

        asyncio.run_coroutine_threadsafe(
            self._emit(
                session, None, EventType.FALLBACK_FIRED,
                from_model=self.settings.video_model,
                to_model=self.settings.video_fallback_model,
                error_code=provider_error_code(original),
                detail=str(original)[:500],
            ),
            self._loop,
        )

        pipeline = factory(None)
        return pipeline.run(sink=sink, timeout=self.settings.step_timeout_seconds)

    # ------------------------------------------------------------ the review

    def _review_iteration(
        self,
        session: ReviewSession,
        recorder: Recorder | None,
        hook: BrandGuardrailHook,
        result: Any,
        iteration: int,
    ) -> BoardVerdict:
        """Review one generated take. Called by the evaluator on a worker thread."""
        started = time.perf_counter()
        outcome = normalize_run_result(result)
        run_id = outcome.run_id or f"{session.session_id}-take{iteration}"

        take = Take(
            run_id=run_id,
            parent_run_id=outcome.parent_run_id,
            take_number=iteration,
            prompt=session.brief.prompt,
            status=TakeStatus.REVIEWING,
            image_model=self.settings.image_model,
            video_model=self.settings.video_model,
            image_provider="gmicloud",
            video_provider="gmicloud",
        )
        session.takes.append(take)

        self._publish_threadsafe(
            session, recorder, EventType.STEP_COMPLETED,
            take_number=iteration, run_id=run_id, stage="generation",
        )

        with tempfile.TemporaryDirectory(prefix="notary-") as tmp:
            workdir = Path(tmp)
            asset_path = self._materialize_asset(outcome, workdir)

            info = probe(asset_path) if asset_path else None
            duration = info.duration_seconds if info else None

            frames: list[Path] = []
            if asset_path:
                frames = extract_frames(asset_path, workdir / "frames", count=5)
                take.sha256 = self._sha256_file(asset_path)
                take.duration_seconds = duration

            frame_keys = [
                workbench_frame_key(session.brief.tenant, run_id, iteration, i)
                for i in range(len(frames))
            ]
            take.frame_keys = frame_keys

            self._upload_workbench(session, take, asset_path, frames, frame_keys)

            # --- deterministic half ---------------------------------------
            hook.attach_frames(frames, frame_keys, duration)
            self._publish_threadsafe(
                session, recorder, EventType.BOARD_CONVENED,
                stage="output", take_number=iteration, frames=len(frames),
            )
            hook.check_output()
            deterministic = [*hook.verdicts, *hook.unmeasured()]
            for criterion in deterministic:
                self._publish_criterion_threadsafe(session, recorder, criterion)

            # --- perceptual half ------------------------------------------
            perceptual = []
            vision_meta: dict[str, Any] = {}
            if frames:
                review = run_vision_review(
                    session.brief, frames,
                    model=self.settings.board_vision_model,
                    api_key=self.settings.gmicloud_api_key,
                )
                perceptual = review.criteria
                vision_meta = {
                    "vision_model": review.model,
                    "vision_provider": "gmicloud",
                    "tokens_in": review.tokens_in,
                    "tokens_out": review.tokens_out,
                    "estimated_cost_usd": review.cost_usd,
                }
                for criterion in perceptual:
                    self._publish_criterion_threadsafe(session, recorder, criterion)

        remaining = max(0, self.settings.max_board_iterations - iteration)
        verdict = decide(
            deterministic, perceptual,
            run_id=run_id, take_number=iteration, iterations_remaining=remaining,
        )
        verdict.frames_reviewed = frame_keys
        for key, value in vision_meta.items():
            setattr(verdict, key, value)

        take.verdict = verdict
        take.status = (
            TakeStatus.CERTIFIED
            if verdict.decision is BoardDecision.VERIFIED
            else TakeStatus.REJECTED
            if verdict.decision is BoardDecision.REJECTED
            else TakeStatus.ESCALATED
        )
        take.cost_usd = vision_meta.get("estimated_cost_usd") or 0.0

        self._publish_threadsafe(
            session, recorder, EventType.BOARD_VERDICT,
            take_number=iteration,
            decision=verdict.decision.value,
            summary=verdict.summary,
            blocking_failures=[c.criterion.value for c in verdict.blocking_failures],
            iterations_remaining=remaining,
            elapsed_seconds=round(time.perf_counter() - started, 2),
            asset_url=take.asset_url,
        )
        return verdict

    # ------------------------------------------------------------ certifying

    async def _certify(
        self, session: ReviewSession, recorder: Recorder | None, verdict: BoardVerdict
    ) -> None:
        take = session.current_take
        if take is None or take.sha256 is None:
            raise RuntimeError("cannot certify without a hashed asset")
        if not take.asset_key:
            # Reached when the workbench upload failed earlier. Fail with a
            # diagnosis rather than a TypeError three frames deeper in boto3.
            raise RuntimeError(
                f"take {take.take_id} has no workbench object to promote; "
                "the upload failed earlier in the run and there is nothing to "
                "seal. Check B2 credentials and the workbench bucket."
            )

        brief = session.brief
        asset_id = take.take_id

        await self._emit(
            session, recorder, EventType.CERTIFICATION_STARTED,
            asset_id=asset_id, retention_days=self.settings.vault_retention_days,
        )

        asset_key = vault_asset_key(brief.tenant, brief.campaign_id, asset_id)
        manifest_key = vault_manifest_key(brief.tenant, brief.campaign_id, asset_id)
        verdict_key = vault_verdict_key(brief.tenant, brief.campaign_id, asset_id)
        cert_key = vault_certificate_key(brief.tenant, brief.campaign_id, asset_id)
        thumb_key = vault_thumbnail_key(brief.tenant, brief.campaign_id, asset_id)

        retention = self.settings.vault_retention_days

        # The manifest is built first because it has to be embedded into the
        # media *before* the media is hashed and sealed. Embedding changes the
        # bytes, so hashing first would certify a digest that does not match
        # the object anyone downloads.
        #
        # `asset_sha256` commits to the media excluding the manifest box, which
        # is what makes an embedded manifest able to describe its own file
        # without circularity. See provenance/embedding.py.
        manifest_payload = {
            "schema": "notary.manifest-index/v1",
            "run_id": take.run_id,
            "parent_run_id": take.parent_run_id,
            "asset_sha256": take.sha256,
            "provider": take.video_provider,
            "model": take.video_model,
            "prompt": take.prompt,
            "verdict_digest": hash_payload(verdict.model_dump(mode="json")),
            "lineage": session.lineage(),
        }
        manifest_hash = hash_payload(manifest_payload)

        sealed = await asyncio.to_thread(
            self._seal_media, take.asset_key, asset_key, manifest_payload, retention
        )

        # take.sha256 now refers to the sealed artifact -- the exact bytes B2
        # serves -- so a downloader who hashes what they fetched matches the
        # certificate. The pre-embed media digest lives on in the manifest.
        take.sha256 = sealed.sha256
        identity = self._signing_identity()

        await asyncio.to_thread(
            self.storage.put_json,
            self.settings.b2_bucket_vault, manifest_key, manifest_payload,
            retention_days=retention,
        )

        verdict_doc = build_verdict_document(verdict, brief, take)
        await asyncio.to_thread(
            self.storage.put_json,
            self.settings.b2_bucket_vault, verdict_key, verdict_doc,
            retention_days=retention,
        )

        certificate = build_certificate(
            session=session, take=take, verdict=verdict,
            asset_key=asset_key, asset_url=sealed.url,
            manifest_key=manifest_key, verdict_key=verdict_key,
            manifest_hash=manifest_hash,
            thumbnail_url=take.thumbnail_url,
            retention_days=retention,
            identity=identity,
            require_signing=self.settings.require_signing,
        )

        await asyncio.to_thread(
            self.storage.put_json,
            self.settings.b2_bucket_vault, cert_key,
            certificate_document(certificate),
            retention_days=retention,
        )

        self.store.put_certificate(certificate)
        session.certificate_id = certificate.certificate_id
        session.status = TakeStatus.CERTIFIED
        session.finished_at = datetime.now(UTC)
        take.status = TakeStatus.CERTIFIED

        await self._emit(
            session, recorder, EventType.CERTIFICATION_SEALED,
            certificate_id=certificate.certificate_id,
            asset_url=certificate.asset_url,
            sha256=certificate.sha256,
            manifest_hash=manifest_hash,
            trust_mode=certificate.trust_mode,
            manifest_embedded=sealed.embedded_by != "none",
            embedded_by=sealed.embedded_by,
            signature_key_id=(
                certificate.signature.key_id if certificate.signature else None
            ),
            retention_until=certificate.retention_until.isoformat(),
            object_lock_mode=certificate.object_lock_mode,
            vault_prefix=asset_key.rsplit("/", 1)[0],
        )
        await self._emit(
            session, recorder, EventType.SESSION_COMPLETED,
            outcome="certified",
            certificate_id=certificate.certificate_id,
            summary=(
                f"Certified and sealed under Object Lock until "
                f"{certificate.retention_until:%Y-%m-%d}."
            ),
        )

    def _seal_media(
        self,
        source_key: str,
        dest_key: str,
        manifest_payload: dict[str, Any],
        retention_days: int,
    ) -> _SealedMedia:
        """Embed the manifest, then write the result into the locked vault.

        Read-then-write rather than a server-side copy, for two reasons that
        both matter: the manifest has to be injected between the read and the
        write, and hashing the outgoing buffer means the digest Notary
        certifies is computed over the exact bytes it seals. A hash produced
        at any other layer would leave a gap between what was measured and
        what was stored.
        """
        body = self.storage.get_bytes(self.settings.b2_bucket_workbench, source_key)

        embedded_by = "none"
        try:
            body = embed_bytes(body, manifest_payload)
            embedded_by = "notary"
        except EmbeddingError as exc:
            # A provider that returned something other than a parseable MP4.
            # The sidecar manifest.json still ships, so provenance survives --
            # but the certificate must not claim an embedding that is absent.
            log.warning("could not embed manifest into the media: %s", exc)

        digest = hashlib.sha256(body).hexdigest()

        stored = self.storage.put(
            self.settings.b2_bucket_vault,
            dest_key,
            body,
            content_type="video/mp4",
            retention_days=retention_days,
            cache_control="public, max-age=31536000, immutable",
        )

        if embedded_by == "notary":
            passed, detail = verify_bytes(body)
            if not passed:  # pragma: no cover - would indicate a logic error
                raise RuntimeError(f"embedded manifest failed self-check: {detail}")
            log.info("manifest embedded and self-verified: %s", detail)

        return _SealedMedia(
            sha256=digest, url=stored.url, size=len(body), embedded_by=embedded_by
        )

    def _signing_identity(self) -> SigningIdentity | None:
        try:
            return load_or_create(
                self.settings.signing_key_path, self.settings.signing_key_id
            )
        except SigningUnavailable as exc:
            if self.settings.require_signing:
                raise
            log.warning("proceeding without a signature: %s", exc)
            return None

    # --------------------------------------------------------------- helpers

    def _materialize_asset(self, outcome: Any, workdir: Path) -> Path | None:
        """Fetch the generated video to local disk for probing and hashing."""
        url = None
        for step in reversed(outcome.steps):
            assets = getattr(step, "assets", None) or []
            for asset in assets:
                candidate = getattr(asset, "url", None) or getattr(asset, "uri", None)
                if candidate and not str(candidate).startswith("text:"):
                    url = candidate
                    break
            if url:
                break

        if not url:
            log.error("run produced no downloadable asset")
            return None

        target = workdir / "asset.mp4"
        try:
            if str(url).startswith("http"):
                import httpx

                with httpx.stream("GET", str(url), timeout=120.0, follow_redirects=True) as r:
                    r.raise_for_status()
                    with target.open("wb") as fh:
                        for chunk in r.iter_bytes(1 << 20):
                            fh.write(chunk)
            else:
                source = Path(str(url))
                if source.exists():
                    target.write_bytes(source.read_bytes())
                else:
                    return None
        except Exception as exc:  # noqa: BLE001
            log.error("could not materialize asset from %s: %s", url, exc)
            return None

        return target if target.exists() and target.stat().st_size else None

    @staticmethod
    def _sha256_file(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def _upload_workbench(
        self,
        session: ReviewSession,
        take: Take,
        asset_path: Path | None,
        frames: list[Path],
        frame_keys: list[str],
    ) -> None:
        if not self.storage.available or asset_path is None:
            return

        bucket = self.settings.b2_bucket_workbench
        key = workbench_take_key(session.brief.tenant, take.run_id, take.take_number)

        try:
            stored = self.storage.put(
                bucket, key, asset_path.read_bytes(), content_type="video/mp4"
            )
            take.asset_key = key
            take.asset_url = stored.url

            thumb = make_thumbnail(asset_path, asset_path.parent / "thumb.jpg")
            if thumb:
                thumb_key = key.rsplit(".", 1)[0] + "-thumb.jpg"
                take.thumbnail_url = self.storage.put(
                    bucket, thumb_key, thumb.read_bytes(), content_type="image/jpeg"
                ).url

            for path, frame_key in zip(frames, frame_keys, strict=False):
                self.storage.put(
                    bucket, frame_key, path.read_bytes(), content_type="image/jpeg"
                )
        except Exception as exc:  # noqa: BLE001
            log.warning("workbench upload failed: %s", exc)

    # ---------------------------------------------------------------- events

    async def _emit(
        self,
        session: ReviewSession,
        recorder: Recorder | None,
        event_type: EventType,
        **data: Any,
    ) -> Event:
        event = await self.bus.publish(session.session_id, event_type, **data)
        if recorder is not None:
            recorder.write(event)
        return event

    async def _emit_criterion(
        self, session: ReviewSession, recorder: Recorder | None, criterion: Any
    ) -> None:
        await self._emit(
            session, recorder, EventType.BOARD_CRITERION,
            criterion=criterion.criterion.value,
            outcome=criterion.outcome.value,
            kind=criterion.kind.value,
            severity=criterion.severity.value,
            rationale=criterion.rationale,
            confidence=criterion.confidence,
            measurement=criterion.measurement,
            evidence_frame=criterion.evidence_frame,
        )

    def _publish_threadsafe(
        self,
        session: ReviewSession,
        recorder: Recorder | None,
        event_type: EventType,
        **data: Any,
    ) -> None:
        asyncio.run_coroutine_threadsafe(
            self._emit(session, recorder, event_type, **data), self._loop
        )

    def _publish_criterion_threadsafe(
        self, session: ReviewSession, recorder: Recorder | None, criterion: Any
    ) -> None:
        asyncio.run_coroutine_threadsafe(
            self._emit_criterion(session, recorder, criterion), self._loop
        )


async def run_review(brief: CampaignBrief) -> ReviewSession:
    return await ReviewRunner().run(brief)
