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


def _evaluation_summary(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict) or not value:
        return None
    return {
        "decision": value.get("decision"),
        "gates_passed": value.get("gates_passed"),
        "baseline_fingerprint": value.get("baseline_fingerprint"),
        "candidate_fingerprint": value.get("candidate_fingerprint"),
    }


def _generation_payload(row: Any) -> dict[str, Any]:
    payload = dict(row)
    expected = int(payload["expected_chunk_count"])
    embedded = int(payload["embedded_chunk_count"])
    failed = int(payload["failed_chunk_count"])
    return {
        "id": str(payload["id"]),
        "embedding_profile": payload["embedding_profile"],
        "source_corpus_revision": int(payload["source_corpus_revision"]),
        "status": payload["status"],
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
    }


GENERATION_SELECT = """
    SELECT id, embedding_profile, source_corpus_revision, status,
           expected_chunk_count, embedded_chunk_count, failed_chunk_count,
           previous_generation_id, evaluation_report, last_error,
           created_at, updated_at, sealed_at, activated_at, retired_at,
           stale_at, rollback_until, retain_until
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
            if status is not None:
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
        if status is not None:
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
            generation_id = str(cursor.fetchone()["generation_id"])
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


@router.post("/{generation_id}/rollback")
def rollback_embedding_generation(
    generation_id: uuid.UUID,
    identity: RequestIdentity = Depends(require_request_identity),
):
    with get_db_cursor() as cursor:
        cursor.execute(
            """
            SELECT status
            FROM workspace_embedding_generations
            WHERE id = %s AND tenant_id = %s AND user_id = %s
            FOR UPDATE
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
