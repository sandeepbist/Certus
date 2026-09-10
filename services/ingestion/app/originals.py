"""Durable upload-intent persistence for exact document originals."""

from __future__ import annotations

import json
import random
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from typing import Any, Callable, Dict, Optional
from urllib.parse import quote

from psycopg2.extras import RealDictCursor

from services.shared.object_storage import (
    ObjectDigest,
    ObjectStorageSettings,
    OriginalObjectStorage,
    StoredObject,
    original_object_key,
)


class UploadIntentError(RuntimeError):
    pass


class UploadIdempotencyConflict(UploadIntentError):
    pass


class DuplicateDocument(UploadIntentError):
    def __init__(self, detail: Dict[str, Any]):
        super().__init__(str(detail.get("message", "Duplicate document")))
        self.detail = detail


class ReplacementDocumentNotFound(UploadIntentError):
    pass


class WorkspaceQuotaExceeded(UploadIntentError):
    def __init__(
        self,
        *,
        dimension: str,
        usage: int,
        limit: int,
    ) -> None:
        self.detail = {
            "code": "workspace_quota_exceeded",
            "dimension": dimension,
            "usage": usage,
            "limit": limit,
            "message": (
                "This upload would exceed the workspace document limit."
                if dimension == "documents"
                else "This upload would exceed the workspace retained-original storage limit."
            ),
        }
        super().__init__(self.detail["message"])


class UploadIntentAbandoned(UploadIntentError):
    pass


@dataclass(frozen=True)
class UploadIntent:
    id: str
    idempotency_key: str
    tenant_id: str
    user_id: str
    document_id: str
    document_version_id: str
    source_object_id: str
    replace_document_id: Optional[str]
    bucket: str
    object_key: str
    original_filename: str
    claimed_mime_type: str
    byte_length: int
    content_sha256: str
    checksum_sha256_base64: str
    request_metadata: Dict[str, Any]
    status: str
    object_version_id: Optional[str]
    etag: Optional[str]
    storage_class: Optional[str]
    server_side_encryption: Optional[str]
    kms_key_id: Optional[str]
    bucket_key_enabled: Optional[bool]
    object_stored_at: Optional[datetime]
    response_payload: Optional[Dict[str, Any]]

    @classmethod
    def from_row(cls, row: Dict[str, Any]) -> "UploadIntent":
        return cls(
            id=str(row["id"]),
            idempotency_key=row["idempotency_key"],
            tenant_id=row["tenant_id"],
            user_id=row["user_id"],
            document_id=str(row["document_id"]),
            document_version_id=str(row["document_version_id"]),
            source_object_id=str(row["source_object_id"]),
            replace_document_id=(
                str(row["replace_document_id"]) if row["replace_document_id"] else None
            ),
            bucket=row["bucket"],
            object_key=row["object_key"],
            original_filename=row["original_filename"],
            claimed_mime_type=row["claimed_mime_type"],
            byte_length=int(row["byte_length"]),
            content_sha256=row["content_sha256"],
            checksum_sha256_base64=row["checksum_sha256_base64"],
            request_metadata=dict(row["request_metadata"] or {}),
            status=row["status"],
            object_version_id=row["object_version_id"],
            etag=row["etag"],
            storage_class=row["storage_class"],
            server_side_encryption=row["server_side_encryption"],
            kms_key_id=row["kms_key_id"],
            bucket_key_enabled=row["bucket_key_enabled"],
            object_stored_at=row["object_stored_at"],
            response_payload=(
                dict(row["response_payload"]) if row["response_payload"] else None
            ),
        )

    def stored_object(self) -> Optional[StoredObject]:
        if not self.object_version_id or not self.object_stored_at:
            return None
        return StoredObject(
            bucket=self.bucket,
            object_key=self.object_key,
            object_version_id=self.object_version_id,
            byte_length=self.byte_length,
            sha256_hex=self.content_sha256,
            checksum_sha256_base64=self.checksum_sha256_base64,
            etag=self.etag,
            storage_class=self.storage_class,
            server_side_encryption=self.server_side_encryption,
            kms_key_id=self.kms_key_id,
            bucket_key_enabled=self.bucket_key_enabled,
        )


@lru_cache(maxsize=1)
def ready_original_storage() -> OriginalObjectStorage:
    storage = OriginalObjectStorage(ObjectStorageSettings.from_env())
    storage.ensure_capabilities()
    return storage


