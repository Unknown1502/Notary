"""Backblaze B2 storage, including the parts Genblaze's sink doesn't reach.

Division of responsibility
--------------------------
Genblaze's `ObjectStorageSink(S3StorageBackend.for_backblaze(...))` handles the
happy path beautifully: it persists generated assets and their manifests to B2
as a pipeline runs. Notary uses it for exactly that, in pipeline/factory.py.

But certification needs something the sink does not expose — **writing an
object with an Object Lock retention header**. That is an S3 API concern
(`ObjectLockMode` + `ObjectLockRetainUntilDate` on PutObject), so this module
drops to boto3 for the seal, and for bucket bootstrap, lifecycle, and CORS.

Using the SDK where it fits and the underlying API where it doesn't is the
honest engineering answer, and it's why Notary can make an immutability claim
that an application-layer convention could not.

Why Object Lock is the load-bearing feature
-------------------------------------------
Compliance-mode retention is enforced by the storage layer, not by Notary. Once
written, an object cannot be overwritten or deleted before its retention date
by *anyone* — not an admin, not the account owner, not someone holding stolen
application keys. That property is what upgrades "we keep a record" into "the
record cannot be revised after the fact", which is the entire premise of a
compliance audit trail.

It also means a mistake is permanent, which is why `vault_retention_days`
defaults to 7 in development. A 10-year default is how you fill a bucket with
undeletable test garbage on day one.
"""

from __future__ import annotations

import io
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Iterator

from ..config import Settings, get_settings

log = logging.getLogger(__name__)

try:  # boto3 is required for live/hybrid, optional for replay
    import boto3
    from botocore.config import Config as BotoConfig
    from botocore.exceptions import ClientError

    BOTO_AVAILABLE = True
except Exception:  # noqa: BLE001  # pragma: no cover
    BOTO_AVAILABLE = False
    ClientError = Exception  # type: ignore[assignment,misc]


class StorageUnavailable(RuntimeError):
    """B2 was asked for something it cannot do in the current mode."""


@dataclass(frozen=True)
class StoredObject:
    key: str
    bucket: str
    size: int
    etag: str
    last_modified: datetime
    url: str
    retention_until: datetime | None = None
    retention_mode: str | None = None

    @property
    def is_sealed(self) -> bool:
        return bool(
            self.retention_until and self.retention_until > datetime.now(UTC)
        )


