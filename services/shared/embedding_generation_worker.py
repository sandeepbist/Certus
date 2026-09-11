"""Crash-safe worker primitives for workspace embedding generations."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Any, Sequence
from uuid import UUID

from services.shared.embeddings import EMBEDDING_DIMENSIONS, parse_embedding_profile


class EmbeddingGenerationLeaseLostError(RuntimeError):
    """A generation changed state or a newer worker owns the candidate lease."""


@dataclass(frozen=True)
class EmbeddingGenerationCandidate:
    generation_id: str
    chunk_id: str
    content: str
    contextualized_content: str | None
    content_sha256: str
    attempt_count: int

    @property
    def embedding_input(self) -> str:
        return self.contextualized_content or self.content


def _canonical_uuid(value: str, field_name: str) -> str:
    try:
        return str(UUID(str(value)))
    except (AttributeError, TypeError, ValueError) as error:
        raise ValueError(f"{field_name} must be a valid UUID") from error


def qualify_next_embedding_generation(
    cursor: Any,
    *,
    embedding_profile: str,
) -> str | None:
    """Qualify one complete generation through the database-owned gate."""
    profile = parse_embedding_profile(embedding_profile).identifier
    cursor.execute(
        """
        SELECT id, tenant_id, user_id
        FROM workspace_embedding_generations
        WHERE status = 'building'
          AND embedding_profile = %s
          AND embedded_chunk_count = expected_chunk_count
          AND failed_chunk_count = 0
          AND NOT EXISTS (
              SELECT 1
              FROM chunk_embedding_vectors AS candidate
              WHERE candidate.generation_id = workspace_embedding_generations.id
                AND candidate.status <> 'embedded'
          )
        ORDER BY updated_at, id
        LIMIT 1
        """,
        (profile,),
    )
    row = cursor.fetchone()
    if not row:
        return None
    generation_id = str(row[0])
    cursor.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended(%s || chr(31) || %s, 0))",
        (str(row[1]), str(row[2])),
    )
    cursor.execute(
        "SELECT qualify_workspace_embedding_generation(%s::uuid)",
        (generation_id,),
    )
    return generation_id if bool(cursor.fetchone()[0]) else None


def claim_next_embedding_generation_batch(
    cursor: Any,
    *,
    embedding_profile: str,
    lease_owner: str,
    batch_size: int,
    lease_seconds: int,
) -> list[EmbeddingGenerationCandidate]:
    """Fairly select one compatible generation and claim one durable batch."""
    profile = parse_embedding_profile(embedding_profile).identifier
    owner = _canonical_uuid(lease_owner, "lease_owner")
    if not 1 <= batch_size <= 100:
        raise ValueError("batch_size must be between 1 and 100")
    if not 60 <= lease_seconds <= 3_600:
        raise ValueError("lease_seconds must be between 60 and 3600")

    cursor.execute(
        """
        WITH selected_generation AS MATERIALIZED (
            SELECT generation.id
            FROM workspace_embedding_generations AS generation
            WHERE generation.status = 'building'
              AND generation.embedding_profile = %s
              AND EXISTS (
                  SELECT 1
                  FROM chunk_embedding_vectors AS candidate
                  WHERE candidate.generation_id = generation.id
                    AND (
                        (
                            candidate.status IN ('pending', 'failed')
                            AND candidate.available_at <= NOW()
                        )
                        OR (
                            candidate.status = 'processing'
                            AND candidate.leased_at
                                < NOW() - (%s * INTERVAL '1 second')
                        )
                    )
              )
            ORDER BY generation.updated_at, generation.id
            FOR UPDATE SKIP LOCKED
            LIMIT 1
        )
        SELECT selected_generation.id AS generation_id,
               claimed.chunk_id, claimed.content,
               claimed.contextualized_content, claimed.content_sha256,
               claimed.attempt_count
        FROM selected_generation
        CROSS JOIN LATERAL claim_chunk_embedding_vectors(
            selected_generation.id, %s::uuid, %s, %s
        ) AS claimed
        ORDER BY claimed.chunk_id
        """,
        (profile, lease_seconds, owner, batch_size, lease_seconds),
    )
    return [
        EmbeddingGenerationCandidate(
            generation_id=str(row[0]),
            chunk_id=str(row[1]),
            content=str(row[2]),
            contextualized_content=(str(row[3]) if row[3] is not None else None),
            content_sha256=str(row[4]),
            attempt_count=int(row[5]),
        )
        for row in cursor.fetchall()
    ]


def record_embedding_generation_batch(
    cursor: Any,
    *,
    candidates: Sequence[EmbeddingGenerationCandidate],
    lease_owner: str,
    embeddings: Sequence[Sequence[float]],
    provider_metadata: dict[str, Any],
) -> None:
    """Commit a claimed batch atomically or reject every stale provider result."""
    if not candidates:
        raise ValueError("candidates must not be empty")
    if len(candidates) != len(embeddings):
        raise ValueError("embedding result count does not match the claimed batch")
    owner = _canonical_uuid(lease_owner, "lease_owner")
    generation_ids = {candidate.generation_id for candidate in candidates}
    if len(generation_ids) != 1:
        raise ValueError("one batch cannot span embedding generations")
    if any(len(vector) != EMBEDDING_DIMENSIONS for vector in embeddings):
        raise ValueError("embedding result has an incompatible vector dimension")
    if any(not math.isfinite(float(value)) for vector in embeddings for value in vector):
        raise ValueError("embedding result contains a non-finite value")
    metadata_json = json.dumps(provider_metadata, sort_keys=True, separators=(",", ":"))

    for candidate, vector in zip(candidates, embeddings, strict=True):
        vector_literal = f"[{','.join(str(value) for value in vector)}]"
        cursor.execute(
            """
            SELECT record_chunk_embedding_vector(
                %s::uuid, %s::uuid, %s::uuid, %s::vector, %s::jsonb
            )
            """,
            (
                candidate.generation_id,
                candidate.chunk_id,
                owner,
                vector_literal,
                metadata_json,
            ),
        )
        if not bool(cursor.fetchone()[0]):
            raise EmbeddingGenerationLeaseLostError(
                "embedding generation candidate lease is no longer authoritative"
            )


def record_embedding_generation_failure(
    cursor: Any,
    *,
    candidates: Sequence[EmbeddingGenerationCandidate],
    lease_owner: str,
    error_code: str,
    error_message: str,
    retry_delay_seconds: int,
    max_attempts: int,
) -> bool:
    """Release a failed batch with backoff and fail exhausted generations."""
    if not candidates:
        raise ValueError("candidates must not be empty")
    if not error_code.strip() or not error_message.strip():
        raise ValueError("generation failure requires a code and message")
    if not 0 <= retry_delay_seconds <= 86_400:
        raise ValueError("retry_delay_seconds must be between 0 and 86400")
    if not 1 <= max_attempts <= 20:
        raise ValueError("max_attempts must be between 1 and 20")
    owner = _canonical_uuid(lease_owner, "lease_owner")
    generation_ids = {candidate.generation_id for candidate in candidates}
    if len(generation_ids) != 1:
        raise ValueError("one batch cannot span embedding generations")
    generation_id = next(iter(generation_ids))

    for candidate in candidates:
        cursor.execute(
            """
            SELECT record_chunk_embedding_failure(
                %s::uuid, %s::uuid, %s::uuid, %s, %s,
                make_interval(secs => %s)
            )
            """,
            (
                generation_id,
                candidate.chunk_id,
                owner,
                error_code[:80],
                error_message[:2_000],
                retry_delay_seconds,
            ),
        )
        if not bool(cursor.fetchone()[0]):
            raise EmbeddingGenerationLeaseLostError(
                "embedding generation candidate lease is no longer authoritative"
            )

    cursor.execute(
        """
        UPDATE workspace_embedding_generations AS generation
        SET status = 'failed',
            last_error = %s,
            updated_at = NOW()
        WHERE generation.id = %s::uuid
          AND generation.status = 'building'
          AND EXISTS (
              SELECT 1
              FROM chunk_embedding_vectors AS candidate
              WHERE candidate.generation_id = generation.id
                AND candidate.status = 'failed'
                AND candidate.attempt_count >= %s
          )
        """,
        (error_message[:2_000], generation_id, max_attempts),
    )
    return cursor.rowcount == 1