def close_original_storage() -> None:
    """Close the cached storage pools without constructing them during shutdown."""

    if ready_original_storage.cache_info().currsize:
        try:
            ready_original_storage().close()
        finally:
            ready_original_storage.cache_clear()


def canonical_idempotency_key(value: str) -> str:
    try:
        return str(uuid.UUID(value))
    except (ValueError, AttributeError) as error:
        raise UploadIdempotencyConflict("Idempotency-Key must be a UUID") from error


def original_content_disposition(filename: str, disposition: str = "attachment") -> str:
    if disposition not in {"attachment", "inline"}:
        raise ValueError("original disposition must be attachment or inline")
    ascii_name = "".join(
        character if 32 <= ord(character) < 127 and character not in {'"', '\\'} else "_"
        for character in filename
    ).strip() or "document-original"
    encoded_name = quote(filename, safe="")
    return f"{disposition}; filename=\"{ascii_name}\"; filename*=UTF-8''{encoded_name}"


def original_attachment_header(filename: str) -> str:
    return original_content_disposition(filename, "attachment")


def reserve_upload_intent(
    get_db: Callable[[], Any],
    *,
    idempotency_key: str,
    tenant_id: str,
    user_id: str,
    original_filename: str,
    claimed_mime_type: str,
    digest: ObjectDigest,
    request_metadata: Dict[str, Any],
    replace_document_id: Optional[str],
    bucket: str,
) -> UploadIntent:
    canonical_key = canonical_idempotency_key(idempotency_key)
    with get_db() as connection:
        with connection.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute(
                """
                SELECT *
                FROM document_upload_intents
                WHERE tenant_id = %s AND user_id = %s AND idempotency_key = %s
                FOR UPDATE
                """,
                (tenant_id, user_id, canonical_key),
            )
            existing = cursor.fetchone()
            if existing:
                _assert_same_request(
                    existing,
                    original_filename=original_filename,
                    claimed_mime_type=claimed_mime_type,
                    digest=digest,
                    request_metadata=request_metadata,
                    replace_document_id=replace_document_id,
                    bucket=bucket,
                )
                if existing["status"] == "abandoned":
                    raise UploadIntentAbandoned(
                        "This upload exhausted recovery before storing an object; "
                        "retry with a new Idempotency-Key."
                    )
                if existing["status"] == "error":
                    resumed_status = (
                        "object_stored" if existing["object_version_id"] else "pending_object"
                    )
                    cursor.execute(
                        """
                        UPDATE document_upload_intents
                        SET status = %s, last_error = NULL, available_at = NOW(),
                            locked_at = NULL, lock_owner = NULL, updated_at = NOW()
                        WHERE id = %s
                        RETURNING *
                        """,
                        (resumed_status, str(existing["id"])),
                    )
                    existing = cursor.fetchone()
                return UploadIntent.from_row(existing)

            if replace_document_id:
                cursor.execute(
                    """
                    SELECT document.id, document.current_version_id,
                           version.title, version.mime_type, version.content_hash,
                           version.tags, version.source_time
                    FROM documents AS document
                    JOIN document_versions AS version
                      ON version.id = document.current_version_id
                     AND version.document_id = document.id
                    WHERE document.id = %s
                      AND document.tenant_id = %s AND document.user_id = %s
                      AND document.deleted_at IS NULL
                    """,
                    (replace_document_id, tenant_id, user_id),
                )
                replacement = cursor.fetchone()
                if not replacement:
                    raise ReplacementDocumentNotFound(
                        "Document to replace was not found in this workspace"
                    )
                same_source_record = (
                    replacement["content_hash"] == digest.sha256_hex
                    and replacement["title"] == original_filename
                    and replacement["mime_type"] == claimed_mime_type
                    and list(replacement["tags"] or []) == request_metadata["tags"]
                    and _isoformat(replacement["source_time"]) == request_metadata["source_time"]
                )
                if same_source_record:
                    raise DuplicateDocument(
                        {
                            "code": "duplicate_document_version",
                            "message": "The current document version already has these exact source bytes.",
                            "document_id": str(replacement["id"]),
                            "document_version_id": str(replacement["current_version_id"]),
                        }
                    )
                document_id = str(replacement["id"])
            else:
                cursor.execute(
                    """
                    SELECT document.id, document.current_version_id,
                           matched_version.id AS matched_version_id,
                           matched_version.version_number
                    FROM document_versions AS matched_version
                    JOIN documents AS document
                      ON document.id = matched_version.document_id
                    WHERE matched_version.tenant_id = %s
                      AND matched_version.user_id = %s
                      AND matched_version.content_hash = %s
                      AND document.deleted_at IS NULL
                    ORDER BY
                        (matched_version.id = document.current_version_id) DESC,
                        matched_version.version_number DESC
                    LIMIT 1
                    """,
                    (tenant_id, user_id, digest.sha256_hex),
                )
                duplicate = cursor.fetchone()
                if duplicate:
                    raise DuplicateDocument(
                        {
                            "code": "duplicate_document",
                            "message": "This document has already been ingested in the workspace.",
                            "document_id": str(duplicate["id"]),
                            "document_version_id": str(duplicate["matched_version_id"]),
                            "version_number": int(duplicate["version_number"]),
                        }
                    )
                document_id = str(uuid.uuid4())

            version_id = str(uuid.uuid4())
            source_object_id = str(uuid.uuid4())
            object_key = original_object_key(
                tenant_id,
                document_id,
                version_id,
                digest.sha256_hex,
            )
            cursor.execute(
                """
                INSERT INTO document_upload_intents (
                    idempotency_key, tenant_id, user_id, document_id,
                    document_version_id, source_object_id, replace_document_id,
                    bucket, object_key, original_filename, claimed_mime_type,
                    byte_length, content_sha256, checksum_sha256_base64,
                    request_metadata, quota_document_count,
                    quota_original_bytes
                ) VALUES (
                    %s, %s, %s, %s,
                    %s, %s, %s,
                    %s, %s, %s, %s,
                    %s, %s, %s, %s::jsonb, %s, %s
                )
                ON CONFLICT (tenant_id, user_id, idempotency_key) DO NOTHING
                RETURNING *
                """,
                (
                    canonical_key,
                    tenant_id,
                    user_id,
                    document_id,
                    version_id,
                    source_object_id,
                    replace_document_id,
                    bucket,
                    object_key,
                    original_filename,
                    claimed_mime_type,
                    digest.byte_length,
                    digest.sha256_hex,
                    digest.checksum_sha256_base64,
                    json.dumps(request_metadata),
                    0 if replace_document_id else 1,
                    digest.byte_length,
                ),
            )
            inserted = cursor.fetchone()
            if inserted:
                cursor.execute(
                    "SELECT * FROM reserve_workspace_upload_quota(%s)",
                    (str(inserted["id"]),),
                )
                quota = cursor.fetchone()
                if not quota or not quota["accepted"]:
                    dimension = str(quota["exceeded_dimension"] if quota else "storage")
                    usage_field = (
                        "document_usage" if dimension == "documents" else "storage_usage"
                    )
                    limit_field = (
                        "document_limit" if dimension == "documents" else "storage_limit"
                    )
                    raise WorkspaceQuotaExceeded(
                        dimension=dimension,
                        usage=int(quota[usage_field]) if quota else digest.byte_length,
                        limit=(
                            int(quota[limit_field])
                            if quota and quota[limit_field] is not None
                            else 0
                        ),
                    )
                return UploadIntent.from_row(inserted)

            cursor.execute(
                """
                SELECT *
                FROM document_upload_intents
                WHERE tenant_id = %s AND user_id = %s AND idempotency_key = %s
                FOR UPDATE
                """,
                (tenant_id, user_id, canonical_key),
            )
            concurrent = cursor.fetchone()
            if not concurrent:
                raise UploadIntentError("Concurrent idempotency reservation was lost")
            _assert_same_request(
                concurrent,
                original_filename=original_filename,
                claimed_mime_type=claimed_mime_type,
                digest=digest,
                request_metadata=request_metadata,
                replace_document_id=replace_document_id,
                bucket=bucket,
            )
            if concurrent["status"] == "abandoned":
                raise UploadIntentAbandoned(
                    "This upload exhausted recovery before storing an object; "
                    "retry with a new Idempotency-Key."
                )
            return UploadIntent.from_row(concurrent)


