import uuid
from typing import Any, Literal

import psycopg2
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, field_validator

from app.core.db import get_db_cursor
from app.core.identity import RequestIdentity, require_request_identity
from services.shared.embeddings import (
    SUPPORTED_SERVING_EMBEDDING_PROFILES,
    parse_embedding_profile,
)


router = APIRouter(prefix="/embedding-generations", tags=["Embedding Generations"])

GenerationStatus = Literal[
    "building",
    "paused",
    "ready",
    "active",
    "retired",
    "stale",
    "failed",
    "cancelled",
    "rolled_back",
]


class StartEmbeddingGenerationRequest(BaseModel):
    embedding_profile: str = Field(min_length=1, max_length=255)

    @field_validator("embedding_profile")
    @classmethod
    def supported_profile(cls, value: str) -> str:
        identifier = parse_embedding_profile(value.strip()).identifier
        if identifier not in SUPPORTED_SERVING_EMBEDDING_PROFILES:
            raise ValueError("Embedding profile is not supported for serving")
        return identifier


class ActivateEmbeddingGenerationRequest(BaseModel):
    rollback_window_hours: int = Field(default=168, ge=1, le=720)


class PauseEmbeddingGenerationRequest(BaseModel):
    reason: str | None = Field(default=None, max_length=500)

    @field_validator("reason")
    @classmethod
    def normalize_reason(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return value.strip() or None


def _evaluation_summary(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict) or not value:
        return None
    return {
        "schema_version": value.get("schema_version"),
        "evaluation_profile": value.get("evaluation_profile"),
        "evaluation_source": value.get("evaluation_source"),
        "decision": value.get("decision"),
        "gates_passed": value.get("gates_passed"),
        "quality_claim": value.get("quality_claim"),
        "baseline_fingerprint": value.get("baseline_fingerprint"),
        "candidate_fingerprint": value.get("candidate_fingerprint"),
        "metrics": value.get("metrics") if isinstance(value.get("metrics"), dict) else None,
    }


def _generation_payload(row: Any) -> dict[str, Any]:
    payload = dict(row)
    expected = int(payload["expected_chunk_count"])
    embedded = int(payload["embedded_chunk_count"])
    failed = int(payload["failed_chunk_count"])
    return {
        "id": str(payload["id"]),
        "embedding_profile": payload["embedding_profile"],
        "creation_reason": payload["creation_reason"],
        "source_corpus_revision": int(payload["source_corpus_revision"]),
        "corpus": {
            "snapshot_revision": int(payload["source_corpus_revision"]),
            "current_revision": int(payload["current_corpus_revision"]),
            "is_current": (
                int(payload["source_corpus_revision"])
                == int(payload["current_corpus_revision"])
            ),
        },
        "status": "paused" if payload["is_paused"] else payload["status"],
        "is_paused": bool(payload["is_paused"]),
        "progress": {
            "expected": expected,
            "embedded": embedded,
            "failed": failed,
            "remaining": max(0, expected - embedded),
        },
        "previous_generation_id": (
            str(payload["previous_generation_id"])
            if payload.get("previous_generation_id") is not None
            else None
        ),
        "evaluation": _evaluation_summary(payload.get("evaluation_report")),
        "has_error": bool(payload.get("last_error")),
        "created_at": payload["created_at"],
        "updated_at": payload["updated_at"],
        "sealed_at": payload.get("sealed_at"),
        "activated_at": payload.get("activated_at"),
        "retired_at": payload.get("retired_at"),
        "stale_at": payload.get("stale_at"),
        "rollback_until": payload.get("rollback_until"),
        "retain_until": payload.get("retain_until"),
        "last_paused_at": payload.get("last_paused_at"),
        "last_resumed_at": payload.get("last_resumed_at"),
        "last_pause_reason": payload.get("last_pause_reason"),
    }


GENERATION_SELECT = """
    SELECT id, embedding_profile, creation_reason, source_corpus_revision, status,
           expected_chunk_count, embedded_chunk_count, failed_chunk_count,
           previous_generation_id, evaluation_report, last_error,
           created_at, updated_at, sealed_at, activated_at, retired_at,
           stale_at, rollback_until, retain_until, is_paused,
           last_paused_at, last_resumed_at, last_pause_reason,
           COALESCE((
               SELECT revision
               FROM workspace_embedding_corpus_revisions AS corpus
               WHERE corpus.tenant_id = workspace_embedding_generations.tenant_id
                 AND corpus.user_id = workspace_embedding_generations.user_id
           ), source_corpus_revision) AS current_corpus_revision
    FROM workspace_embedding_generations
"""


@router.get("")
def list_embedding_generations(
    status: GenerationStatus | None = Query(default=None),
    limit: int = Query(default=25, ge=1, le=100),
    page_cursor: uuid.UUID | None = Query(default=None, alias="cursor"),
    identity: RequestIdentity = Depends(require_request_identity),
):
    with get_db_cursor() as cursor:
        if page_cursor is not None:
            anchor_filters = ["id = %s", "tenant_id = %s", "user_id = %s"]
            anchor_params: list[object] = [
                str(page_cursor),
                identity.tenant_id,
                identity.user_id,
            ]
            if status == "paused":
                anchor_filters.append("status = 'building' AND is_paused = true")
            elif status == "building":
                anchor_filters.append("status = 'building' AND is_paused = false")
            elif status is not None:
                anchor_filters.append("status = %s")
                anchor_params.append(status)
            cursor.execute(
                f"""
                SELECT created_at, id
                FROM workspace_embedding_generations
                WHERE {' AND '.join(anchor_filters)}
                """,
                anchor_params,
            )
            anchor = cursor.fetchone()
            if not anchor:
                raise HTTPException(
                    status_code=422,
                    detail="The embedding generation page cursor is invalid.",
                )
        else:
            anchor = None

        filters = ["tenant_id = %s", "user_id = %s"]
        params: list[object] = [identity.tenant_id, identity.user_id]
        if status == "paused":
            filters.append("status = 'building' AND is_paused = true")
        elif status == "building":
            filters.append("status = 'building' AND is_paused = false")
        elif status is not None:
            filters.append("status = %s")
            params.append(status)
        if anchor is not None:
            filters.append("(created_at, id) < (%s, %s)")
            params.extend([anchor["created_at"], anchor["id"]])
        params.append(limit + 1)
        cursor.execute(
            f"""
            {GENERATION_SELECT}
            WHERE {' AND '.join(filters)}
            ORDER BY created_at DESC, id DESC
            LIMIT %s
            """,
            params,
        )
        rows = cursor.fetchall()
    has_more = len(rows) > limit
    items = [_generation_payload(row) for row in rows[:limit]]
    return {
        "generations": items,
        "supported_profiles": sorted(SUPPORTED_SERVING_EMBEDDING_PROFILES),
        "pagination": {
            "limit": limit,
            "next_cursor": items[-1]["id"] if has_more else None,
        },
    }


@router.get("/{generation_id}")
def get_embedding_generation(
    generation_id: uuid.UUID,
    identity: RequestIdentity = Depends(require_request_identity),
):
    with get_db_cursor() as cursor:
        cursor.execute(
            f"""
            {GENERATION_SELECT}
            WHERE id = %s AND tenant_id = %s AND user_id = %s
            """,
            (str(generation_id), identity.tenant_id, identity.user_id),
        )
        row = cursor.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Embedding generation not found")
    return {"generation": _generation_payload(row)}


@router.post("", status_code=202)
def start_embedding_generation(
    request: StartEmbeddingGenerationRequest,
    identity: RequestIdentity = Depends(require_request_identity),
):
    capacity = None
    try:
        with get_db_cursor() as cursor:
            cursor.execute(
                """
                SELECT start_workspace_embedding_generation(
                    %s, %s, %s
                ) AS generation_id
                """,
                (
                    identity.tenant_id,
                    identity.user_id,
                    request.embedding_profile,
                ),
            )
            generation_result = cursor.fetchone()
            generation_id = generation_result["generation_id"]
            if generation_id is None:
                cursor.execute(
                    """
                    SELECT eligible_chunk_count, chunk_limit
                    FROM inspect_workspace_embedding_generation_capacity(%s, %s)
                    """,
                    (identity.tenant_id, identity.user_id),
                )
                capacity = cursor.fetchone()
                row = None
            else:
                generation_id = str(generation_id)
                cursor.execute(
                    f"""
                    {GENERATION_SELECT}
                    WHERE id = %s AND tenant_id = %s AND user_id = %s
                    """,
                    (generation_id, identity.tenant_id, identity.user_id),
                )
                row = cursor.fetchone()
    except psycopg2.errors.UniqueViolation as error:
        raise HTTPException(
            status_code=409,
            detail="A build for this embedding profile is already open.",
        ) from error
    if capacity is not None:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "embedding_generation_quota_exceeded",
                "dimension": "chunks",
                "usage": int(capacity["eligible_chunk_count"]),
                "limit": (
                    int(capacity["chunk_limit"])
                    if capacity["chunk_limit"] is not None
                    else None
                ),
                "message": (
                    "This workspace corpus exceeds the configured "
                    "embedding-generation chunk limit."
                ),
            },
        )
    if not row:
        raise HTTPException(status_code=500, detail="Embedding generation was not created")
    return {"generation": _generation_payload(row)}


