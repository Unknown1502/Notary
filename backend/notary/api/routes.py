"""HTTP surface.

Route design follows one rule: **a judge or reviewer must reach every hero
moment without credentials.** So the demo endpoints (`/api/demo/*`) are
first-class rather than an afterthought, they use the same SSE contract and the
same frontend code path as live runs, and they label themselves as replays.

Live generation is available at the same endpoints when NOTARY_MODE=live.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Any

import httpx
from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, Request
from fastapi.responses import FileResponse, RedirectResponse
from sse_starlette.sse import EventSourceResponse

from ..board.rubric import describe_profiles
from ..config import RunMode, describe_runtime, get_settings
from ..events import get_bus, list_recordings, load_recording, replay_into_bus
from ..genblaze_compat import runtime_report
from ..media import tooling_available
from ..models import CampaignBrief, HumanSignoff
from ..pipeline.runner import ReviewRunner
from ..provenance import summarize, verify_certificate
from ..storage import describe_layout, get_storage
from ..store import get_store
from .schemas import (
    ReplayRequest,
    ReplayResponse,
    SignoffRequest,
    SubmitBriefRequest,
    SubmitBriefResponse,
)

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api")


# --------------------------------------------------------------------------
# Serving media the certificate points at
#
# A certificate's `asset_url` and `thumbnail_url` are sealed under Object Lock,
# so they record where a copy lived at the moment of certification and can
# never be amended. Neither is safe to hand a browser:
#
#   * `asset_url` is whatever NOTARY_PUBLIC_BASE_URL was on the machine that
#     issued the certificate. On a deployment that only *reads* certificates,
#     replaying it points the viewer's <video> at the issuer's origin -- which
#     for a locally-issued certificate means the viewer's own localhost.
#   * `thumbnail_url` is a bare S3 URL into a private bucket, so an
#     unauthenticated GET is a 403.
#
# So the API returns app-relative paths for playback, resolved against
# whichever origin is serving, and lets these endpoints mint a fresh presigned
# URL per request. The sealed values stay in the certificate document,
# unaltered and still shown as the record.
# --------------------------------------------------------------------------


def _media_urls(certificate_id: str, thumbnail_url: str | None) -> dict[str, str | None]:
    return {
        "playback_url": f"/api/certificates/{certificate_id}/asset",
        "poster_url": (
            f"/api/certificates/{certificate_id}/thumbnail" if thumbnail_url else None
        ),
    }


def _own_bucket_key(url: str) -> tuple[str, str] | None:
    """Map one of our own B2 URLs back to (bucket, key), else None."""
    from urllib.parse import unquote, urlparse

    settings = get_settings()
    parsed = urlparse(url)
    if not parsed.scheme.startswith("http"):
        return None
    if parsed.netloc != urlparse(settings.b2_endpoint).netloc:
        return None

    bucket, _, key = unquote(parsed.path).lstrip("/").partition("/")
    known = {settings.b2_bucket_vault, settings.b2_bucket_workbench}
    return (bucket, key) if bucket in known and key else None


# --------------------------------------------------------------------------
# System
# --------------------------------------------------------------------------


@router.get("/health")
async def health() -> dict[str, Any]:
    settings = get_settings()
    storage = get_storage()
    return {
        "status": "ok",
        "runtime": describe_runtime(),
        "genblaze": runtime_report(),
        "ffmpeg_available": tooling_available(),
        "storage": storage.bucket_report() if storage.available else {"available": False},
        "recordings": len(list_recordings(settings.seed_dir)),
        "stats": get_store().stats(),
    }


@router.get("/profiles")
async def profiles() -> dict[str, Any]:
    return {"profiles": describe_profiles()}


@router.get("/storage/layout")
async def storage_layout() -> dict[str, Any]:
    return describe_layout()


@router.get("/stats")
async def stats() -> dict[str, Any]:
    return get_store().stats()


@router.get("/evaluation")
async def evaluation() -> dict[str, Any]:
    """The Board's measured accuracy and the exhaustive safety proof.

    Served from the report `scripts/evaluate_board.py` last wrote, so the
    numbers in the interface are the numbers the code produced rather than a
    claim typed into a template. If the report is missing, that is reported
    plainly instead of being silently hidden -- an evaluation page that
    disappears when it has nothing good to say is worse than no page.
    """
    report_path = get_settings().evaluation_report
    if not report_path.exists():
        return {
            "available": False,
            "reason": (
                "No evaluation report found. Run "
                "`python scripts/evaluate_board.py` to generate one."
            ),
        }

    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {"available": False, "reason": f"report unreadable: {exc}"}

    return {"available": True, **report}


# --------------------------------------------------------------------------
# Reviews
# --------------------------------------------------------------------------


@router.post("/reviews", response_model=SubmitBriefResponse)
async def submit_brief(
    payload: SubmitBriefRequest, background: BackgroundTasks
) -> SubmitBriefResponse:
    settings = get_settings()

    if settings.mode is not RunMode.LIVE:
        raise HTTPException(
            status_code=409,
            detail=(
                f"This deployment runs in {settings.mode.value} mode, so live "
                "generation is disabled. Use POST /api/demo/replay/{id} to "
                "watch a recorded run of the full review, or set "
                "NOTARY_MODE=live with provider credentials."
            ),
        )

    brief = CampaignBrief(
        title=payload.title,
        prompt=payload.prompt,
        tenant=payload.tenant,
        compliance_profile=payload.compliance_profile,
        submitted_by=payload.submitted_by,
        brand_kit=payload.brand_kit,
        channel=payload.channel,
    )

    runner = ReviewRunner(settings)

    # The session id must be minted HERE and handed to the runner, not
    # generated inside it. The caller is told which stream to subscribe to
    # before the run starts, so if the runner invented its own id the client
    # would attach to a session that never emits a single event -- which is
    # exactly the bug this replaced.
    session_id = f"sess_{uuid.uuid4().hex[:12]}"

    async def _run() -> None:
        try:
            await runner.run(brief, session_id=session_id)
        except Exception:  # noqa: BLE001
            log.exception("background review failed for session %s", session_id)

    background.add_task(_run)

    return SubmitBriefResponse(
        session_id=session_id,
        campaign_id=brief.campaign_id,
        stream_url=f"/api/reviews/{session_id}/stream",
        mode=settings.mode.value,
        message="Review started. Subscribe to the stream to watch the Board work.",
    )


@router.get("/reviews")
async def list_reviews(limit: int = Query(default=50, ge=1, le=200)) -> dict[str, Any]:
    sessions = get_store().list_sessions(limit=limit)
    return {
        "sessions": [
            {
                "session_id": s.session_id,
                "title": s.brief.title,
                "tenant": s.brief.tenant,
                "status": s.status.value,
                "takes": len(s.takes),
                "certificate_id": s.certificate_id,
                "started_at": s.started_at.isoformat(),
                "compliance_profile": s.brief.compliance_profile,
            }
            for s in sessions
        ]
    }


@router.get("/reviews/{session_id}")
async def get_review(session_id: str) -> dict[str, Any]:
    session = get_store().get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail=f"no session {session_id}")
    return {
        "session": session.model_dump(mode="json"),
        "lineage": session.lineage(),
    }


@router.get("/reviews/{session_id}/stream")
async def stream_review(session_id: str, request: Request) -> EventSourceResponse:
    """SSE relay of the review as it happens.

    `Last-Event-ID` is honoured so a dropped connection resumes rather than
    replaying from zero — which matters because a run can span minutes and a
    reconnect mid-review would otherwise re-animate the whole checklist.
    """
    bus = get_bus()
    try:
        from_sequence = int(request.headers.get("last-event-id", 0))
    except ValueError:
        from_sequence = 0

    async def publisher() -> Any:
        async for event in bus.subscribe(session_id, from_sequence=from_sequence):
            if await request.is_disconnected():
                break
            yield event.to_sse()

    return EventSourceResponse(publisher(), ping=15000)


# --------------------------------------------------------------------------
# Demo / replay
# --------------------------------------------------------------------------


@router.get("/demo/recordings")
async def demo_recordings() -> dict[str, Any]:
    settings = get_settings()
    recordings = list_recordings(settings.seed_dir)
    return {
        "recordings": [r.summary() for r in recordings],
        "note": (
            "Each recording is the captured event stream of a real run. "
            "Replaying one exercises the same SSE contract and the same UI "
            "code path as a live review; events are flagged replayed=true."
        ),
    }


@router.get("/demo/media/{recording_id}/{filename}")
async def demo_media(recording_id: str, filename: str) -> FileResponse:
    """Serve a reviewed frame that ships alongside a recording.

    These are the exact images the deterministic checks measured, kept so a
    replayed review can show the frame whose pixels produced the numbers on
    screen. A measurement you cannot look at is one the viewer has to take on
    faith.

    Both path segments are resolved and confined to the seed directory before
    anything is opened — `recording_id` and `filename` arrive from the URL, and
    a `..` in either would otherwise read arbitrary files off the host.
    """
    settings = get_settings()
    root = settings.seed_dir.resolve()
    target = (root / recording_id / filename).resolve()

    if not target.is_relative_to(root) or not target.is_file():
        raise HTTPException(status_code=404, detail="no such media")
    if target.suffix.lower() not in {".jpg", ".jpeg", ".png", ".mp4", ".webm"}:
        raise HTTPException(status_code=404, detail="unsupported media type")

    return FileResponse(target, headers={"Cache-Control": "public, max-age=3600"})


@router.post("/demo/replay/{recording_id}", response_model=ReplayResponse)
async def demo_replay(
    recording_id: str, payload: ReplayRequest | None = None
) -> ReplayResponse:
    settings = get_settings()
    directory = settings.seed_dir / recording_id
    recording = load_recording(directory)
    if recording is None:
        raise HTTPException(status_code=404, detail=f"no recording {recording_id}")

    session_id = f"replay-{uuid.uuid4().hex[:12]}"
    speed = (payload or ReplayRequest()).speed

    asyncio.create_task(replay_into_bus(recording, get_bus(), session_id, speed=speed))

    return ReplayResponse(
        session_id=session_id,
        stream_url=f"/api/reviews/{session_id}/stream",
        recording=recording.summary(),
    )


# --------------------------------------------------------------------------
# Human queue
# --------------------------------------------------------------------------


@router.get("/queue")
async def human_queue() -> dict[str, Any]:
    store = get_store()
    return {
        "items": store.queue_items(),
        "depth": store.queue_depth(),
        "note": (
            "Everything here is a take the Board declined to clear on its own. "
            "Nothing in this queue has been published."
        ),
    }


@router.post("/queue/{session_id}/signoff")
async def signoff(session_id: str, payload: SignoffRequest) -> dict[str, Any]:
    store = get_store()
    session = store.apply_signoff(
        session_id,
        HumanSignoff(
            reviewer=payload.reviewer, decision=payload.decision, note=payload.note
        ),
    )
    if session is None:
        raise HTTPException(
            status_code=404, detail=f"no escalated take for session {session_id}"
        )

    certificate_id: str | None = None
    if payload.decision == "approved":
        take = session.current_take
        if take and take.verdict:
            try:
                runner = ReviewRunner()
                await runner._certify(session, None, take.verdict)  # noqa: SLF001
                certificate_id = session.certificate_id
            except Exception as exc:  # noqa: BLE001
                log.exception("certification after human approval failed")
                raise HTTPException(
                    status_code=500, detail=f"certification failed: {exc}"
                ) from exc

    return {
        "session_id": session_id,
        "decision": payload.decision,
        "certificate_id": certificate_id,
        "queue_depth": store.queue_depth(),
        "message": (
            "Approved and sealed; the reviewer's sign-off is inside the "
            "immutable record."
            if payload.decision == "approved"
            else "Rejected. Nothing was published."
        ),
    }


# --------------------------------------------------------------------------
# Library and certificates
# --------------------------------------------------------------------------


@router.get("/library")
async def library(
    tenant: str | None = None, campaign_id: str | None = None
) -> dict[str, Any]:
    certificates = get_store().list_certificates(tenant=tenant, campaign_id=campaign_id)
    return {
        "assets": [
            {
                "certificate_id": c.certificate_id,
                "asset_id": c.asset_id,
                "campaign_id": c.campaign_id,
                "tenant": c.tenant,
                "asset_url": c.asset_url,
                "thumbnail_url": c.thumbnail_url,
                **_media_urls(c.certificate_id, c.thumbnail_url),
                "model": c.model,
                "provider": c.provider,
                "certified_at": c.certified_at.isoformat(),
                "retention_until": c.retention_until.isoformat(),
                "trust_mode": c.trust_mode,
                "is_sealed": c.is_sealed,
                "decision": c.verdict.decision.value,
                "takes": len(c.lineage),
                "prompt": c.prompt[:240],
            }
            for c in certificates
        ]
    }


@router.get("/certificates/{certificate_id}")
async def certificate(certificate_id: str) -> dict[str, Any]:
    cert = get_store().get_certificate(certificate_id)
    if cert is None:
        raise HTTPException(status_code=404, detail=f"no certificate {certificate_id}")
    return {
        "certificate": cert.model_dump(mode="json"),
        **_media_urls(cert.certificate_id, cert.thumbnail_url),
        "trust_mode": cert.trust_mode,
        "trust_mode_label": (
            "Mode 2 — authenticated integrity (Ed25519)"
            if cert.trust_mode == 2
            else "Mode 1 — integrity only"
        ),
        "is_sealed": cert.is_sealed,
    }


@router.get("/certificates/{certificate_id}/asset")
async def certificate_asset(certificate_id: str) -> RedirectResponse:
    """Durable URL for a certified asset, backed by a private bucket.

    A presigned URL expires, so a certificate must never embed one -- a
    provenance record pointing at a dead link is worse than no record. Instead
    the certificate stores the object key, and this endpoint mints a fresh
    presigned URL on each request and redirects to it.

    The result: a permanent, shareable reference to certified media that lives
    in a private bucket. For a public bucket with NOTARY_B2_PUBLIC_VAULT_BASE
    set, this still works but simply redirects to the durable B2 URL.
    """
    cert = get_store().get_certificate(certificate_id)
    if cert is None:
        raise HTTPException(status_code=404, detail=f"no certificate {certificate_id}")

    storage = get_storage()
    if not storage.available:
        raise HTTPException(
            status_code=503,
            detail="B2 is not configured; the asset cannot be served.",
        )

    settings = get_settings()

    if cert.asset_version_id:
        # Serve the exact sealed version. Resolving the bare key would follow a
        # delete marker (404) or a later upload -- neither of which is the
        # object this certificate attests to. Verified against live B2: the
        # sealed version remains readable after a delete marker is placed.
        url = await asyncio.to_thread(
            storage.presigned_version_url,
            settings.b2_bucket_vault,
            cert.asset_key,
            cert.asset_version_id,
            expires_in=3600,
        )
    else:
        url = await asyncio.to_thread(
            storage.serve_url, settings.b2_bucket_vault, cert.asset_key, expires_in=3600
        )
    # 307 preserves the method and tells caches this is not permanent -- the
    # target changes on every request by design.
    return RedirectResponse(url=url, status_code=307)


@router.get("/certificates/{certificate_id}/thumbnail")
async def certificate_thumbnail(certificate_id: str) -> RedirectResponse:
    """Poster frame for a certified asset, mediated the same way as the asset.

    The certificate stores `thumbnail_url` as a bare S3 URL, which was
    reachable only because the process that wrote it held credentials. The
    vault is private, so handing that URL to a browser is a 403. This resolves
    the key back out of the sealed URL and mints a presigned one per request.
    """
    cert = get_store().get_certificate(certificate_id)
    if cert is None:
        raise HTTPException(status_code=404, detail=f"no certificate {certificate_id}")
    if not cert.thumbnail_url:
        raise HTTPException(status_code=404, detail="no thumbnail for this certificate")

    storage = get_storage()
    if not storage.available:
        raise HTTPException(
            status_code=503, detail="B2 is not configured; the thumbnail cannot be served."
        )

    own = _own_bucket_key(cert.thumbnail_url)
    if own is None:
        # Already a URL a browser can fetch (public vault base, or a host we
        # do not own). Pass it through rather than guessing at a key.
        return RedirectResponse(url=cert.thumbnail_url, status_code=307)

    bucket, key = own
    url = await asyncio.to_thread(storage.serve_url, bucket, key, expires_in=3600)
    return RedirectResponse(url=url, status_code=307)


@router.post("/certificates/{certificate_id}/verify")
async def verify(certificate_id: str) -> dict[str, Any]:
    cert = get_store().get_certificate(certificate_id)
    if cert is None:
        raise HTTPException(status_code=404, detail=f"no certificate {certificate_id}")

    # Resolve the object the certificate *names*, not the URL it happens to
    # carry: asset_url is fixed at sealing time and cannot be corrected
    # afterwards, since Object Lock makes the certificate immutable.
    fetch_url: str | None = None
    storage = get_storage()
    if storage.available and cert.asset_key:
        settings = get_settings()
        try:
            if cert.asset_version_id:
                fetch_url = await asyncio.to_thread(
                    storage.presigned_version_url,
                    settings.b2_bucket_vault, cert.asset_key, cert.asset_version_id,
                )
            else:
                fetch_url = await asyncio.to_thread(
                    storage.serve_url, settings.b2_bucket_vault, cert.asset_key
                )
        except Exception as exc:  # noqa: BLE001
            log.warning("could not resolve a fetch URL for %s: %s", certificate_id, exc)

    async with httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=10.0)) as client:
        report = await verify_certificate(cert, client=client, fetch_url=fetch_url)

    return {
        "report": report.model_dump(mode="json"),
        "passed": report.passed,
        "summary": summarize(report),
    }