def record_stored_object(
    get_db: Callable[[], Any],
    intent_id: str,
    stored: StoredObject,
) -> UploadIntent:
    with get_db() as connection:
        with connection.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute(
                """
                UPDATE document_upload_intents
                SET status = 'object_stored', object_version_id = %s,
                    etag = %s, storage_class = %s,
                    server_side_encryption = %s, kms_key_id = %s,
                    bucket_key_enabled = %s,
                    object_stored_at = COALESCE(object_stored_at, NOW()),
                    attempt_count = attempt_count + 1,
                    last_error = NULL, locked_at = NULL, lock_owner = NULL,
                    updated_at = NOW()
                WHERE id = %s AND status IN ('pending_object', 'object_stored', 'error')
                RETURNING *
                """,
                (
                    stored.object_version_id,
                    stored.etag,
                    stored.storage_class,
                    stored.server_side_encryption,
                    stored.kms_key_id,
                    stored.bucket_key_enabled,
                    intent_id,
                ),
            )
            row = cursor.fetchone()
            if not row:
                raise UploadIntentError("upload intent can no longer accept an object result")
            return UploadIntent.from_row(row)


def record_upload_error(
    get_db: Callable[[], Any],
    intent_id: str,
    error: Exception,
) -> None:
    sanitized = str(error).strip().replace("\x00", "")[:2000] or error.__class__.__name__
    with get_db() as connection:
        with connection.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute(
                """
                SELECT status, attempt_count, locked_at
                FROM document_upload_intents
                WHERE id = %s
                FOR UPDATE
                """,
                (intent_id,),
            )
            current = cursor.fetchone()
            if not current or current["status"] in {"completed", "abandoned"}:
                return
            if current["status"] == "error" and current["locked_at"] is None:
                return
            next_attempt = int(current["attempt_count"]) + 1
            base_delay = min(3600.0, 5.0 * (2 ** min(next_attempt - 1, 10)))
            available_at = datetime.now(timezone.utc) + timedelta(
                seconds=random.uniform(base_delay * 0.75, base_delay * 1.25)
            )
            cursor.execute(
                """
                UPDATE document_upload_intents
                SET status = 'error', last_error = %s,
                    attempt_count = %s, available_at = %s,
                    locked_at = NULL, lock_owner = NULL,
                    updated_at = NOW()
                WHERE id = %s AND status <> 'completed'
                """,
                (sanitized, next_attempt, available_at, intent_id),
            )


