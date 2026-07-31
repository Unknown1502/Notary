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

import base64
import hashlib
import io
import json
import logging
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

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

    version_id: str | None = None
    """The specific immutable version, and the thing that actually matters.

    B2 buckets keep all versions. On such a bucket `delete_object` without a
    version id does NOT remove anything -- it places a *delete marker* that
    becomes the new current version, so a plain GET or HEAD on the key returns
    404 while the sealed version sits underneath, untouched and still
    protected by Object Lock.

    A certificate that records only a key can therefore be made to look broken
    by anyone with write access, even though they cannot alter the sealed
    bytes. Recording the version id closes that: the exact certified version
    stays addressable no matter what is layered on top of the key.
    """

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

        # B2 buckets keep all versions by default, which changes what
        # "expire" means. On a versioned bucket an Expiration rule does not
        # remove anything -- it makes the current version noncurrent and
        # leaves a delete marker. Three rules are therefore needed, and B2
        # rejects the configuration outright (MalformedXML) if the
        # ExpiredObjectDeleteMarker rule is missing for the same prefix:
        #
        #   1. expire the current version after N days
        #   2. expire noncurrent versions, which is what actually frees bytes
        #   3. sweep the delete markers left behind by (1)
        #
        # Without 2 and 3 the drafts are invisible but still billed, forever.
        rules = [
            {
                "ID": "expire-draft-current",
                "Status": "Enabled",
                "Filter": {"Prefix": "workbench/"},
                "Expiration": {"Days": days},
                "NoncurrentVersionExpiration": {"NoncurrentDays": 1},
                "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 1},
            },
            {
                "ID": "purge-expired-delete-markers",
                "Status": "Enabled",
                "Filter": {"Prefix": "workbench/"},
                "Expiration": {"ExpiredObjectDeleteMarker": True},
            },
        ]

        try:
            self.client.put_bucket_lifecycle_configuration(
                Bucket=self.settings.b2_bucket_workbench,
                LifecycleConfiguration={"Rules": rules},
            )
            return (
                f"workbench/ current versions expire after {days} day(s), "
                "noncurrent after 1, delete markers swept"
            )
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

            # S3 REQUIRES Content-MD5 on any PutObject carrying Object Lock
            # parameters, and B2 enforces it. Without the header the request is
            # rejected with a bare InvalidRequest that says nothing about the
            # cause -- it looks exactly like a misconfigured bucket.
            #
            # boto3 would normally supply this, but the client is built with
            # request_checksum_calculation="when_required" (needed to keep B2's
            # S3 layer happy on streaming uploads), which suppresses it. So it
            # is computed explicitly here. MD5 is used for transport integrity
            # only, because that is what the API specifies; it is not a
            # security property, and the provenance hashes are SHA-256.
            kwargs["ContentMD5"] = base64.b64encode(
                hashlib.md5(body).digest()  # noqa: S324 - required by the S3 API
            ).decode("ascii")

        try:
            response = self.client.put_object(**kwargs)
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            message = exc.response.get("Error", {}).get("Message", str(exc))
            if retention_days and code in {"InvalidRequest", "InvalidBucketState"}:
                raise StorageUnavailable(
                    f"Object Lock write to '{bucket}' was rejected ({code}): "
                    f"{message}\n"
                    "Two causes are common, and they need different fixes:\n"
                    "  1. Object Lock was not enabled at bucket creation. It "
                    "cannot be added later -- create a new bucket.\n"
                    "  2. The request was missing Content-MD5, which S3 "
                    "requires whenever Object Lock parameters are present.\n"
                    f"Check the bucket's lock state with: "
                    f"aws s3api get-object-lock-configuration --bucket {bucket}"
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
            version_id=response.get("VersionId"),
        )

    def get_version_bytes(self, bucket: str, key: str, version_id: str) -> bytes:
        """Read one specific version, ignoring anything layered over the key.

        This is how a certified asset must be fetched. A plain GET resolves to
        the current version, which a delete marker or a later upload can
        displace; addressing the version id retrieves exactly the bytes that
        were sealed.
        """
        response = self.client.get_object(Bucket=bucket, Key=key, VersionId=version_id)
        return response["Body"].read()

    def version_exists(self, bucket: str, key: str, version_id: str) -> bool:
        try:
            self.client.head_object(Bucket=bucket, Key=key, VersionId=version_id)
            return True
        except ClientError:
            return False

    def presigned_version_url(
        self, bucket: str, key: str, version_id: str, *, expires_in: int = 3600
    ) -> str:
        return self.client.generate_presigned_url(
            "get_object",
            Params={"Bucket": bucket, "Key": key, "VersionId": version_id},
            ExpiresIn=expires_in,
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

    def serve_url(self, bucket: str, key: str, *, expires_in: int = 3600) -> str:
        """A URL a browser can actually fetch, whichever bucket type this is.

        Public bucket with a friendly base configured -> a durable, permanent
        URL. Private bucket -> a presigned URL that expires.

        Callers that need a *stable* reference (a certificate, a library entry)
        must not persist the presigned form; they store the object key and let
        the API mint a fresh URL per request. See
        `GET /api/certificates/{id}/asset`.
        """
        if self.settings.b2_public_vault_base and bucket == self.settings.b2_bucket_vault:
            return self.public_url(bucket, key)
        try:
            return self.presigned_url(bucket, key, expires_in=expires_in)
        except Exception as exc:  # noqa: BLE001
            log.warning("could not presign s3://%s/%s: %s", bucket, key, exc)
            return self.public_url(bucket, key)

    @property
    def vault_is_private(self) -> bool:
        """Whether certified media needs presigning to be fetchable.

        Inferred from configuration rather than probed: a public bucket is only
        usable as one if a friendly URL base is configured, and without that
        base the durable-URL path cannot be built anyway.
        """
        return not bool(self.settings.b2_public_vault_base)

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
