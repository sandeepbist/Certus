"""Private, create-once storage for exact document originals.

PostgreSQL owns workflow and authorization state. This adapter owns the narrow
S3 protocol: bounded SHA-256 hashing, opaque version keys, conditional writes,
exact-version verification, and verified downloads. It intentionally exposes
no delete method to the normal ingestion process.
"""

from __future__ import annotations

import base64
import hashlib
import io
import os
import tempfile
import time
import uuid
from dataclasses import dataclass
from typing import BinaryIO, Iterator, Mapping, Optional

import boto3
from botocore.client import BaseClient
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError


HASH_CHUNK_BYTES = 1024 * 1024
DEFAULT_SPOOL_MEMORY_BYTES = 1024 * 1024
CAPABILITY_PROBE_BYTES = b"certus-object-storage-capability-v1\n"


class ObjectStorageError(RuntimeError):
    """Base failure for the original-object contract."""


class ObjectStorageConfigurationError(ObjectStorageError):
    """The configured provider cannot meet the required contract."""


class ObjectIntegrityError(ObjectStorageError):
    """Stored or downloaded bytes do not match their cataloged digest."""


class ObjectNotFoundError(ObjectStorageError):
    """The exact cataloged object version is absent."""


class ObjectConflictError(ObjectStorageError):
    """A create-once key already contains different bytes."""


class ObjectTooLargeError(ObjectStorageError):
    """The incoming stream exceeded the configured byte limit."""


@dataclass(frozen=True)
class ObjectStorageSettings:
    bucket: str
    region: str = "us-east-1"
    endpoint_url: Optional[str] = None
    access_key: Optional[str] = None
    secret_key: Optional[str] = None
    auto_create_bucket: bool = False
    require_versioning: bool = True
    server_side_encryption: Optional[str] = None
    kms_key_id: Optional[str] = None
    bucket_key_enabled: Optional[bool] = None

    @classmethod
    def from_env(cls) -> "ObjectStorageSettings":
        bucket = os.getenv("OBJECT_STORAGE_BUCKET", "").strip()
        if not bucket:
            raise ObjectStorageConfigurationError("OBJECT_STORAGE_BUCKET is required")

        access_key = os.getenv("OBJECT_STORAGE_ACCESS_KEY") or None
        secret_key = os.getenv("OBJECT_STORAGE_SECRET_KEY") or None
        if bool(access_key) is not bool(secret_key):
            raise ObjectStorageConfigurationError(
                "OBJECT_STORAGE_ACCESS_KEY and OBJECT_STORAGE_SECRET_KEY must be set together"
            )

        encryption = os.getenv("OBJECT_STORAGE_SERVER_SIDE_ENCRYPTION") or None
        kms_key_id = os.getenv("OBJECT_STORAGE_KMS_KEY_ID") or None
        if kms_key_id and encryption != "aws:kms":
            raise ObjectStorageConfigurationError(
                "OBJECT_STORAGE_KMS_KEY_ID requires aws:kms server-side encryption"
            )

        bucket_key_raw = os.getenv("OBJECT_STORAGE_BUCKET_KEY_ENABLED")
        bucket_key_enabled = _optional_bool(bucket_key_raw)
        if bucket_key_enabled is not None and encryption != "aws:kms":
            raise ObjectStorageConfigurationError(
                "OBJECT_STORAGE_BUCKET_KEY_ENABLED requires aws:kms server-side encryption"
            )

        return cls(
            bucket=bucket,
            region=os.getenv("OBJECT_STORAGE_REGION", "us-east-1"),
            endpoint_url=os.getenv("OBJECT_STORAGE_ENDPOINT_URL") or None,
            access_key=access_key,
            secret_key=secret_key,
            auto_create_bucket=_env_bool("OBJECT_STORAGE_AUTO_CREATE_BUCKET", False),
            require_versioning=_env_bool("OBJECT_STORAGE_REQUIRE_VERSIONING", True),
            server_side_encryption=encryption,
            kms_key_id=kms_key_id,
            bucket_key_enabled=bucket_key_enabled,
        )


@dataclass(frozen=True)
class ObjectDigest:
    byte_length: int
    sha256_hex: str
    checksum_sha256_base64: str