@router.post("/{generation_id}/cancel")
def cancel_embedding_generation(
    generation_id: uuid.UUID,
    identity: RequestIdentity = Depends(require_request_identity),
):
    with get_db_cursor() as cursor:
        cursor.execute(
            f"""
            UPDATE workspace_embedding_generations
            SET status = 'cancelled',
                is_paused = false,
                last_error = 'Cancelled by a workspace operator.',
                rollback_until = NULL,
                updated_at = NOW()
            WHERE id = %s AND tenant_id = %s AND user_id = %s
              AND status IN ('building', 'ready')
            RETURNING id
            """,
            (str(generation_id), identity.tenant_id, identity.user_id),
        )
        cancelled = cursor.fetchone()
        if cancelled:
            return {"generation_id": str(cancelled["id"]), "status": "cancelled"}
        cursor.execute(
            """
            SELECT status FROM workspace_embedding_generations
            WHERE id = %s AND tenant_id = %s AND user_id = %s
            """,
            (str(generation_id), identity.tenant_id, identity.user_id),
        )
        current = cursor.fetchone()
    if not current:
        raise HTTPException(status_code=404, detail="Embedding generation not found")
    raise HTTPException(
        status_code=409,
        detail=f"A {current['status']} generation cannot be cancelled.",
    )


