import re
import time
import uuid
from collections import defaultdict
from typing import Literal

import psycopg2
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, field_validator

from app.core.db import get_db_cursor
from app.core.identity import RequestIdentity, require_request_identity
from app.retrieval.hybrid import embed_query
from services.shared.embedding_registry import register_embedding_profile

router = APIRouter(prefix="", tags=["Long-Term Memory & Reflection"])

class MemoryCreate(BaseModel):
    fact: str = Field(min_length=1, max_length=2_000)
    category: Literal["preference", "fact", "constraint", "goal", "project", "person", "deadline", "concept"] = "preference"
    confidence: float = Field(default=1.0, ge=0, le=1)
    confirm_sensitive: bool = False

    @field_validator("fact")
    @classmethod
    def clean_fact(cls, value: str) -> str:
        clean_value = " ".join(value.split())
        if not clean_value:
            raise ValueError("Memory fact cannot be blank")
        return clean_value


SENSITIVE_PATTERNS = {
    "social_security_number": re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
    "payment_card_number": re.compile(r"\b(?:\d[ -]*?){13,19}\b"),
}


def sensitive_fact_types(fact: str) -> list[str]:
    return [name for name, pattern in SENSITIVE_PATTERNS.items() if pattern.search(fact)]

@router.get("/memories")
def list_memories(
    limit: int = Query(default=50, ge=1, le=100),
    page_cursor: uuid.UUID | None = Query(default=None, alias="cursor"),
    identity: RequestIdentity = Depends(require_request_identity),
):
    with get_db_cursor() as cursor:
        if page_cursor is not None:
            cursor.execute(
                """
                SELECT confidence, created_at, id
                FROM memories
                WHERE id = %s AND tenant_id = %s AND user_id = %s
                  AND is_active = true
                """,
                (str(page_cursor), identity.tenant_id, identity.user_id),
            )
            anchor = cursor.fetchone()
            if not anchor:
                raise HTTPException(status_code=422, detail="The memory page cursor is invalid.")
        else:
            anchor = None

        params: list[object] = [identity.tenant_id, identity.user_id]
        page_filter = ""
        if anchor:
            page_filter = "AND (confidence, created_at, id) < (%s, %s, %s)"
            params.extend([anchor["confidence"], anchor["created_at"], anchor["id"]])
        params.append(limit + 1)
        cursor.execute(
            """
            SELECT id, fact, category, confidence, access_count, is_active,
                   embedding_provider, embedding_profile, created_at, last_accessed_at
            FROM memories
            WHERE tenant_id = %s AND user_id = %s AND is_active = true
              {page_filter}
            ORDER BY confidence DESC, created_at DESC, id DESC
            LIMIT %s
            """.format(page_filter=page_filter),
            params,
        )
        rows = cursor.fetchall()
        has_more = len(rows) > limit
        memories = [dict(memory) for memory in rows[:limit]]
        return {
            "memories": memories,
            "pagination": {
                "limit": limit,
                "next_cursor": str(memories[-1]["id"]) if has_more else None,
            },
        }

@router.post("/memories")
def create_memory(
    mem: MemoryCreate,
    identity: RequestIdentity = Depends(require_request_identity),
):
    sensitive_types = sensitive_fact_types(mem.fact)
    if sensitive_types and not mem.confirm_sensitive:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "sensitive_memory_confirmation_required",
                "message": "This fact may contain sensitive personal data. Confirm before storing it.",
                "detected_types": sensitive_types,
            },
        )

    embedding_result = embed_query(mem.fact)
    vec_str = f"[{','.join(str(v) for v in embedding_result.vector)}]"
    memory_id = str(uuid.uuid4())

    try:
        with get_db_cursor() as cursor:
            register_embedding_profile(cursor, embedding_result.profile)
            cursor.execute(
                """
                SELECT COUNT(*) AS total
                FROM memories
                WHERE tenant_id = %s AND user_id = %s AND is_active = true
                """,
                (identity.tenant_id, identity.user_id),
            )
            if cursor.fetchone()["total"] >= 1_000:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "memory_cap_reached",
                        "message": "The active memory limit of 1,000 facts has been reached.",
                    },
                )

            cursor.execute(
                """
                INSERT INTO memories (
                    id, user_id, tenant_id, fact, category, confidence,
                    embedding, embedding_provider, embedding_profile,
                    is_active, access_count, created_at
                ) VALUES (
                    %s, %s, %s, %s, %s, %s,
                    %s::vector, %s, %s, true, 0, NOW()
                )
                RETURNING id, fact, category, confidence, access_count, is_active,
                          embedding_provider, embedding_profile, created_at,
                          last_accessed_at
                """,
                (
                    memory_id,
                    identity.user_id,
                    identity.tenant_id,
                    mem.fact,
                    mem.category,
                    mem.confidence,
                    vec_str,
                    embedding_result.provider_model,
                    embedding_result.profile.identifier,
                ),
            )
            memory = dict(cursor.fetchone())
    except psycopg2.errors.UniqueViolation as error:
        raise HTTPException(
            status_code=409,
            detail={"code": "duplicate_memory", "message": "This active memory already exists."},
        ) from error

    return {"memory": memory, "status": "stored"}