class B2Storage:
    """Thin, explicit B2 client. One instance per process.

    Deliberately not a generic S3 wrapper: every method here exists because
    Notary needs that exact operation, and the Object Lock methods carry
    warnings a generic wrapper would not.
    """

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._client: Any = None

    # ------------------------------------------------------------------ setup

    @property
    def available(self) -> bool:
        return BOTO_AVAILABLE and self.settings.b2_configured

    @property
    def client(self) -> Any:
        if not BOTO_AVAILABLE:
            raise StorageUnavailable(
                "boto3 is not installed. Install the backend requirements, or "
                "run with NOTARY_MODE=replay."
            )
        if not self.settings.b2_configured:
            raise StorageUnavailable(
                "B2 credentials are not configured. Set NOTARY_B2_KEY_ID and "
                "NOTARY_B2_APPLICATION_KEY, or run with NOTARY_MODE=replay."
            )
        if self._client is None:
            self._client = boto3.client(
                "s3",
                endpoint_url=self.settings.b2_endpoint,
                region_name=self.settings.b2_region,
                aws_access_key_id=self.settings.b2_key_id,
                aws_secret_access_key=self.settings.b2_application_key,
                config=BotoConfig(
                    signature_version="s3v4",
                    retries={"max_attempts": 4, "mode": "standard"},
                    # B2's S3 layer is strict about checksum behaviour; the
                    # default flexible-checksum path can produce signature
                    # mismatches on streaming uploads.
                    request_checksum_calculation="when_required",
                    response_checksum_validation="when_required",
                ),
            )
        return self._client

    # ------------------------------------------------------------ bucket ops

    def create_buckets(self) -> dict[str, str]:
        """Create the vault (Object Lock ON) and workbench (lock OFF) buckets.

        **Object Lock can only be enabled at bucket creation.** It cannot be
        retrofitted. Getting this wrong on day one means deleting the bucket and
        starting over, which is why bootstrap is a script and not a lazy
        side-effect of the first write.
        """
        results: dict[str, str] = {}

        for bucket, locked in (
            (self.settings.b2_bucket_vault, True),
            (self.settings.b2_bucket_workbench, False),
        ):
            try:
                kwargs: dict[str, Any] = {"Bucket": bucket}
                if locked:
                    kwargs["ObjectLockEnabledForBucket"] = True
                self.client.create_bucket(**kwargs)
                results[bucket] = "created" + (" (object lock enabled)" if locked else "")
                log.info("created bucket %s (object_lock=%s)", bucket, locked)
            except ClientError as exc:
                code = exc.response.get("Error", {}).get("Code", "")
                if code in {"BucketAlreadyOwnedByYou", "BucketAlreadyExists"}:
                    results[bucket] = "already exists"
                else:
                    results[bucket] = f"error: {code or exc}"
                    log.error("could not create %s: %s", bucket, exc)

        return results

    def configure_lifecycle(self) -> str:
        """Expire workbench drafts.

        Rejected takes are not precious. Their *verdicts* are, and those are
        copied into the vault on certification or kept as small JSON here, so
        expiring the media loses no accountability.
        """
        days = self.settings.workbench_expiry_days
        try:
            self.client.put_bucket_lifecycle_configuration(
                Bucket=self.settings.b2_bucket_workbench,
                LifecycleConfiguration={
                    "Rules": [
                        {
                            "ID": "expire-drafts",
                            "Status": "Enabled",
                            "Filter": {"Prefix": "workbench/"},
                            "Expiration": {"Days": days},
                            "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 1},
                        }
                    ]
                },
            )
            return f"workbench/ expires after {days} day(s)"
        except ClientError as exc:
            log.error("lifecycle configuration failed: %s", exc)
            return f"error: {exc}"

    def configure_cors(self) -> str:
        """Allow browser playback and range requests.

        Without this, `<video>` playback from B2 fails in the browser with an
        opaque CORS error and no server-side symptom -- a genuinely expensive
        hour to debug if you meet it for the first time during a demo.
        """
        rule = {
            "AllowedHeaders": ["*"],
            "AllowedMethods": ["GET", "HEAD"],
            "AllowedOrigins": self.settings.cors_origins or ["*"],
            "ExposeHeaders": ["Content-Length", "Content-Range", "ETag", "Accept-Ranges"],
            "MaxAgeSeconds": 3600,
        }
        out: list[str] = []
        for bucket in (self.settings.b2_bucket_vault, self.settings.b2_bucket_workbench):
            try:
                self.client.put_bucket_cors(
                    Bucket=bucket, CORSConfiguration={"CORSRules": [rule]}
                )
                out.append(f"{bucket}: ok")
            except ClientError as exc:
                out.append(f"{bucket}: {exc}")
        return "; ".join(out)

    # ------------------------------------------------------------- write ops

    def put(
        self,
        bucket: str,
        key: str,
        body: bytes,
        *,
        content_type: str = "application/octet-stream",
        metadata: dict[str, str] | None = None,
        retention_days: int | None = None,
        cache_control: str | None = None,
    ) -> StoredObject:
        """Write an object, optionally sealing it under Object Lock.

        When `retention_days` is set, the object becomes immutable for that
        window in COMPLIANCE mode. There is no undo. The caller passing this
        argument is asserting the content is final.
        """
        kwargs: dict[str, Any] = {
            "Bucket": bucket,
            "Key": key,
            "Body": body,
            "ContentType": content_type,
        }
        if metadata:
            kwargs["Metadata"] = {k: str(v)[:1024] for k, v in metadata.items()}
        if cache_control:
            kwargs["CacheControl"] = cache_control

        retain_until: datetime | None = None
        if retention_days:
            retain_until = datetime.now(UTC) + timedelta(days=retention_days)
            kwargs["ObjectLockMode"] = "COMPLIANCE"
            kwargs["ObjectLockRetainUntilDate"] = retain_until

        try:
            response = self.client.put_object(**kwargs)
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if retention_days and code in {"InvalidRequest", "InvalidBucketState"}:
                raise StorageUnavailable(
                    f"Object Lock write to '{bucket}' was rejected ({code}). "
                    "Object Lock must be enabled AT BUCKET CREATION and cannot "
                    "be added later. Run scripts/bootstrap_b2.py against a new "
                    "bucket."
                ) from exc
            raise

        if retain_until:
            log.info(
                "sealed s3://%s/%s under COMPLIANCE retention until %s",
                bucket,
                key,
                retain_until.isoformat(),
            )

        return StoredObject(
            key=key,
            bucket=bucket,
            size=len(body),
            etag=(response.get("ETag") or "").strip('"'),
            last_modified=datetime.now(UTC),
            url=self.public_url(bucket, key),
            retention_until=retain_until,
            retention_mode="COMPLIANCE" if retain_until else None,
        )

    def put_json(
        self,
        bucket: str,
        key: str,
        payload: Any,
        *,
        retention_days: int | None = None,
        metadata: dict[str, str] | None = None,
    ) -> StoredObject:
        body = json.dumps(payload, indent=2, sort_keys=True, default=str).encode("utf-8")
        return self.put(
            bucket,
            key,
            body,
            content_type="application/json",
            metadata=metadata,
            retention_days=retention_days,
        )

    def copy_into_vault(
        self,
        source_bucket: str,
        source_key: str,
        dest_key: str,
        *,
        retention_days: int,
        content_type: str = "video/mp4",
    ) -> StoredObject:
        """Promote a workbench draft into the sealed vault.

        Read-then-write rather than server-side CopyObject. Slower, and chosen
        anyway: streaming the bytes through the process is what lets Notary
        compute the SHA-256 it is about to certify from the *exact* bytes it
        seals, instead of trusting a value computed earlier at a different
        layer. Certifying a hash you did not personally compute over the sealed
        object is a gap, and this closes it.
        """
        body = self.get_bytes(source_bucket, source_key)
        return self.put(
            self.settings.b2_bucket_vault,
            dest_key,
            body,
            content_type=content_type,
            retention_days=retention_days,
            cache_control="public, max-age=31536000, immutable",
        )

    # -------------------------------------------------------------- read ops

    def get_bytes(self, bucket: str, key: str) -> bytes:
        buf = io.BytesIO()
        self.client.download_fileobj(bucket, key, buf)
        return buf.getvalue()

    def get_json(self, bucket: str, key: str) -> Any:
        return json.loads(self.get_bytes(bucket, key).decode("utf-8"))

    def exists(self, bucket: str, key: str) -> bool:
        try:
            self.client.head_object(Bucket=bucket, Key=key)
            return True
        except ClientError:
            return False

    def head(self, bucket: str, key: str) -> StoredObject | None:
        try:
            r = self.client.head_object(Bucket=bucket, Key=key)
        except ClientError:
            return None

        retain_until = r.get("ObjectLockRetainUntilDate")
        if isinstance(retain_until, datetime) and retain_until.tzinfo is None:
            retain_until = retain_until.replace(tzinfo=UTC)

        return StoredObject(
            key=key,
            bucket=bucket,
            size=int(r.get("ContentLength", 0)),
            etag=(r.get("ETag") or "").strip('"'),
            last_modified=r.get("LastModified", datetime.now(UTC)),
            url=self.public_url(bucket, key),
            retention_until=retain_until,
            retention_mode=r.get("ObjectLockMode"),
        )

    def list_prefix(
        self, bucket: str, prefix: str, *, max_keys: int = 1000
    ) -> Iterator[StoredObject]:
        paginator = self.client.get_paginator("list_objects_v2")
        for page in paginator.paginate(
            Bucket=bucket, Prefix=prefix, PaginationConfig={"MaxItems": max_keys}
        ):
            for item in page.get("Contents", []):
                yield StoredObject(
                    key=item["Key"],
                    bucket=bucket,
                    size=int(item.get("Size", 0)),
                    etag=(item.get("ETag") or "").strip('"'),
                    last_modified=item["LastModified"],
                    url=self.public_url(bucket, item["Key"]),
                )

    def list_common_prefixes(self, bucket: str, prefix: str) -> list[str]:
        """One 'directory' level. How the library enumerates campaigns/assets."""
        out: list[str] = []
        paginator = self.client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix, Delimiter="/"):
            for cp in page.get("CommonPrefixes", []):
                out.append(cp["Prefix"])
        return out

    # ----------------------------------------------------------------- URLs

    def public_url(self, bucket: str, key: str) -> str:
        """Durable, credential-free URL for browser playback.

        Credential-free means publicly readable. That is the correct trade for
        published marketing creative and the wrong one for anything sensitive;
        `presigned_url` is the alternative and docs/B2-AND-GENBLAZE.md states
        which deployment should use which.
        """
        base = self.settings.b2_public_vault_base
        if base and bucket == self.settings.b2_bucket_vault:
            return f"{base.rstrip('/')}/{key}"
        endpoint = self.settings.b2_endpoint.rstrip("/")
        return f"{endpoint}/{bucket}/{key}"

    def presigned_url(self, bucket: str, key: str, *, expires_in: int = 3600) -> str:
        return self.client.generate_presigned_url(
            "get_object",
            Params={"Bucket": bucket, "Key": key},
            ExpiresIn=expires_in,
        )

    # ------------------------------------------------------------- diagnostics

    def bucket_report(self) -> dict[str, Any]:
        """Live storage posture, surfaced at /api/health and in the UI."""
        if not self.available:
            return {"available": False, "reason": "credentials not configured"}

        report: dict[str, Any] = {"available": True, "buckets": {}}
        for label, bucket in (
            ("vault", self.settings.b2_bucket_vault),
            ("workbench", self.settings.b2_bucket_workbench),
        ):
            entry: dict[str, Any] = {"name": bucket}
            try:
                lock = self.client.get_object_lock_configuration(Bucket=bucket)
                cfg = lock.get("ObjectLockConfiguration", {})
                entry["object_lock"] = cfg.get("ObjectLockEnabled", "Disabled")
            except ClientError as exc:
                code = exc.response.get("Error", {}).get("Code", "")
                entry["object_lock"] = (
                    "Disabled" if code == "ObjectLockConfigurationNotFoundError" else code
                )
            report["buckets"][label] = entry

        report["retention_days"] = self.settings.vault_retention_days
        return report


_storage: B2Storage | None = None


def get_storage() -> B2Storage:
    global _storage
    if _storage is None:
        _storage = B2Storage()
    return _storage