@router.post("/{generation_id}/pause")
def pause_embedding_generation(
    generation_id: uuid.UUID,
    request: PauseEmbeddingGenerationRequest,
    identity: RequestIdentity = Depends(require_request_identity),
):
    with get_db_cursor() as cursor:
        cursor.execute(
            """
            SELECT pause_workspace_embedding_generation(%s, %s, %s, %s) AS paused
            """,
            (
                str(generation_id),
                identity.tenant_id,
                identity.user_id,
                request.reason,
            ),
        )
        paused = bool(cursor.fetchone()["paused"])
        if paused:
            return {"generation_id": str(generation_id), "status": "paused"}
        cursor.execute(
            """
            SELECT status, is_paused
            FROM workspace_embedding_generations
            WHERE id = %s AND tenant_id = %s AND user_id = %s
            """,
            (str(generation_id), identity.tenant_id, identity.user_id),
        )
        current = cursor.fetchone()
    if not current:
        raise HTTPException(status_code=404, detail="Embedding generation not found")
    current_status = "paused" if current["is_paused"] else current["status"]
    raise HTTPException(
        status_code=409,
        detail=f"A {current_status} generation cannot be paused.",
    )


@router.post("/{generation_id}/resume")
def resume_embedding_generation(
    generation_id: uuid.UUID,
    identity: RequestIdentity = Depends(require_request_identity),
):
    with get_db_cursor() as cursor:
        cursor.execute(
            """
            SELECT resume_workspace_embedding_generation(%s, %s, %s) AS resumed
            """,
            (str(generation_id), identity.tenant_id, identity.user_id),
        )
        resumed = bool(cursor.fetchone()["resumed"])
        if resumed:
            return {"generation_id": str(generation_id), "status": "building"}
        cursor.execute(
            """
            SELECT status, is_paused
            FROM workspace_embedding_generations
            WHERE id = %s AND tenant_id = %s AND user_id = %s
            """,
            (str(generation_id), identity.tenant_id, identity.user_id),
        )
        current = cursor.fetchone()
    if not current:
        raise HTTPException(status_code=404, detail="Embedding generation not found")
    current_status = "paused" if current["is_paused"] else current["status"]
    raise HTTPException(
        status_code=409,
        detail=(
            f"A {current_status} generation cannot be resumed; "
            "its corpus may have changed."
        ),
    )