@router.delete("/memories/{memory_id}")
def delete_memory(
    memory_id: str,
    identity: RequestIdentity = Depends(require_request_identity),
):
    with get_db_cursor(dict_cursor=False) as cursor:
        cursor.execute(
            """
            UPDATE memories SET is_active = false
            WHERE id = %s AND tenant_id = %s AND user_id = %s
            RETURNING id
            """,
            (memory_id, identity.tenant_id, identity.user_id),
        )
        if not cursor.fetchone():
            raise HTTPException(status_code=404, detail="Memory not found")
    return {"deleted": True, "memory_id": memory_id}

@router.post("/memories/reflect")
def reflect_memories(identity: RequestIdentity = Depends(require_request_identity)):
    with get_db_cursor() as cursor:
        cursor.execute(
            """
            SELECT id
            FROM memories
            WHERE tenant_id = %s AND user_id = %s AND is_active = true
            ORDER BY created_at DESC
            LIMIT 1000
            """,
            (identity.tenant_id, identity.user_id),
        )
        memory_ids = [str(row["id"]) for row in cursor.fetchall()]
        if not memory_ids:
            return {
                "status": "reflection_complete",
                "memories_examined": 0,
                "clusters_formed": 0,
                "related_pairs": 0,
                "timestamp": time.time(),
            }

        cursor.execute(
            """
            SELECT source.id AS source_id,
                   nearest.id AS related_id,
                   1.0 - (source.embedding <=> nearest.embedding) AS similarity
            FROM memories source
            JOIN LATERAL (
                SELECT candidate.id, candidate.embedding
                FROM memories candidate
                WHERE candidate.tenant_id = source.tenant_id
                  AND candidate.user_id = source.user_id
                  AND candidate.is_active = true
                  AND candidate.embedding IS NOT NULL
                  AND candidate.embedding_profile = source.embedding_profile
                  AND candidate.id <> source.id
                  AND candidate.category = source.category
                ORDER BY candidate.embedding <=> source.embedding
                LIMIT 1
            ) nearest ON true
            WHERE source.tenant_id = %s
              AND source.user_id = %s
              AND source.is_active = true
              AND source.embedding IS NOT NULL
              AND 1.0 - (source.embedding <=> nearest.embedding) >= 0.72
            """,
            (identity.tenant_id, identity.user_id),
        )
        pairs = [(str(row["source_id"]), str(row["related_id"])) for row in cursor.fetchall()]

    parent = {memory_id: memory_id for memory_id in memory_ids}

    def find(memory_id: str) -> str:
        while parent[memory_id] != memory_id:
            parent[memory_id] = parent[parent[memory_id]]
            memory_id = parent[memory_id]
        return memory_id

    for left, right in pairs:
        left_root = find(left)
        right_root = find(right)
        if left_root != right_root:
            parent[right_root] = left_root

    clusters = defaultdict(list)
    for memory_id in memory_ids:
        clusters[find(memory_id)].append(memory_id)
    related_clusters = sum(1 for members in clusters.values() if len(members) > 1)

    return {
        "status": "reflection_complete",
        "memories_examined": len(memory_ids),
        "clusters_formed": related_clusters,
        "related_pairs": len({tuple(sorted(pair)) for pair in pairs}),
        "timestamp": time.time(),
    }
