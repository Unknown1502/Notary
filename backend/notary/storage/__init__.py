"""Backblaze B2 storage: the two-bucket split and the Object Lock seal.

    vault/      Object Lock COMPLIANCE. Certified asset + manifest + verdict +
                certificate under one prefix, immutable for the retention
                window. The storage-layer guarantee the product rests on.

    workbench/  No lock, Lifecycle-expired. Drafts and rejected takes, whose
                verdicts outlive their media.

Genblaze's ObjectStorageSink writes generated assets here during a run;
b2.py drops to the S3 API for the operations the sink does not expose --
Object Lock retention, bucket bootstrap, lifecycle, and CORS.
"""

from .b2 import B2Storage, StorageUnavailable, StoredObject, get_storage
from .keys import (
    Strategy,
    campaign_prefix,
    content_key,
    content_key_from_digest,
    describe_layout,
    replay_key,
    slug,
    tenant_prefix,
    vault_asset_key,
    vault_certificate_key,
    vault_manifest_key,
    vault_prefix,
    vault_thumbnail_key,
    vault_verdict_key,
    workbench_frame_key,
    workbench_prefix,
    workbench_storyboard_key,
    workbench_take_key,
    workbench_verdict_key,
)

__all__ = [
    "B2Storage",
    "StorageUnavailable",
    "StoredObject",
    "Strategy",
    "campaign_prefix",
    "content_key",
    "content_key_from_digest",
    "describe_layout",
    "get_storage",
    "replay_key",
    "slug",
    "tenant_prefix",
    "vault_asset_key",
    "vault_certificate_key",
    "vault_manifest_key",
    "vault_prefix",
    "vault_thumbnail_key",
    "vault_verdict_key",
    "workbench_frame_key",
    "workbench_prefix",
    "workbench_storyboard_key",
    "workbench_take_key",
    "workbench_verdict_key",
]