@router.post("/{generation_id}/activate")
def activate_embedding_generation(
    generation_id: uuid.UUID,
    request: ActivateEmbeddingGenerationRequest,
    identity: RequestIdentity = Depends(require_request_identity),
):
    with get_db_cursor() as cursor:
        cursor.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s || chr(31) || %s, 0))",
            (identity.tenant_id, identity.user_id),
        )
        cursor.execute(
            """
            SELECT status
            FROM workspace_embedding_generations
            WHERE id = %s AND tenant_id = %s AND user_id = %s
            """,
            (str(generation_id), identity.tenant_id, identity.user_id),
        )
        current = cursor.fetchone()
        if not current:
            raise HTTPException(status_code=404, detail="Embedding generation not found")
        if current["status"] != "ready":
            raise HTTPException(
                status_code=409,
                detail="Only a database-qualified ready generation can be activated.",
            )
        cursor.execute(
            """
            SELECT activate_workspace_embedding_generation(
                %s, make_interval(hours => %s)
            ) AS activated
            """,
            (str(generation_id), request.rollback_window_hours),
        )
        activated = bool(cursor.fetchone()["activated"])
    if not activated:
        raise HTTPException(
            status_code=409,
            detail=(
                "The generation no longer satisfies its qualification, corpus, "
                "or activation contract."
            ),
        )
    return {
        "generation_id": str(generation_id),
        "status": "active",
        "rollback_window_hours": request.rollback_window_hours,
    }


@router.post("/{generation_id}/rollback")
def rollback_embedding_generation(
    generation_id: uuid.UUID,
    identity: RequestIdentity = Depends(require_request_identity),
):
    with get_db_cursor() as cursor:
        cursor.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s || chr(31) || %s, 0))",
            (identity.tenant_id, identity.user_id),
        )
        cursor.execute(
            """
            SELECT status
            FROM workspace_embedding_generations
            WHERE id = %s AND tenant_id = %s AND user_id = %s
            """,
            (str(generation_id), identity.tenant_id, identity.user_id),
        )
        current = cursor.fetchone()
        if not current:
            raise HTTPException(status_code=404, detail="Embedding generation not found")
        if current["status"] != "active":
            raise HTTPException(
                status_code=409,
                detail="Only an active generation can be rolled back.",
            )
        cursor.execute(
            """
            SELECT rollback_workspace_embedding_generation(%s) AS rolled_back
            """,
            (str(generation_id),),
        )
        rolled_back = bool(cursor.fetchone()["rolled_back"])
    if not rolled_back:
        raise HTTPException(
            status_code=409,
            detail="The rollback window or retained predecessor is no longer valid.",
        )
    return {"generation_id": str(generation_id), "status": "rolled_back"}