def lease_upload_for_recovery(
    get_db: Callable[[], Any],
    *,
    worker_id: str,
    lease_seconds: int,
    max_attempts: int,
) -> Optional[UploadIntent]:
    with get_db() as connection:
        with connection.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute(
                "SELECT abandon_exhausted_unstored_upload(%s) AS abandoned_id",
                (max_attempts,),
            )
            cursor.execute(
                """
                SELECT id
                FROM document_upload_intents
                WHERE status IN ('pending_object', 'object_stored', 'finalizing', 'error')
                  AND attempt_count < %s
                  AND available_at <= NOW()
                  AND (
                       status <> 'pending_object'
                       OR created_at <= NOW() - INTERVAL '5 seconds'
                  )
                  AND (
                       locked_at IS NULL
                       OR locked_at < NOW() - make_interval(secs => %s)
                  )
                ORDER BY available_at, created_at, id
                FOR UPDATE SKIP LOCKED
                LIMIT 1
                """,
                (max_attempts, lease_seconds),
            )
            candidate = cursor.fetchone()
            if not candidate:
                return None
            cursor.execute(
                """
                UPDATE document_upload_intents
                SET locked_at = NOW(), lock_owner = %s, updated_at = NOW()
                WHERE id = %s
                RETURNING *
                """,
                (worker_id, str(candidate["id"])),
            )
            return UploadIntent.from_row(cursor.fetchone())


def _assert_same_request(
    row: Dict[str, Any],
    *,
    original_filename: str,
    claimed_mime_type: str,
    digest: ObjectDigest,
    request_metadata: Dict[str, Any],
    replace_document_id: Optional[str],
    bucket: str,
) -> None:
    stored_replace_id = str(row["replace_document_id"]) if row["replace_document_id"] else None
    if (
        row["original_filename"] != original_filename
        or row["claimed_mime_type"] != claimed_mime_type
        or int(row["byte_length"]) != digest.byte_length
        or row["content_sha256"] != digest.sha256_hex
        or row["checksum_sha256_base64"] != digest.checksum_sha256_base64
        or dict(row["request_metadata"] or {}) != request_metadata
        or stored_replace_id != replace_document_id
        or row["bucket"] != bucket
    ):
        raise UploadIdempotencyConflict(
            "Idempotency-Key was already used for a different upload request"
        )


def _isoformat(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat() if value else None
