import logging
import math
import os
from typing import Any, Dict, List

from psycopg2.extras import RealDictCursor

from app.core.db import get_db_connection
from app.core.runtime import RETRIEVAL_STATEMENT_TIMEOUT_MS
from app.retrieval.hybrid import EmbeddingResult, embed_query


logger = logging.getLogger("orchestration_memory_retrieval")


def normalize_memory_similarity(value: object) -> float:
    try:
        normalized = float(value)
    except (TypeError, ValueError) as error:
        raise ValueError(
            "MEMORY_MIN_SIMILARITY must be a finite number between -1 and 1"
        ) from error
    if not math.isfinite(normalized) or not -1.0 <= normalized <= 1.0:
        raise ValueError(
            "MEMORY_MIN_SIMILARITY must be a finite number between -1 and 1"
        )
    return normalized


def normalize_memory_limit(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 100:
        raise ValueError("Memory result limit must be an integer between 1 and 100")
    return value


MIN_MEMORY_SIMILARITY = normalize_memory_similarity(
    os.getenv("MEMORY_MIN_SIMILARITY", "0.3")
)


def retrieve_relevant_memories(
    query: str,
    tenant_id: str,
    user_id: str,
    limit: int = 5,
    query_embedding: EmbeddingResult | None = None,
    allow_embedding_generation: bool = True,
) -> List[Dict[str, Any]]:
    normalized_limit = normalize_memory_limit(limit)
    try:
        resolved_embedding = query_embedding
        if resolved_embedding is None and allow_embedding_generation:
            resolved_embedding = embed_query(query)
        if resolved_embedding is None:
            return []
        vector = f"[{','.join(str(value) for value in resolved_embedding.vector)}]"
        with get_db_connection() as connection:
            with connection.cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute(
                    "SELECT set_config('statement_timeout', %s, true)",
                    (f"{RETRIEVAL_STATEMENT_TIMEOUT_MS}ms",),
                )
                cursor.execute(
                    """
                    SELECT id, fact, category, confidence,
                           1.0 - (embedding <=> %s::vector) AS similarity
                    FROM memories
                    WHERE tenant_id = %s
                      AND user_id = %s
                      AND is_active = true
                      AND embedding IS NOT NULL
                      AND embedding_profile = %s
                      AND 1.0 - (embedding <=> %s::vector) >= %s
                    ORDER BY embedding <=> %s::vector
                    LIMIT %s
                    """,
                    (
                        vector,
                        tenant_id,
                        user_id,
                        resolved_embedding.profile.identifier,
                        vector,
                        MIN_MEMORY_SIMILARITY,
                        vector,
                        normalized_limit,
                    ),
                )
                memories = [dict(memory) for memory in cursor.fetchall()]
                if memories:
                    cursor.execute(
                        """
                        UPDATE memories
                        SET access_count = access_count + 1, last_accessed_at = NOW()
                        WHERE id = ANY(%s::uuid[]) AND tenant_id = %s AND user_id = %s
                        """,
                        ([str(memory["id"]) for memory in memories], tenant_id, user_id),
                    )
        return memories
    except Exception as error:
        logger.warning("Long-term memory retrieval degraded: %s", error)
        return []
