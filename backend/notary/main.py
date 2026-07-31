"""Notary — FastAPI application entrypoint.

Genblaze is library-only and stateless, which is what makes it a good fit for a
request-scoped service: there is no daemon to supervise, no broker to run, and
a pipeline is constructed and executed inside a handler. Notary is therefore a
single process plus Backblaze B2, and B2 holds all the durable state.

Run it:
    uvicorn notary.main:app --reload          # replay mode, no credentials
    NOTARY_MODE=live uvicorn notary.main:app  # real generation
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .api.routes import router
from .config import RunMode, get_settings
from .events import list_recordings
from .genblaze_compat import GENBLAZE_AVAILABLE, GENBLAZE_IMPORT_ERROR
from .media import tooling_available
from .store import get_store

log = logging.getLogger("notary")


def _configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    _configure_logging(settings.log_level)

    log.info("Notary starting in %s mode", settings.mode.value)

    if not GENBLAZE_AVAILABLE:
        log.warning(
            "genblaze SDK not importable (%s). Live generation is disabled; "
            "replay and verification are fully functional.",
            GENBLAZE_IMPORT_ERROR,
        )
    if not tooling_available():
        log.warning(
            "ffmpeg/ffprobe not found. Frame extraction is unavailable, so "
            "visual criteria will report UNCERTAIN and escalate to a human."
        )

    # B2 is the system of record. Rebuild the certificate index by listing the
    # vault -- if this works after a cold start with no database, the claim
    # holds.
    if settings.reads_real_storage:
        try:
            loaded = get_store().rehydrate_from_b2()
            log.info("rehydrated %d certificate(s) from the B2 vault", loaded)
        except Exception as exc:  # noqa: BLE001
            log.warning("could not rehydrate from B2: %s", exc)

    recordings = list_recordings(settings.seed_dir)
    log.info("%d demo recording(s) available for replay", len(recordings))

    if settings.mode is RunMode.REPLAY and not recordings:
        log.warning(
            "Replay mode with no recordings in %s. The app will start but the "
            "demo path has nothing to play. Run scripts/seed_demo.py.",
            settings.seed_dir,
        )

    yield

    log.info("Notary shutting down")


app = FastAPI(
    title="Notary",
    description=(
        "An AI Creative Review Board for generative media. Screens every "
        "generated take against brand and compliance criteria, revises the "
        "clear failures, escalates the ambiguous ones to a human, and seals "
        "what clears with a signed, immutable record on Backblaze B2."
    ),
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=get_settings().cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Last-Event-ID"],
)

app.include_router(router)


@app.get("/")
async def root() -> JSONResponse:
    settings = get_settings()
    return JSONResponse(
        {
            "name": "Notary",
            "tagline": "Every clip goes before the Board. Every approval is provable.",
            "mode": settings.mode.value,
            "docs": "/docs",
            "start_here": {
                "health": "/api/health",
                "recordings": "/api/demo/recordings",
                "library": "/api/library",
                "human_queue": "/api/queue",
            },
        }
    )
