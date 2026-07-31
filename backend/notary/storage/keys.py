"""Object key strategies.

Notary uses two different layouts, in two different buckets, for two different
reasons. Both choices are defensible out loud, which is the point -- a single
flat prefix would work and would say nothing about understanding the storage.

    HIERARCHICAL        vault/{tenant}/{campaign}/{asset_id}/...
                        Certified output. Optimised for a human with an audit
                        request: "show me everything Acme certified for the Q3
                        campaign" is a prefix listing, not a database query.
                        The manifest, verdict, certificate and media for one
                        asset sit together under one prefix, so the complete
                        record can be retrieved -- or legally produced -- as a
                        single unit.

    CONTENT_ADDRESSABLE cache/{sha256[:2]}/{sha256[2:4]}/{sha256}
                        Render cache and extracted frames. Identical bytes get
                        identical keys, so re-running an unchanged brief costs
                        one HEAD request instead of a Kling render. The two-level
                        fan-out keeps any single prefix from growing unbounded.

The workbench uses a run-scoped hierarchy because its whole lifecycle is
"expire together": a Lifecycle Rule deletes by prefix age, and grouping every
draft of a run under one prefix makes that rule trivially correct.
"""

from __future__ import annotations

import hashlib
import re
from enum import StrEnum

_UNSAFE = re.compile(r"[^a-zA-Z0-9._-]+")


class Strategy(StrEnum):
    HIERARCHICAL = "hierarchical"
    CONTENT_ADDRESSABLE = "content_addressable"
    RUN_SCOPED = "run_scoped"


def slug(value: str, *, max_length: int = 64) -> str:
    """Make a path segment safe for an S3 key.

    S3 tolerates far more than this. Notary is stricter on purpose: these keys
    end up in URLs, in CLI arguments, and in audit exports, and a tenant name
    with a slash in it would silently create a phantom directory level that
    breaks every prefix listing built on it.
    """
    cleaned = _UNSAFE.sub("-", (value or "").strip()).strip("-.")
    cleaned = re.sub(r"-{2,}", "-", cleaned)
    if not cleaned:
        cleaned = "unnamed"
    return cleaned[:max_length].lower()


# --------------------------------------------------------------------------
# Vault — HIERARCHICAL
# --------------------------------------------------------------------------


def vault_prefix(tenant: str, campaign_id: str, asset_id: str) -> str:
    return f"vault/{slug(tenant)}/{slug(campaign_id)}/{slug(asset_id)}"


def vault_asset_key(tenant: str, campaign_id: str, asset_id: str, ext: str = "mp4") -> str:
    return f"{vault_prefix(tenant, campaign_id, asset_id)}/asset.{ext.lstrip('.')}"


def vault_manifest_key(tenant: str, campaign_id: str, asset_id: str) -> str:
    return f"{vault_prefix(tenant, campaign_id, asset_id)}/manifest.json"


def vault_verdict_key(tenant: str, campaign_id: str, asset_id: str) -> str:
    return f"{vault_prefix(tenant, campaign_id, asset_id)}/verdict.json"


def vault_certificate_key(tenant: str, campaign_id: str, asset_id: str) -> str:
    return f"{vault_prefix(tenant, campaign_id, asset_id)}/certificate.json"


def vault_thumbnail_key(tenant: str, campaign_id: str, asset_id: str) -> str:
    return f"{vault_prefix(tenant, campaign_id, asset_id)}/thumbnail.jpg"


def campaign_prefix(tenant: str, campaign_id: str) -> str:
    """Prefix for 'every certified asset in this campaign'. The audit query."""
    return f"vault/{slug(tenant)}/{slug(campaign_id)}/"


def tenant_prefix(tenant: str) -> str:
    return f"vault/{slug(tenant)}/"


# --------------------------------------------------------------------------
# Workbench — RUN_SCOPED, lifecycle-expired
# --------------------------------------------------------------------------


def workbench_prefix(tenant: str, run_id: str) -> str:
    return f"workbench/{slug(tenant)}/{slug(run_id)}"


def workbench_take_key(tenant: str, run_id: str, take: int, ext: str = "mp4") -> str:
    return f"{workbench_prefix(tenant, run_id)}/take-{take:02d}.{ext.lstrip('.')}"


def workbench_frame_key(tenant: str, run_id: str, take: int, index: int) -> str:
    return f"{workbench_prefix(tenant, run_id)}/take-{take:02d}/frame-{index:02d}.jpg"


def workbench_storyboard_key(tenant: str, run_id: str, take: int) -> str:
    return f"{workbench_prefix(tenant, run_id)}/take-{take:02d}/storyboard.png"


def workbench_verdict_key(tenant: str, run_id: str, take: int) -> str:
    """Rejected takes keep their verdict.

    The media expires with the Lifecycle Rule; this small JSON is what remains
    to answer "why was this rejected?" long after the bytes are gone. Retaining
    the reasoning while discarding the payload is the cheap, correct trade.
    """
    return f"{workbench_prefix(tenant, run_id)}/take-{take:02d}/verdict.json"


# --------------------------------------------------------------------------
# Cache — CONTENT_ADDRESSABLE
# --------------------------------------------------------------------------


def content_key(data: bytes | str, *, ext: str = "bin", prefix: str = "cache") -> str:
    payload = data.encode("utf-8") if isinstance(data, str) else data
    digest = hashlib.sha256(payload).hexdigest()
    return content_key_from_digest(digest, ext=ext, prefix=prefix)


def content_key_from_digest(
    digest: str, *, ext: str = "bin", prefix: str = "cache"
) -> str:
    digest = digest.lower()
    return f"{prefix}/{digest[:2]}/{digest[2:4]}/{digest}.{ext.lstrip('.')}"


# --------------------------------------------------------------------------
# Replay / analytics
# --------------------------------------------------------------------------


def replay_key(session_id: str) -> str:
    return f"replay/{slug(session_id)}/events.ndjson"


def analytics_key(day: str) -> str:
    return f"analytics/dt={day}/reviews.parquet"


def describe_layout() -> dict[str, object]:
    """Rendered on the storage panel in the UI and in the README."""
    return {
        "buckets": {
            "vault": {
                "strategy": Strategy.HIERARCHICAL.value,
                "object_lock": "COMPLIANCE",
                "pattern": (
                    "vault/{tenant}/{campaign}/{asset_id}/"
                    "{asset,manifest,verdict,certificate,thumbnail}"
                ),
                "why": (
                    "Audit-shaped. One prefix returns the complete sealed "
                    "record for one asset; one prefix up returns the campaign."
                ),
            },
            "workbench": {
                "strategy": Strategy.RUN_SCOPED.value,
                "object_lock": None,
                "lifecycle": "expire drafts after N days",
                "pattern": "workbench/{tenant}/{run_id}/take-NN...",
                "why": (
                    "Everything for a run expires together, so the Lifecycle "
                    "Rule is a prefix age rule and cannot orphan bytes. "
                    "Rejected verdicts are kept after the media expires."
                ),
            },
            "cache": {
                "strategy": Strategy.CONTENT_ADDRESSABLE.value,
                "pattern": "cache/{sha[0:2]}/{sha[2:4]}/{sha}.ext",
                "why": (
                    "Identical bytes deduplicate to one object; re-running an "
                    "unchanged brief costs a HEAD, not a render."
                ),
            },
        }
    }