@dataclass(frozen=True)
class StoredObject:
    bucket: str
    object_key: str
    object_version_id: str
    byte_length: int
    sha256_hex: str
    checksum_sha256_base64: str
    etag: Optional[str]
    storage_class: Optional[str]
    server_side_encryption: Optional[str]
    kms_key_id: Optional[str]
    bucket_key_enabled: Optional[bool]


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise ObjectStorageConfigurationError(f"{name} must be true or false")


def _optional_bool(raw: Optional[str]) -> Optional[bool]:
    if raw is None or not raw.strip():
        return None
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise ObjectStorageConfigurationError(
        "OBJECT_STORAGE_BUCKET_KEY_ENABLED must be true or false"
    )


def hash_stream(stream: BinaryIO, max_bytes: int) -> ObjectDigest:
    """Hash a seekable stream incrementally and rewind it for the next owner."""

    if max_bytes < 0:
        raise ValueError("max_bytes must be non-negative")
    digest = hashlib.sha256()
    byte_length = 0
    stream.seek(0)
    while True:
        chunk = stream.read(min(HASH_CHUNK_BYTES, max_bytes - byte_length + 1))
        if not chunk:
            break
        byte_length += len(chunk)
        if byte_length > max_bytes:
            stream.seek(0)
            raise ObjectTooLargeError(f"object exceeds the {max_bytes}-byte limit")
        digest.update(chunk)
    stream.seek(0)
    raw_digest = digest.digest()
    return ObjectDigest(
        byte_length=byte_length,
        sha256_hex=raw_digest.hex(),
        checksum_sha256_base64=base64.b64encode(raw_digest).decode("ascii"),
    )


def read_bounded_bytes(stream: BinaryIO, expected_length: int) -> bytes:
    """Read exactly the verified source bytes for parsers that require bytes."""

    stream.seek(0)
    content = stream.read(expected_length + 1)
    stream.seek(0)
    if len(content) != expected_length:
        raise ObjectIntegrityError(
            f"source stream length changed: expected {expected_length}, received {len(content)}"
        )
    return content


def tenant_storage_uuid(tenant_id: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"certus:tenant-storage:{tenant_id}"))


def original_object_key(
    tenant_id: str,
    document_id: str,
    document_version_id: str,
    sha256_hex: str,
) -> str:
    document_uuid = str(uuid.UUID(document_id))
    version_uuid = str(uuid.UUID(document_version_id))
    if len(sha256_hex) != 64 or any(character not in "0123456789abcdef" for character in sha256_hex):
        raise ValueError("sha256_hex must be a lowercase SHA-256 digest")
    return (
        f"originals/v1/{tenant_storage_uuid(tenant_id)}/"
        f"{document_uuid}/{version_uuid}/{sha256_hex}"
    )


def pdf_layout_object_key(
    tenant_id: str,
    document_id: str,
    document_version_id: str,
    source_sha256_hex: str,
    canonical_layout_sha256_hex: str,
) -> str:
    """Return an opaque deterministic key for one immutable PDF layout graph."""

    document_uuid = str(uuid.UUID(document_id))
    version_uuid = str(uuid.UUID(document_version_id))
    for name, digest in (
        ("source_sha256_hex", source_sha256_hex),
        ("canonical_layout_sha256_hex", canonical_layout_sha256_hex),
    ):
        if len(digest) != 64 or any(
            character not in "0123456789abcdef" for character in digest
        ):
            raise ValueError(f"{name} must be a lowercase SHA-256 digest")
    return (
        f"layouts/pdf/v1/{tenant_storage_uuid(tenant_id)}/"
        f"{document_uuid}/{version_uuid}/{source_sha256_hex}/"
        f"{canonical_layout_sha256_hex}.json.gz"
    )


