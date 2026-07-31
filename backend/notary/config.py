"""Runtime configuration.

Notary runs in one of three modes. The mode is the single most important
operational switch in the system, because it decides whether a request costs
money and minutes, or nothing and milliseconds.

    live    Real providers, real B2, real spend. What a customer runs.
    replay  No credentials, no providers, no network. Deterministic playback of
            recorded runs from `seed/`. What a hackathon judge hits first, and
            what CI runs against.
    hybrid  Real B2 reads (library, certificates, live hash verification) with
            replayed generation. Lets a judge verify a real signed artifact in
            real storage without paying for a Kling render to get there.

`replay` is not a mock. It replays event streams captured from real `live` runs,
so the frontend, the SSE contract, and the certificate screens are exercised by
the same bytes a real run produced. See docs/ARCHITECTURE.md#replay.
"""

from __future__ import annotations

import os
from enum import StrEnum
from functools import lru_cache
from pathlib import Path

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[2]


class RunMode(StrEnum):
    LIVE = "live"
    REPLAY = "replay"
    HYBRID = "hybrid"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="NOTARY_",
        env_file=(REPO_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ------------------------------------------------------------------ mode
    mode: RunMode = RunMode.REPLAY
    """Default to REPLAY so a fresh clone runs with zero credentials."""

    seed_dir: Path = REPO_ROOT / "seed"
    public_base_url: str = "http://localhost:8000"

    # ----------------------------------------------------------- backblaze b2
    b2_key_id: str | None = None
    b2_application_key: str | None = None
    b2_endpoint: str = "https://s3.us-west-004.backblazeb2.com"
    b2_region: str = "us-west-004"

    b2_bucket_workbench: str = "notary-workbench"
    """Drafts and rejected takes. Lifecycle-expired. NOT object-locked."""

    b2_bucket_vault: str = "notary-vault"
    """Certified finals. Object Lock enabled AT CREATION, compliance mode."""

    b2_public_vault_base: str | None = None
    """Backblaze friendly URL base (f004.backblazeb2.com/file/<bucket>) for
    credential-free <video> playback. None => serve via presigned URLs."""

    vault_retention_days: int = Field(default=7, ge=1, le=36500)
    """Object Lock retention. SHORT in dev on purpose: compliance-mode objects
    cannot be deleted before this lapses, by anyone, including the account
    owner. A 10-year dev default is how you accrue permanent test garbage."""

    workbench_expiry_days: int = 3

    # -------------------------------------------------------------- providers
    gmicloud_api_key: str | None = None
    luma_api_key: str | None = None
    openai_api_key: str | None = None

    image_model: str = "seedream-5.0-lite"
    video_model: str = "kling-image2video-v2.1-master"
    video_fallback_model: str = "ray-2"
    board_vision_model: str = "qwen2.5-vl-72b-instruct"

    step_timeout_seconds: int = 600
    max_board_iterations: int = 3
    """AgentLoop cap. Iteration 1 is the draft; 2-3 are verdict-conditioned
    revisions. Exhausting the cap escalates to the human queue -- it never
    ships and it never silently passes."""

    # ------------------------------------------------------------- provenance
    signing_key_path: Path = REPO_ROOT / "keys" / "notary-ed25519.pem"
    signing_key_id: str = "notary-dev-2026"
    require_signing: bool = True
    """If True, certification aborts when the signing key is unavailable rather
    than emitting an unsigned certificate. Fail closed: an unsigned certificate
    that looks signed is worse than no certificate."""

    # ------------------------------------------------------------------ misc
    cors_origins: list[str] = ["http://localhost:5173", "http://localhost:4173"]
    log_level: str = "INFO"

    @model_validator(mode="after")
    def _validate_live_requirements(self) -> Settings:
        """Fail at startup, not at first request.

        A misconfigured LIVE deployment that only reveals itself four minutes
        into a judge's first render is the worst possible failure mode.
        """
        if self.mode is RunMode.REPLAY:
            return self

        missing: list[str] = []
        if not self.b2_key_id:
            missing.append("NOTARY_B2_KEY_ID")
        if not self.b2_application_key:
            missing.append("NOTARY_B2_APPLICATION_KEY")

        if self.mode is RunMode.LIVE and not self.gmicloud_api_key:
            missing.append("NOTARY_GMICLOUD_API_KEY")

        if missing:
            raise ValueError(
                f"mode={self.mode} requires: {', '.join(missing)}. "
                "Set them in .env, or run with NOTARY_MODE=replay for a "
                "credential-free demo."
            )
        return self

    @property
    def b2_configured(self) -> bool:
        return bool(self.b2_key_id and self.b2_application_key)

    @property
    def reads_real_storage(self) -> bool:
        return self.mode in (RunMode.LIVE, RunMode.HYBRID) and self.b2_configured

    @property
    def generates_for_real(self) -> bool:
        return self.mode is RunMode.LIVE


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def reset_settings_cache() -> None:
    """Test hook: forget memoized settings after mutating the environment."""
    get_settings.cache_clear()


def describe_runtime() -> dict[str, object]:
    """Surfaced at GET /api/health and rendered in the UI footer.

    A judge should be able to tell at a glance whether what they are looking at
    was generated live or replayed. Hiding that would be the kind of small
    dishonesty that undermines the entire provenance pitch.
    """
    s = get_settings()
    return {
        "mode": s.mode.value,
        "generates_for_real": s.generates_for_real,
        "reads_real_storage": s.reads_real_storage,
        "vault_bucket": s.b2_bucket_vault if s.reads_real_storage else None,
        "retention_days": s.vault_retention_days,
        "trust_mode": 2 if s.require_signing else 1,
        "signing_key_id": s.signing_key_id,
        "python_env": os.environ.get("NOTARY_ENV", "development"),
    }