class OriginalObjectStorage:
    """Long-lived per-process S3 client implementing the Certus object contract."""

    def __init__(
        self,
        settings: ObjectStorageSettings,
        client: Optional[BaseClient] = None,
    ) -> None:
        self.settings = settings
        self.client = client or self._build_client(settings)
        self._readiness_client = client or self._build_client(
            settings,
            connect_timeout=1,
            read_timeout=1,
            max_attempts=1,
        )

    def close(self) -> None:
        """Close both process-owned botocore connection pools exactly once."""

        closed: set[int] = set()
        for client in (self.client, self._readiness_client):
            if id(client) in closed:
                continue
            closed.add(id(client))
            close = getattr(client, "close", None)
            if callable(close):
                close()

    @staticmethod
    def _build_client(
        settings: ObjectStorageSettings,
        *,
        connect_timeout: int = 3,
        read_timeout: int = 120,
        max_attempts: int = 3,
    ) -> BaseClient:
        config = Config(
            signature_version="s3v4",
            connect_timeout=connect_timeout,
            read_timeout=read_timeout,
            max_pool_connections=32,
            tcp_keepalive=True,
            retries={"mode": "standard", "total_max_attempts": max_attempts},
            request_checksum_calculation="when_supported",
            response_checksum_validation="when_supported",
            s3={"addressing_style": "path" if settings.endpoint_url else "auto"},
        )
        kwargs = {
            "service_name": "s3",
            "region_name": settings.region,
            "endpoint_url": settings.endpoint_url,
            "config": config,
        }
        if settings.access_key and settings.secret_key:
            kwargs.update(
                aws_access_key_id=settings.access_key,
                aws_secret_access_key=settings.secret_key,
            )
        return boto3.client(**kwargs)

    def check_readiness(self) -> None:
        """Boundedly verify the exact capability object without mutating storage."""

        digest = ObjectDigest(
            byte_length=len(CAPABILITY_PROBE_BYTES),
            sha256_hex=hashlib.sha256(CAPABILITY_PROBE_BYTES).hexdigest(),
            checksum_sha256_base64=base64.b64encode(
                hashlib.sha256(CAPABILITY_PROBE_BYTES).digest()
            ).decode("ascii"),
        )
        key = f"system/capabilities/v1/{digest.sha256_hex}"
        try:
            self._readiness_client.head_bucket(Bucket=self.settings.bucket)
            if self.settings.require_versioning:
                versioning = self._readiness_client.get_bucket_versioning(
                    Bucket=self.settings.bucket
                )
                if versioning.get("Status") != "Enabled":
                    raise ObjectStorageConfigurationError(
                        "object-storage bucket versioning is not enabled"
                    )
            head = self._readiness_client.head_object(
                Bucket=self.settings.bucket,
                Key=key,
                ChecksumMode="ENABLED",
            )
        except ObjectStorageError:
            raise
        except (ClientError, BotoCoreError) as error:
            raise ObjectStorageError("object-storage readiness probe failed") from error
        if head.get("ContentLength") != digest.byte_length:
            raise ObjectIntegrityError("object-storage capability length is invalid")
        if head.get("ChecksumSHA256") != digest.checksum_sha256_base64:
            raise ObjectIntegrityError("object-storage capability checksum is invalid")
        if self.settings.require_versioning and not head.get("VersionId"):
            raise ObjectStorageConfigurationError(
                "object-storage capability has no version identifier"
            )

    def ensure_capabilities(self) -> StoredObject:
        """Verify bucket/versioning plus conditional checksum round-trip."""

        self._ensure_bucket()
        digest = ObjectDigest(
            byte_length=len(CAPABILITY_PROBE_BYTES),
            sha256_hex=hashlib.sha256(CAPABILITY_PROBE_BYTES).hexdigest(),
            checksum_sha256_base64=base64.b64encode(
                hashlib.sha256(CAPABILITY_PROBE_BYTES).digest()
            ).decode("ascii"),
        )
        key = f"system/capabilities/v1/{digest.sha256_hex}"
        return self.put_create_once(
            key,
            io.BytesIO(CAPABILITY_PROBE_BYTES),
            digest,
            "application/octet-stream",
            {"certus-schema": "original-object-v1", "certus-purpose": "capability-probe"},
        )

    def _ensure_bucket(self) -> None:
        try:
            self.client.head_bucket(Bucket=self.settings.bucket)
        except ClientError as error:
            code = _error_code(error)
            if not self.settings.auto_create_bucket or code not in {"404", "NoSuchBucket", "NotFound"}:
                raise ObjectStorageConfigurationError(
                    f"object-storage bucket is unavailable ({code})"
                ) from error
            create_kwargs = {"Bucket": self.settings.bucket}
            if self.settings.region != "us-east-1" and not self.settings.endpoint_url:
                create_kwargs["CreateBucketConfiguration"] = {
                    "LocationConstraint": self.settings.region
                }
            self.client.create_bucket(**create_kwargs)

        if not self.settings.require_versioning:
            return
        versioning = self.client.get_bucket_versioning(Bucket=self.settings.bucket)
        if versioning.get("Status") == "Enabled":
            return
        if not self.settings.auto_create_bucket:
            raise ObjectStorageConfigurationError("object-storage bucket versioning is not enabled")
        self.client.put_bucket_versioning(
            Bucket=self.settings.bucket,
            VersioningConfiguration={"Status": "Enabled"},
        )
        versioning = self.client.get_bucket_versioning(Bucket=self.settings.bucket)
        if versioning.get("Status") != "Enabled":
            raise ObjectStorageConfigurationError(
                "object-storage provider did not enable bucket versioning"
            )

    def put_create_once(
        self,
        object_key: str,
        stream: BinaryIO,
        digest: ObjectDigest,
        content_type: str,
        metadata: Mapping[str, str],
    ) -> StoredObject:
        put_kwargs = {
            "Bucket": self.settings.bucket,
            "Key": object_key,
            "Body": stream,
            "ContentLength": digest.byte_length,
            "ContentType": content_type,
            "ChecksumAlgorithm": "SHA256",
            "ChecksumSHA256": digest.checksum_sha256_base64,
            "IfNoneMatch": "*",
            "Metadata": dict(metadata),
        }
        if self.settings.server_side_encryption:
            put_kwargs["ServerSideEncryption"] = self.settings.server_side_encryption
        if self.settings.kms_key_id:
            put_kwargs["SSEKMSKeyId"] = self.settings.kms_key_id
        if self.settings.bucket_key_enabled is not None:
            put_kwargs["BucketKeyEnabled"] = self.settings.bucket_key_enabled

        last_error: Optional[Exception] = None
        for attempt in range(3):
            stream.seek(0)
            try:
                response = self.client.put_object(**put_kwargs)
                version_id = response.get("VersionId")
                if self.settings.require_versioning and not version_id:
                    raise ObjectStorageConfigurationError(
                        "object-storage provider did not return a version identifier"
                    )
                return self.verify_exact(
                    object_key,
                    str(version_id or "null"),
                    digest,
                    response,
                )
            except ClientError as error:
                last_error = error
                code = _error_code(error)
                if code in {"409", "Conflict", "ConditionalRequestConflict"} and attempt < 2:
                    time.sleep(0.05 * (2**attempt))
                    continue
                if code in {"412", "PreconditionFailed"}:
                    return self._adopt_existing(object_key, digest)
                adopted = self._try_adopt_after_ambiguous_failure(object_key, digest)
                if adopted:
                    return adopted
                raise ObjectStorageError(f"create-once object write failed ({code})") from error
            except BotoCoreError as error:
                last_error = error
                adopted = self._try_adopt_after_ambiguous_failure(object_key, digest)
                if adopted:
                    return adopted
                raise ObjectStorageError("create-once object write failed") from error
        raise ObjectStorageError("create-once object write exhausted conflict retries") from last_error

    def _adopt_existing(self, object_key: str, digest: ObjectDigest) -> StoredObject:
        try:
            latest = self.client.head_object(
                Bucket=self.settings.bucket,
                Key=object_key,
                ChecksumMode="ENABLED",
            )
        except ClientError as error:
            if _is_not_found(error):
                raise ObjectNotFoundError("create-once object was not found") from error
            raise ObjectStorageError("failed to inspect conditional object conflict") from error
        version_id = latest.get("VersionId")
        if self.settings.require_versioning and not version_id:
            raise ObjectStorageConfigurationError(
                "existing object did not expose a version identifier"
            )
        try:
            return self._stored_from_head(object_key, str(version_id or "null"), digest, latest)
        except ObjectIntegrityError as error:
            raise ObjectConflictError("create-once key contains different bytes") from error

    def recover_existing(self, object_key: str, digest: ObjectDigest) -> StoredObject:
        """Adopt a matching object after a process lost the PUT response."""

        return self._adopt_existing(object_key, digest)

    def _try_adopt_after_ambiguous_failure(
        self,
        object_key: str,
        digest: ObjectDigest,
    ) -> Optional[StoredObject]:
        try:
            return self._adopt_existing(object_key, digest)
        except ObjectConflictError:
            raise
        except ObjectStorageError:
            return None

    def verify_exact(
        self,
        object_key: str,
        object_version_id: str,
        digest: ObjectDigest,
        put_response: Optional[Mapping[str, object]] = None,
    ) -> StoredObject:
        kwargs = {
            "Bucket": self.settings.bucket,
            "Key": object_key,
            "ChecksumMode": "ENABLED",
        }
        if object_version_id != "null":
            kwargs["VersionId"] = object_version_id
        try:
            head = self.client.head_object(**kwargs)
        except ClientError as error:
            if _is_not_found(error):
                raise ObjectNotFoundError("exact object version was not found") from error
            raise ObjectStorageError("exact object verification failed") from error
        return self._stored_from_head(object_key, object_version_id, digest, head, put_response)

    def _stored_from_head(
        self,
        object_key: str,
        object_version_id: str,
        digest: ObjectDigest,
        head: Mapping[str, object],
        put_response: Optional[Mapping[str, object]] = None,
    ) -> StoredObject:
        if head.get("ContentLength") != digest.byte_length:
            raise ObjectIntegrityError("stored object length does not match the upload intent")
        if head.get("ChecksumSHA256") != digest.checksum_sha256_base64:
            raise ObjectIntegrityError("stored object SHA-256 checksum is absent or different")
        if self.settings.require_versioning and head.get("VersionId") != object_version_id:
            raise ObjectIntegrityError("provider returned a different object version")
        response = put_response or head
        return StoredObject(
            bucket=self.settings.bucket,
            object_key=object_key,
            object_version_id=object_version_id,
            byte_length=digest.byte_length,
            sha256_hex=digest.sha256_hex,
            checksum_sha256_base64=digest.checksum_sha256_base64,
            etag=_optional_string(response.get("ETag") or head.get("ETag")),
            storage_class=_optional_string(response.get("StorageClass") or head.get("StorageClass")),
            server_side_encryption=_optional_string(
                response.get("ServerSideEncryption") or head.get("ServerSideEncryption")
            ),
            kms_key_id=_optional_string(response.get("SSEKMSKeyId") or head.get("SSEKMSKeyId")),
            bucket_key_enabled=_optional_bool_value(
                response.get("BucketKeyEnabled", head.get("BucketKeyEnabled"))
            ),
        )

    def download_verified_to_spool(
        self,
        stored: StoredObject,
        spool_memory_bytes: int = DEFAULT_SPOOL_MEMORY_BYTES,
    ) -> BinaryIO:
        kwargs = {
            "Bucket": stored.bucket,
            "Key": stored.object_key,
            "ChecksumMode": "ENABLED",
        }
        if stored.object_version_id != "null":
            kwargs["VersionId"] = stored.object_version_id
        try:
            response = self.client.get_object(**kwargs)
        except ClientError as error:
            if _is_not_found(error):
                raise ObjectNotFoundError("exact original object version was not found") from error
            raise ObjectStorageError("original object download failed") from error

        spool = tempfile.SpooledTemporaryFile(max_size=spool_memory_bytes, mode="w+b")
        digest = hashlib.sha256()
        byte_length = 0
        body = response["Body"]
        try:
            for chunk in _iter_body(body):
                byte_length += len(chunk)
                if byte_length > stored.byte_length:
                    raise ObjectIntegrityError("downloaded original exceeded its cataloged length")
                digest.update(chunk)
                spool.write(chunk)
        except Exception:
            spool.close()
            raise
        finally:
            close = getattr(body, "close", None)
            if close:
                close()

        if byte_length != stored.byte_length or digest.hexdigest() != stored.sha256_hex:
            spool.close()
            raise ObjectIntegrityError("downloaded original failed SHA-256 verification")
        spool.seek(0)
        return spool


def _iter_body(body: object) -> Iterator[bytes]:
    iterator = getattr(body, "iter_chunks", None)
    if iterator:
        yield from (chunk for chunk in iterator(chunk_size=HASH_CHUNK_BYTES) if chunk)
        return
    while True:
        chunk = body.read(HASH_CHUNK_BYTES)  # type: ignore[attr-defined]
        if not chunk:
            return
        yield chunk


def _error_code(error: ClientError) -> str:
    return str(error.response.get("Error", {}).get("Code", "unknown"))


def _is_not_found(error: ClientError) -> bool:
    return _error_code(error) in {"404", "NoSuchKey", "NoSuchVersion", "NotFound"}


def _optional_string(value: object) -> Optional[str]:
    return str(value) if value is not None else None


def _optional_bool_value(value: object) -> Optional[bool]:
    return value if isinstance(value, bool) else None
