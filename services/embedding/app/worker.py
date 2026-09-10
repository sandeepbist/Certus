import os
import sys
import time
import json
import logging
import signal
import socket
import uuid
from threading import Event
from pathlib import Path
from psycopg2.extras import RealDictCursor, execute_values
import redis
from dotenv import load_dotenv

MODULE_PATH = Path(__file__).resolve()
REPO_ROOT = next(
    (parent for parent in MODULE_PATH.parents if (parent / "services" / "shared").exists()),
    MODULE_PATH.parents[1],
)
sys.path.insert(0, str(REPO_ROOT))

from services.shared.embeddings import (
    EMBEDDING_DIMENSIONS,
    configured_embedding_profile,
    has_usable_openai_api_key,
    local_lexical_embedding,
    parse_embedding_profile,
)
from services.shared.embedding_generation_worker import (
    EmbeddingGenerationLeaseLostError,
    claim_next_embedding_generation_batch,
    record_embedding_generation_batch,
    record_embedding_generation_failure,
)
from services.shared.worker_runtime import (
    WorkerIdentity,
    bounded_int_env,
    bounded_retention_days_env,
    connect_database,
    redis_connection_options,
    write_worker_heartbeat,
)

load_dotenv(REPO_ROOT / ".env")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("embedding_worker")

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://nexus:nexus_dev_password@localhost:5432/nexus")
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
EMBEDDING_MODEL = os.getenv("OPENAI_EMBEDDING_MODEL", "text-embedding-3-small")
ACTIVE_EMBEDDING_PROFILE = configured_embedding_profile(
    OPENAI_API_KEY,
    EMBEDDING_MODEL,
)
STREAM_KEY = "embedding:jobs"
GROUP_NAME = "embedding-workers"
DLQ_KEY = "embedding:dlq"
MAX_RETRIES = 3
PENDING_IDLE_MS = 60_000
EMBEDDING_DLQ_MAX_LENGTH = int(os.getenv("EMBEDDING_DLQ_MAX_LENGTH", "1000"))
EMBEDDING_PROCESSING_LEASE_SECONDS = int(
    os.getenv("EMBEDDING_PROCESSING_LEASE_SECONDS", "300")
)
EMBEDDING_GENERATION_BATCH_SIZE = bounded_int_env(
    "EMBEDDING_GENERATION_BATCH_SIZE", 20, 1, 100
)
EMBEDDING_GENERATION_LEASE_SECONDS = bounded_int_env(
    "EMBEDDING_GENERATION_LEASE_SECONDS", 300, 60, 3_600
)
EMBEDDING_GENERATION_MAX_ATTEMPTS = bounded_int_env(
    "EMBEDDING_GENERATION_MAX_ATTEMPTS", 5, 1, 20
)
EMBEDDING_GENERATION_RETRY_BASE_SECONDS = bounded_int_env(
    "EMBEDDING_GENERATION_RETRY_BASE_SECONDS", 30, 1, 3_600
)
EMBEDDING_GENERATION_RETRY_MAX_SECONDS = bounded_int_env(
    "EMBEDDING_GENERATION_RETRY_MAX_SECONDS", 3_600, 1, 86_400
)
EMBEDDING_GENERATION_RETENTION_DAYS = bounded_retention_days_env(
    "EMBEDDING_GENERATION_RETENTION_DAYS", 30
)
EMBEDDING_GENERATION_PRUNE_BATCH_SIZE = bounded_int_env(
    "EMBEDDING_GENERATION_PRUNE_BATCH_SIZE", 25, 1, 1_000
)
EMBEDDING_GENERATION_PRUNE_INTERVAL_SECONDS = bounded_int_env(
    "EMBEDDING_GENERATION_PRUNE_INTERVAL_SECONDS", 3_600, 60, 86_400
)
DATABASE_CONNECT_TIMEOUT_SECONDS = bounded_int_env(
    "EMBEDDING_DB_CONNECT_TIMEOUT_SECONDS", 3, 1, 30
)
REDIS_CONNECT_TIMEOUT_SECONDS = bounded_int_env(
    "EMBEDDING_REDIS_CONNECT_TIMEOUT_SECONDS", 3, 1, 30
)
REDIS_SOCKET_TIMEOUT_SECONDS = bounded_int_env(
    "EMBEDDING_REDIS_SOCKET_TIMEOUT_SECONDS", 5, 3, 60
)
OPENAI_REQUEST_TIMEOUT_SECONDS = bounded_int_env(
    "OPENAI_INDEXING_TIMEOUT_SECONDS", 30, 1, 300
)
OPENAI_MAX_RETRIES = bounded_int_env("OPENAI_INDEXING_MAX_RETRIES", 1, 0, 5)
WORKER_HEARTBEAT_INTERVAL_SECONDS = bounded_int_env(
    "WORKER_HEARTBEAT_INTERVAL_SECONDS", 10, 5, 60
)
WORKER_DB_STATEMENT_TIMEOUT_MS = bounded_int_env(
    "WORKER_DB_STATEMENT_TIMEOUT_MS", 15_000, 1_000, 120_000
)
WORKER_DB_LOCK_TIMEOUT_MS = bounded_int_env(
    "WORKER_DB_LOCK_TIMEOUT_MS", 3_000, 500, 30_000
)
WORKER_IDENTITY = WorkerIdentity("embedding-worker")
CONSUMER_NAME = (
    f"embedding-{socket.gethostname()}-{os.getpid()}-"
    f"{str(WORKER_IDENTITY.instance_id)[:8]}"
)
STOP_REQUESTED = Event()
_openai_client = None

if not 100 <= EMBEDDING_DLQ_MAX_LENGTH <= 100_000:
    raise ValueError("EMBEDDING_DLQ_MAX_LENGTH must be between 100 and 100000")
if not 60 <= EMBEDDING_PROCESSING_LEASE_SECONDS <= 3_600:
    raise ValueError("EMBEDDING_PROCESSING_LEASE_SECONDS must be between 60 and 3600")
if EMBEDDING_GENERATION_RETRY_MAX_SECONDS < EMBEDDING_GENERATION_RETRY_BASE_SECONDS:
    raise ValueError(
        "EMBEDDING_GENERATION_RETRY_MAX_SECONDS must be at least the retry base"
    )


class EmbeddingProfileMismatchError(RuntimeError):
    """A durable job targets a vector space this worker cannot produce."""


def _get_openai_client():
    global _openai_client
    if _openai_client is None:
        from openai import OpenAI

        _openai_client = OpenAI(
            api_key=OPENAI_API_KEY,
            timeout=OPENAI_REQUEST_TIMEOUT_SECONDS,
            max_retries=OPENAI_MAX_RETRIES,
        )
    return _openai_client


def close_openai_client() -> None:
    global _openai_client
    if _openai_client is not None:
        _openai_client.close()
        _openai_client = None


def get_embeddings(
    texts: list[str],
    expected_profile: str,
) -> tuple[list[list[float]], str]:
    if not texts or any(not text.strip() for text in texts):
        raise ValueError("Embedding input must contain non-empty text")
    parse_embedding_profile(expected_profile)
    if expected_profile != ACTIVE_EMBEDDING_PROFILE.identifier:
        raise EmbeddingProfileMismatchError(
            "Embedding job profile does not match this worker: "
            f"job={expected_profile}, worker={ACTIVE_EMBEDDING_PROFILE.identifier}"
        )

    if has_usable_openai_api_key(OPENAI_API_KEY):
        client = _get_openai_client()
        response = client.embeddings.create(
            input=texts,
            model=EMBEDDING_MODEL,
            dimensions=EMBEDDING_DIMENSIONS,
            encoding_format="float",
        )
        ordered = sorted(response.data, key=lambda item: item.index)
        embeddings = [item.embedding for item in ordered]
        if len(embeddings) != len(texts):
            raise RuntimeError("Embedding provider returned an unexpected result count")
        if any(len(embedding) != EMBEDDING_DIMENSIONS for embedding in embeddings):
            raise RuntimeError("Embedding provider returned an incompatible vector dimension")
        if response.model != ACTIVE_EMBEDDING_PROFILE.model:
            raise EmbeddingProfileMismatchError(
                "Embedding provider returned a different model than requested: "
                f"requested={ACTIVE_EMBEDDING_PROFILE.model}, returned={response.model}"
            )
        logger.info(
            "OpenAI embedding request completed (request_id=%s, model=%s, inputs=%s)",
            getattr(response, "_request_id", None) or "unavailable",
            response.model,
            len(texts),
        )
        return embeddings, response.model

    return [local_lexical_embedding(text) for text in texts], ACTIVE_EMBEDDING_PROFILE.model

def acknowledge_message(redis_client, message_id: str) -> None:
    pipeline = redis_client.pipeline(transaction=True)
    pipeline.xack(STREAM_KEY, GROUP_NAME, message_id)
    pipeline.xdel(STREAM_KEY, message_id)
    pipeline.execute()


def _validated_job_identity(
    fields: dict,
) -> tuple[str, str, str, str, str, str, str]:
    outbox_id = str(uuid.UUID(fields["outbox_id"]))
    document_id = str(uuid.UUID(fields["document_id"]))
    document_version_id = str(uuid.UUID(fields["document_version_id"]))
    derivation_id = str(uuid.UUID(fields["derivation_id"]))
    processing_generation = str(uuid.UUID(fields["processing_generation"]))
    tenant_id = fields["tenant_id"].strip()
    user_id = fields["user_id"].strip()
    if not tenant_id or not user_id:
        raise ValueError("Embedding job is missing tenant or user identity")
    return (
        outbox_id,
        document_id,
        document_version_id,
        derivation_id,
        tenant_id,
        user_id,
        processing_generation,
    )


def _validated_job_fields(
    fields: dict,
) -> tuple[str, str, str, str, str, str, str, str, int, int, int]:
    identity = _validated_job_identity(fields)
    embedding_profile = parse_embedding_profile(fields["embedding_profile"]).identifier
    batch_start = int(fields["batch_start"])
    batch_end = int(fields["batch_end"])
    total_chunks = int(fields["total_chunks"])
    if batch_start < 0 or batch_end <= batch_start or total_chunks < batch_end:
        raise ValueError("Embedding job has invalid batch boundaries")
    return (*identity, embedding_profile, batch_start, batch_end, total_chunks)


def process_batch(
    conn,
    redis_client,
    message_id: str,
    fields: dict,
    processing_owner: str,
    lease_state: dict[str, bool],
):
    (
        outbox_id,
        document_id,
        document_version_id,
        derivation_id,
        tenant_id,
        user_id,
        processing_generation,
        expected_embedding_profile,
        batch_start,
        batch_end,
        expected_chunk_count,
    ) = _validated_job_fields(fields)

    with conn.cursor(cursor_factory=RealDictCursor) as cursor:
        cursor.execute(
            """
            SELECT job.status AS job_status,
                   job.embedding_profile,
                   job.locked_at >= NOW() - (%s * INTERVAL '1 second') AS lease_active,
                   version.current_derivation_id,
                   version.pending_derivation_id,
                   version.processing_generation AS version_processing_generation,
                   version.status AS version_status,
                   derivation.processing_generation AS derivation_processing_generation,
                   derivation.status AS derivation_status,
                   document.deleted_at
            FROM document_embedding_jobs AS job
            JOIN documents AS document ON document.id = job.document_id
            JOIN document_versions AS version
              ON version.id = job.document_version_id
             AND version.document_id = job.document_id
            JOIN document_derivations AS derivation
              ON derivation.id = job.derivation_id
             AND derivation.document_version_id = job.document_version_id
             AND derivation.document_id = job.document_id
            WHERE job.id = %s
              AND job.document_id = %s
              AND job.document_version_id = %s
              AND job.derivation_id = %s
              AND job.tenant_id = %s
              AND job.user_id = %s
              AND job.processing_generation = %s
            FOR UPDATE OF job
            """,
            (
                EMBEDDING_PROCESSING_LEASE_SECONDS,
                outbox_id,
                document_id,
                document_version_id,
                derivation_id,
                tenant_id,
                user_id,
                processing_generation,
            ),
        )
        job = cursor.fetchone()
        if not job:
            conn.commit()
            acknowledge_message(redis_client, message_id)
            return
        if job["embedding_profile"] != expected_embedding_profile:
            conn.commit()
            logger.warning(
                "Discarding embedding message %s with profile %s; outbox job expects %s",
                message_id,
                expected_embedding_profile,
                job["embedding_profile"],
            )
            acknowledge_message(redis_client, message_id)
            return
        if (
            job["deleted_at"] is not None
            or str(job["derivation_processing_generation"]) != processing_generation
            or job["derivation_status"] != "processing"
            or not (
                (
                    str(job["current_derivation_id"]) == derivation_id
                    and str(job["version_processing_generation"]) == processing_generation
                    and job["version_status"] == "processing"
                )
                or str(job["pending_derivation_id"]) == derivation_id
            )
        ):
            cursor.execute(
                """
                UPDATE document_embedding_jobs
                SET status = 'obsolete', processed_at = COALESCE(processed_at, NOW()),
                    locked_at = NULL, processing_owner = NULL, updated_at = NOW()
                WHERE id = %s
                  AND status IN ('pending', 'published', 'publishing', 'processing')
                """,
                (outbox_id,),
            )
            conn.commit()
            acknowledge_message(redis_client, message_id)
            return
        if job["job_status"] in {"processed", "failed", "obsolete"}:
            conn.commit()
            acknowledge_message(redis_client, message_id)
            return
        if job["job_status"] == "processing" and job["lease_active"]:
            conn.commit()
            logger.info("Embedding job %s already has an active worker lease", outbox_id)
            return
        cursor.execute(
            """
            UPDATE document_embedding_jobs
            SET status = 'processing', locked_at = NOW(), processing_owner = %s,
                updated_at = NOW()
            WHERE id = %s
              AND status IN ('pending', 'publishing', 'published', 'processing')
            """,
            (processing_owner, outbox_id),
        )
        if cursor.rowcount != 1:
            conn.commit()
            return
        conn.commit()
        lease_state["acquired"] = True

    if expected_embedding_profile != ACTIVE_EMBEDDING_PROFILE.identifier:
        raise EmbeddingProfileMismatchError(
            "Embedding job profile does not match this worker: "
            f"job={expected_embedding_profile}, "
            f"worker={ACTIVE_EMBEDDING_PROFILE.identifier}"
        )

    with conn.cursor(cursor_factory=RealDictCursor) as cursor:
        cursor.execute(
            """
            SELECT id, content, contextualized_content
            FROM chunks
            WHERE document_id = %s
              AND document_version_id = %s
              AND derivation_id = %s
              AND tenant_id = %s
              AND user_id = %s
              AND processing_generation = %s
              AND embedding_profile = %s
              AND chunk_index >= %s
              AND chunk_index < %s
              AND embedded_at IS NULL
            ORDER BY chunk_index
            """,
            (
                document_id,
                document_version_id,
                derivation_id,
                tenant_id,
                user_id,
                processing_generation,
                expected_embedding_profile,
                batch_start,
                batch_end,
            ),
        )
        chunks = [dict(chunk) for chunk in cursor.fetchall()]
    conn.rollback()

    embeddings: list[list[float]] = []
    embedding_provider = ""
    if chunks:
        logger.info(
            "Generating embeddings for %s chunks of document %s (%s:%s)...",
            len(chunks),
            document_id,
            batch_start,
            batch_end,
        )
        embed_texts = [
            chunk.get("contextualized_content") or chunk.get("content")
            for chunk in chunks
        ]
        embeddings, embedding_provider = get_embeddings(
            embed_texts,
            expected_embedding_profile,
        )

    with conn.cursor(cursor_factory=RealDictCursor) as cursor:
        cursor.execute(
            """
            SELECT status, processing_owner
            FROM document_embedding_jobs
            WHERE id = %s
              AND document_id = %s
              AND document_version_id = %s
              AND derivation_id = %s
              AND processing_generation = %s
            FOR UPDATE
            """,
            (
                outbox_id,
                document_id,
                document_version_id,
                derivation_id,
                processing_generation,
            ),
        )
        owned_job = cursor.fetchone()
        if not owned_job:
            conn.rollback()
            acknowledge_message(redis_client, message_id)
            return
        if owned_job["status"] in {"processed", "failed", "obsolete"}:
            conn.rollback()
            acknowledge_message(redis_client, message_id)
            return
        if (
            owned_job["status"] != "processing"
            or owned_job["processing_owner"] != processing_owner
        ):
            conn.rollback()
            logger.info("Embedding job %s no longer owns its processing lease", outbox_id)
            return
        cursor.execute(
            """
            SELECT version.current_derivation_id,
                   version.pending_derivation_id,
                   version.processing_generation AS version_processing_generation,
                   version.status AS version_status,
                   derivation.processing_generation AS derivation_processing_generation,
                   derivation.status AS derivation_status,
                   derivation.chunker_profile
            FROM document_versions AS version
            JOIN document_derivations AS derivation
              ON derivation.id = %s
             AND derivation.document_version_id = version.id
             AND derivation.document_id = version.document_id
             AND derivation.tenant_id = version.tenant_id
             AND derivation.user_id = version.user_id
            WHERE version.id = %s
              AND version.document_id = %s
              AND version.tenant_id = %s
              AND version.user_id = %s
            FOR UPDATE OF version, derivation
            """,
            (
                derivation_id,
                document_version_id,
                document_id,
                tenant_id,
                user_id,
            ),
        )
        target_state = cursor.fetchone()
        if not target_state or (
            str(target_state["derivation_processing_generation"])
            != processing_generation
            or target_state["derivation_status"] != "processing"
            or not (
                (
                    str(target_state["current_derivation_id"]) == derivation_id
                    and str(target_state["version_processing_generation"])
                    == processing_generation
                    and target_state["version_status"] == "processing"
                )
                or (
                    str(target_state["pending_derivation_id"]) == derivation_id
                    and target_state["version_status"] == "ready"
                )
            )
        ):
            # The document may have been replaced or deleted while the provider
            # was generating embeddings. Retire the leased job before deleting
            # its Redis delivery so it cannot remain stuck in `processing` with
            # no message available for recovery.
            cursor.execute(
                """
                UPDATE document_embedding_jobs
                SET status = 'obsolete',
                    processed_at = COALESCE(processed_at, NOW()),
                    locked_at = NULL, processing_owner = NULL,
                    updated_at = NOW()
                WHERE id = %s
                  AND document_id = %s
                  AND document_version_id = %s
                  AND derivation_id = %s
                  AND processing_generation = %s
                  AND status = 'processing'
                  AND processing_owner = %s
                """,
                (
                    outbox_id,
                    document_id,
                    document_version_id,
                    derivation_id,
                    processing_generation,
                    processing_owner,
                ),
            )
            conn.commit()
            acknowledge_message(redis_client, message_id)
            return

    if chunks:
        update_rows = [
            (
                str(chunk["id"]),
                f"[{','.join(str(value) for value in embeddings[index])}]",
                json.dumps({
                    "embedding_provider": embedding_provider,
                    "embedding_model_requested": ACTIVE_EMBEDDING_PROFILE.model,
                    "embedding_profile": expected_embedding_profile,
                }),
                document_id,
                document_version_id,
                derivation_id,
                tenant_id,
                user_id,
                processing_generation,
                expected_embedding_profile,
            )
            for index, chunk in enumerate(chunks)
        ]
        with conn.cursor() as cursor:
            execute_values(
                cursor,
                """
                UPDATE chunks AS chunk
                SET embedding = values.embedding::vector,
                    metadata = chunk.metadata || values.metadata::jsonb,
                    embedded_at = NOW()
                FROM (VALUES %s) AS values(
                    id, embedding, metadata, document_id,
                    document_version_id, derivation_id,
                    tenant_id, user_id, processing_generation, embedding_profile
                )
                WHERE chunk.id = values.id::uuid
                  AND chunk.document_id = values.document_id::uuid
                  AND chunk.document_version_id = values.document_version_id::uuid
                  AND chunk.derivation_id = values.derivation_id::uuid
                  AND chunk.tenant_id = values.tenant_id
                  AND chunk.user_id = values.user_id
                  AND chunk.processing_generation = values.processing_generation::uuid
                  AND chunk.embedding_profile = values.embedding_profile
                  AND chunk.embedded_at IS NULL
                """,
                update_rows,
                template="(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                page_size=len(update_rows),
            )

    with conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT COUNT(*) FILTER (
                       WHERE chunk.embedded_at IS NOT NULL
                         AND chunk.embedding_profile = %s
                   ) AS embedded_count,
                   BOOL_OR(chunk.embedding_profile <> %s) AS has_profile_mismatch,
                   EXISTS (
                       SELECT 1
                       FROM document_embedding_jobs AS job
                       WHERE job.derivation_id = %s
                         AND job.status = 'failed'
                   ) AS has_failed_job
            FROM chunks AS chunk
            WHERE chunk.document_id = %s
              AND chunk.document_version_id = %s
              AND chunk.derivation_id = %s
              AND chunk.tenant_id = %s
              AND chunk.user_id = %s
              AND chunk.processing_generation = %s
            """,
            (
                expected_embedding_profile,
                expected_embedding_profile,
                derivation_id,
                document_id,
                document_version_id,
                derivation_id,
                tenant_id,
                user_id,
                processing_generation,
            ),
        )
        progress = cursor.fetchone()
        stored_count = progress[0]
        next_status = (
            "error"
            if progress[1] or progress[2]
            else "ready" if stored_count >= expected_chunk_count else "processing"
        )
        cursor.execute(
            """
            UPDATE document_derivations
            SET status = %s,
                chunk_count = %s,
                processing_total_chunks = %s,
                error_message = CASE
                    WHEN %s = 'error' THEN COALESCE(
                        error_message,
                        'One or more embedding batches failed or used an incompatible profile.'
                    )
                    ELSE NULL
                END,
                completed_at = CASE
                    WHEN %s IN ('ready', 'error') THEN COALESCE(completed_at, NOW())
                    ELSE NULL
                END,
                updated_at = NOW()
            WHERE id = %s
              AND document_version_id = %s
              AND document_id = %s
              AND tenant_id = %s
              AND user_id = %s
              AND processing_generation = %s
            """,
            (
                next_status,
                stored_count,
                expected_chunk_count,
                next_status,
                next_status,
                derivation_id,
                document_version_id,
                document_id,
                tenant_id,
                user_id,
                processing_generation,
            ),
        )
        is_shadow_derivation = (
            str(target_state["pending_derivation_id"]) == derivation_id
        )
        previous_derivation_id = str(target_state["current_derivation_id"])
        if next_status == "ready":
            cursor.execute(
                """
                UPDATE document_versions
                SET status = 'ready', error_message = NULL,
                    processing_generation = %s,
                    current_derivation_id = %s,
                    pending_derivation_id = NULL,
                    updated_at = NOW()
                WHERE id = %s AND document_id = %s
                  AND tenant_id = %s AND user_id = %s
                  AND (
                        current_derivation_id = %s
                        OR pending_derivation_id = %s
                  )
                """,
                (
                    processing_generation,
                    derivation_id,
                    document_version_id,
                    document_id,
                    tenant_id,
                    user_id,
                    derivation_id,
                    derivation_id,
                ),
            )
            if is_shadow_derivation and previous_derivation_id != derivation_id:
                cursor.execute(
                    """
                    UPDATE document_derivations
                    SET status = 'superseded',
                        superseded_at = COALESCE(superseded_at, NOW()),
                        updated_at = NOW()
                    WHERE id = %s
                      AND document_version_id = %s
                      AND document_id = %s
                      AND tenant_id = %s AND user_id = %s
                      AND status = 'ready'
                    """,
                    (
                        previous_derivation_id,
                        document_version_id,
                        document_id,
                        tenant_id,
                        user_id,
                    ),
                )
            if is_shadow_derivation:
                chunker_profile = dict(target_state.get("chunker_profile") or {})
                chunk_strategy = chunker_profile.get("strategy") or "token"
                cursor.execute(
                    """
                    UPDATE documents
                    SET status = 'ready', chunk_count = %s,
                        processing_total_chunks = %s, error_message = NULL,
                        processing_generation = %s,
                        metadata = (
                            metadata - 'pending_chunk_strategy' - 'last_rechunk_error'
                        ) || jsonb_build_object(
                            'chunk_strategy', %s,
                            'rechunked_at', NOW()
                        ),
                        updated_at = NOW()
                    WHERE id = %s AND current_version_id = %s
                      AND tenant_id = %s AND user_id = %s
                      AND deleted_at IS NULL
                    """,
                    (
                        stored_count,
                        expected_chunk_count,
                        processing_generation,
                        chunk_strategy,
                        document_id,
                        document_version_id,
                        tenant_id,
                        user_id,
                    ),
                )
            else:
                cursor.execute(
                    """
                    UPDATE documents
                    SET status = 'ready', chunk_count = %s,
                        processing_total_chunks = %s, error_message = NULL,
                        processing_generation = %s, updated_at = NOW()
                    WHERE id = %s AND current_version_id = %s
                      AND tenant_id = %s AND user_id = %s
                      AND processing_generation = %s
                      AND deleted_at IS NULL
                    """,
                    (
                        stored_count,
                        expected_chunk_count,
                        processing_generation,
                        document_id,
                        document_version_id,
                        tenant_id,
                        user_id,
                        processing_generation,
                    ),
                )
        elif is_shadow_derivation:
            if next_status == "error":
                error_message = (
                    "Replacement derivation failed; the prior searchable derivation was retained."
                )
                cursor.execute(
                    """
                    UPDATE document_versions
                    SET pending_derivation_id = NULL, updated_at = NOW()
                    WHERE id = %s AND document_id = %s
                      AND tenant_id = %s AND user_id = %s
                      AND pending_derivation_id = %s
                    """,
                    (
                        document_version_id,
                        document_id,
                        tenant_id,
                        user_id,
                        derivation_id,
                    ),
                )
                cursor.execute(
                    """
                    UPDATE documents
                    SET metadata = (metadata - 'pending_chunk_strategy')
                        || jsonb_build_object(
                            'last_rechunk_error', %s,
                            'rechunk_failed_at', NOW()
                        ),
                        updated_at = NOW()
                    WHERE id = %s AND current_version_id = %s
                      AND tenant_id = %s AND user_id = %s
                      AND deleted_at IS NULL
                    """,
                    (
                        error_message,
                        document_id,
                        document_version_id,
                        tenant_id,
                        user_id,
                    ),
                )
        else:
            cursor.execute(
                """
                UPDATE document_versions
                SET status = %s,
                    error_message = CASE
                        WHEN %s = 'error' THEN COALESCE(
                            error_message,
                            'One or more embedding batches failed or used an incompatible profile.'
                        )
                        ELSE NULL
                    END,
                    updated_at = NOW()
                WHERE id = %s AND document_id = %s
                  AND current_derivation_id = %s
                  AND processing_generation = %s
                """,
                (
                    next_status,
                    next_status,
                    document_version_id,
                    document_id,
                    derivation_id,
                    processing_generation,
                ),
            )
            cursor.execute(
                """
                UPDATE documents
                SET status = %s, chunk_count = %s,
                    processing_total_chunks = %s,
                    error_message = CASE
                        WHEN %s = 'error' THEN COALESCE(
                            error_message,
                            'One or more embedding batches failed or used an incompatible profile.'
                        )
                        ELSE NULL
                    END,
                    updated_at = NOW()
                WHERE id = %s AND current_version_id = %s
                  AND tenant_id = %s AND user_id = %s
                  AND processing_generation = %s
                  AND deleted_at IS NULL
                """,
                (
                    next_status,
                    stored_count,
                    expected_chunk_count,
                    next_status,
                    document_id,
                    document_version_id,
                    tenant_id,
                    user_id,
                    processing_generation,
                ),
            )
        cursor.execute(
            """
            UPDATE document_embedding_jobs
            SET status = 'processed', processed_at = COALESCE(processed_at, NOW()),
                locked_at = NULL, processing_owner = NULL,
                last_error = NULL, updated_at = NOW()
            WHERE id = %s AND document_id = %s
              AND document_version_id = %s
              AND derivation_id = %s
              AND processing_generation = %s
              AND status = 'processing'
              AND processing_owner = %s
            """,
            (
                outbox_id,
                document_id,
                document_version_id,
                derivation_id,
                processing_generation,
                processing_owner,
            ),
        )
    conn.commit()
    acknowledge_message(redis_client, message_id)
    logger.info(
        "Embedded document %s batch %s:%s (%s/%s stored; status=%s).",
        document_id,
        batch_start,
        batch_end,
        stored_count,
        expected_chunk_count,
        next_status,
    )


def reconnect_database(conn):
    try:
        conn.close()
    except Exception:
        pass
    return connect_database(
        DATABASE_URL,
        application_name="certus-embedding-worker",
        connect_timeout_seconds=DATABASE_CONNECT_TIMEOUT_SECONDS,
        statement_timeout_ms=WORKER_DB_STATEMENT_TIMEOUT_MS,
        lock_timeout_ms=WORKER_DB_LOCK_TIMEOUT_MS,
    )


def release_processing_lease(
    conn,
    fields: dict,
    processing_owner: str,
    error: Exception,
) -> bool:
    outbox_id, _, _, _, _, _, processing_generation = _validated_job_identity(fields)
    conn.rollback()
    with conn.cursor() as cursor:
        cursor.execute(
            """
            UPDATE document_embedding_jobs
            SET status = 'published', locked_at = NULL, processing_owner = NULL,
                last_error = %s, updated_at = NOW()
            WHERE id = %s
              AND processing_generation = %s
              AND status = 'processing'
              AND processing_owner = %s
            """,
            (
                str(error)[:1000],
                outbox_id,
                processing_generation,
                processing_owner,
            ),
        )
        released = cursor.rowcount == 1
    conn.commit()
    return released


def mark_document_failed(conn, fields: dict, error: Exception):
    try:
        (
            outbox_id,
            doc_id,
            document_version_id,
            derivation_id,
            tenant_id,
            user_id,
            processing_generation,
        ) = _validated_job_identity(fields)
    except (AttributeError, KeyError, TypeError, ValueError):
        logger.warning("Skipping database failure update for malformed embedding job")
        return

    with conn.cursor() as cursor:
        cursor.execute(
            """
            UPDATE document_embedding_jobs
            SET status = 'failed', processed_at = COALESCE(processed_at, NOW()),
                locked_at = NULL, processing_owner = NULL,
                last_error = %s, updated_at = NOW()
            WHERE id = %s AND document_id = %s
              AND document_version_id = %s AND derivation_id = %s
              AND tenant_id = %s AND user_id = %s
              AND processing_generation = %s
              AND status IN ('pending', 'publishing', 'published', 'failed')
            """,
            (
                str(error)[:1000],
                outbox_id,
                doc_id,
                document_version_id,
                derivation_id,
                tenant_id,
                user_id,
                processing_generation,
            ),
        )
        job_failed = cursor.rowcount == 1
        if not job_failed:
            conn.commit()
            return
        cursor.execute(
            """
            UPDATE document_derivations SET
                status = 'error',
                error_message = %s,
                completed_at = COALESCE(completed_at, NOW()),
                updated_at = NOW()
            WHERE id = %s AND document_version_id = %s AND document_id = %s
              AND tenant_id = %s AND user_id = %s
              AND processing_generation = %s
            """,
            (
                str(error)[:1000],
                derivation_id,
                document_version_id,
                doc_id,
                tenant_id,
                user_id,
                processing_generation,
            ),
        )
        cursor.execute(
            """
            UPDATE document_versions
            SET pending_derivation_id = NULL, updated_at = NOW()
            WHERE id = %s AND document_id = %s
              AND tenant_id = %s AND user_id = %s
              AND pending_derivation_id = %s
            """,
            (
                document_version_id,
                doc_id,
                tenant_id,
                user_id,
                derivation_id,
            ),
        )
        shadow_failed = cursor.rowcount == 1
        if shadow_failed:
            cursor.execute(
                """
                UPDATE documents
                SET metadata = (metadata - 'pending_chunk_strategy')
                    || jsonb_build_object(
                        'last_rechunk_error', %s,
                        'rechunk_failed_at', NOW()
                    ),
                    updated_at = NOW()
                WHERE id = %s AND current_version_id = %s
                  AND tenant_id = %s AND user_id = %s
                  AND deleted_at IS NULL
                """,
                (
                    str(error)[:1000],
                    doc_id,
                    document_version_id,
                    tenant_id,
                    user_id,
                ),
            )
        cursor.execute(
            """
            UPDATE document_versions SET
                status = 'error',
                error_message = %s,
                updated_at = NOW()
            WHERE id = %s AND document_id = %s
              AND current_derivation_id = %s
              AND processing_generation = %s
            """,
            (
                str(error)[:1000],
                document_version_id,
                doc_id,
                derivation_id,
                processing_generation,
            ),
        )
        cursor.execute(
            """
            UPDATE documents SET
                status = 'error',
                error_message = %s,
                updated_at = NOW()
            WHERE id = %s AND current_version_id = %s
              AND tenant_id = %s AND user_id = %s
              AND processing_generation = %s
              AND deleted_at IS NULL
            """,
            (
                str(error)[:1000],
                doc_id,
                document_version_id,
                tenant_id,
                user_id,
                processing_generation,
            ),
        )
    conn.commit()


def process_message(conn, redis_client, message_id: str, fields: dict):
    last_error: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        processing_owner = f"{CONSUMER_NAME}:{uuid.uuid4()}"
        lease_state = {"acquired": False}
        try:
            process_batch(
                conn,
                redis_client,
                message_id,
                fields,
                processing_owner,
                lease_state,
            )
            return conn
        except Exception as error:
            last_error = error
            backoff_seconds = 2 ** attempt
            logger.error(
                "Embedding message %s failed (attempt %s/%s): %s",
                message_id,
                attempt,
                MAX_RETRIES,
                error,
            )
            if lease_state["acquired"]:
                try:
                    released = release_processing_lease(
                        conn,
                        fields,
                        processing_owner,
                        error,
                    )
                except Exception:
                    conn = reconnect_database(conn)
                    released = release_processing_lease(
                        conn,
                        fields,
                        processing_owner,
                        error,
                    )
                if not released:
                    logger.info(
                        "Embedding job for message %s lost its lease; discarding stale delivery",
                        message_id,
                    )
                    acknowledge_message(redis_client, message_id)
                    return conn
            conn = reconnect_database(conn)
            if isinstance(error, EmbeddingProfileMismatchError):
                break
            if attempt < MAX_RETRIES:
                time.sleep(backoff_seconds)

    logger.error("Moving embedding message %s to %s", message_id, DLQ_KEY)
    assert last_error is not None
    mark_document_failed(conn, fields, last_error)
    redis_client.xadd(
        DLQ_KEY,
        {**fields, "error": str(last_error)[:1000]},
        maxlen=EMBEDDING_DLQ_MAX_LENGTH,
        approximate=True,
    )
    acknowledge_message(redis_client, message_id)
    return conn


def process_one_embedding_generation_batch(conn) -> bool:
    """Process at most one fair, profile-compatible PostgreSQL generation batch."""
    lease_owner = str(uuid.uuid4())
    try:
        with conn.cursor() as cursor:
            candidates = claim_next_embedding_generation_batch(
                cursor,
                embedding_profile=ACTIVE_EMBEDDING_PROFILE.identifier,
                lease_owner=lease_owner,
                batch_size=EMBEDDING_GENERATION_BATCH_SIZE,
                lease_seconds=EMBEDDING_GENERATION_LEASE_SECONDS,
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    if not candidates:
        return False

    generation_id = candidates[0].generation_id
    try:
        embeddings, provider_model = get_embeddings(
            [candidate.embedding_input for candidate in candidates],
            ACTIVE_EMBEDDING_PROFILE.identifier,
        )
    except Exception as error:
        error_message = str(error).strip() or type(error).__name__
        retry_exponent = min(
            EMBEDDING_GENERATION_MAX_ATTEMPTS - 1,
            max(candidate.attempt_count - 1 for candidate in candidates),
        )
        retry_delay = min(
            EMBEDDING_GENERATION_RETRY_MAX_SECONDS,
            EMBEDDING_GENERATION_RETRY_BASE_SECONDS
            * (2 ** retry_exponent),
        )
        try:
            with conn.cursor() as cursor:
                exhausted = record_embedding_generation_failure(
                    cursor,
                    candidates=candidates,
                    lease_owner=lease_owner,
                    error_code="embedding_provider_error",
                    error_message=error_message,
                    retry_delay_seconds=retry_delay,
                    max_attempts=EMBEDDING_GENERATION_MAX_ATTEMPTS,
                )
            conn.commit()
        except EmbeddingGenerationLeaseLostError:
            conn.rollback()
            logger.info(
                "Discarded stale provider failure for embedding generation %s",
                generation_id,
            )
            return True
        logger.error(
            "Embedding generation %s batch failed; retry in %ss%s: %s",
            generation_id,
            retry_delay,
            " and generation exhausted" if exhausted else "",
            error_message,
        )
        return True

    try:
        with conn.cursor() as cursor:
            record_embedding_generation_batch(
                cursor,
                candidates=candidates,
                lease_owner=lease_owner,
                embeddings=embeddings,
                provider_metadata={
                    "embedding_provider": ACTIVE_EMBEDDING_PROFILE.provider,
                    "embedding_model_requested": ACTIVE_EMBEDDING_PROFILE.model,
                    "embedding_model_returned": provider_model,
                    "embedding_profile": ACTIVE_EMBEDDING_PROFILE.identifier,
                },
            )
        conn.commit()
    except EmbeddingGenerationLeaseLostError:
        conn.rollback()
        logger.info(
            "Discarded stale provider result for embedding generation %s",
            generation_id,
        )
        return True
    except Exception:
        conn.rollback()
        raise

    logger.info(
        "Embedded generation %s batch (%s candidates)",
        generation_id,
        len(candidates),
    )
    return True


def prune_terminal_embedding_generations(conn) -> tuple[int, int]:
    """Delete one bounded batch of safely expired generation-derived data."""
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT detached_predecessors, deleted_generations
                FROM prune_embedding_generations(%s, %s)
                """,
                (
                    EMBEDDING_GENERATION_RETENTION_DAYS,
                    EMBEDDING_GENERATION_PRUNE_BATCH_SIZE,
                ),
            )
            row = cursor.fetchone()
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return int(row[0]), int(row[1])


def claim_stale_messages(redis_client):
    claimed = redis_client.xautoclaim(
        STREAM_KEY,
        GROUP_NAME,
        CONSUMER_NAME,
        min_idle_time=PENDING_IDLE_MS,
        start_id="0-0",
        count=1,
    )
    return claimed[1] if len(claimed) > 1 else []


def create_redis_client():
    return redis.Redis.from_url(
        REDIS_URL,
        **redis_connection_options(
            connect_timeout_seconds=REDIS_CONNECT_TIMEOUT_SECONDS,
            socket_timeout_seconds=REDIS_SOCKET_TIMEOUT_SECONDS,
        ),
    )


def embedding_queue_metadata(conn) -> dict:
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute("SET LOCAL statement_timeout = '2000ms'")
            cursor.execute(
                """
                WITH bounded AS (
                    (
                        SELECT status, created_at
                        FROM document_embedding_jobs
                        WHERE status IN (
                            'pending', 'publishing', 'published', 'processing'
                        )
                        ORDER BY created_at, id
                        LIMIT 10001
                    )
                    UNION ALL
                    (
                        SELECT status, created_at
                        FROM document_embedding_jobs
                        WHERE status = 'failed'
                        ORDER BY created_at, id
                        LIMIT 10001
                    )
                )
                SELECT
                    LEAST(10000, COUNT(*) FILTER (
                        WHERE status IN ('pending', 'publishing', 'published', 'processing')
                    )) AS outstanding,
                    COUNT(*) FILTER (
                        WHERE status IN ('pending', 'publishing', 'published', 'processing')
                    ) > 10000 AS outstanding_capped,
                    LEAST(10000, COUNT(*) FILTER (WHERE status = 'failed')) AS failed,
                    COUNT(*) FILTER (WHERE status = 'failed') > 10000 AS failed_capped,
                    COALESCE(
                        EXTRACT(EPOCH FROM (
                            NOW() - MIN(created_at) FILTER (
                                WHERE status IN ('pending', 'publishing', 'published', 'processing')
                            )
                        )),
                        0
                    )::BIGINT AS oldest_outstanding_seconds
                FROM bounded
                """
            )
            row = cursor.fetchone()
            cursor.execute(
                """
                WITH bounded AS (
                    SELECT expected_chunk_count, embedded_chunk_count,
                           failed_chunk_count, created_at
                    FROM workspace_embedding_generations
                    WHERE status = 'building'
                      AND embedding_profile = %s
                    ORDER BY updated_at, id
                    LIMIT 1001
                )
                SELECT LEAST(1000, COUNT(*)) AS generations,
                       COUNT(*) > 1000 AS generations_capped,
                       COALESCE(SUM(
                           expected_chunk_count - embedded_chunk_count
                       ), 0) AS outstanding,
                       COALESCE(SUM(failed_chunk_count), 0) AS failed,
                       COALESCE(
                           EXTRACT(EPOCH FROM (NOW() - MIN(created_at))),
                           0
                       )::BIGINT AS oldest_outstanding_seconds
                FROM bounded
                """,
                (ACTIVE_EMBEDDING_PROFILE.identifier,),
            )
            generation_row = cursor.fetchone()
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {
        "queue": {
            "outstanding": int(row["outstanding"] or 0),
            "outstanding_capped": bool(row["outstanding_capped"]),
            "failed": int(row["failed"] or 0),
            "failed_capped": bool(row["failed_capped"]),
            "oldest_outstanding_seconds": max(
                0, int(row["oldest_outstanding_seconds"] or 0)
            ),
        },
        "generation_queue": {
            "generations": int(generation_row["generations"] or 0),
            "generations_capped": bool(generation_row["generations_capped"]),
            "outstanding": int(generation_row["outstanding"] or 0),
            "failed": int(generation_row["failed"] or 0),
            "oldest_outstanding_seconds": max(
                0, int(generation_row["oldest_outstanding_seconds"] or 0)
            ),
        },
        "embedding_profile": ACTIVE_EMBEDDING_PROFILE.identifier,
    }


def record_worker_heartbeat(status: str, metadata: dict) -> None:
    write_worker_heartbeat(
        DATABASE_URL,
        WORKER_IDENTITY,
        status,
        metadata,
        connect_timeout_seconds=DATABASE_CONNECT_TIMEOUT_SECONDS,
    )


def request_stop(signum, _frame) -> None:
    logger.info("Received signal %s; draining the embedding worker", signum)
    STOP_REQUESTED.set()


def connect_dependencies():
    delay = 1
    while not STOP_REQUESTED.is_set():
        conn = None
        redis_client = None
        try:
            conn = reconnect_database(None)
            redis_client = create_redis_client()
            redis_client.ping()
            return conn, redis_client
        except Exception as error:
            logger.warning(
                "Embedding worker dependency connection failed; retrying in %ss: %s",
                delay,
                error,
            )
            if conn is not None:
                conn.close()
            if redis_client is not None:
                redis_client.close()
            STOP_REQUESTED.wait(delay)
            delay = min(delay * 2, 30)
    return None, None


def run_worker():
    logger.info("Certus Embedding Worker starting (consumer=%s)", CONSUMER_NAME)
    STOP_REQUESTED.clear()
    for handled_signal in (signal.SIGINT, signal.SIGTERM):
        signal.signal(handled_signal, request_stop)
    conn, r = connect_dependencies()
    if conn is None or r is None:
        return

    last_metadata = {
        "queue": {
            "outstanding": 0,
            "failed": 0,
            "oldest_outstanding_seconds": 0,
        },
        "generation_queue": {
            "generations": 0,
            "generations_capped": False,
            "outstanding": 0,
            "failed": 0,
            "oldest_outstanding_seconds": 0,
        },
        "embedding_profile": ACTIVE_EMBEDDING_PROFILE.identifier,
    }
    try:
        try:
            r.xgroup_create(STREAM_KEY, GROUP_NAME, id="0", mkstream=True)
            logger.info("Consumer group '%s' created on stream '%s'", GROUP_NAME, STREAM_KEY)
        except Exception as error:
            if "BUSYGROUP" not in str(error):
                raise

        logger.info(
            "Listening for document embedding jobs on Redis and generation leases in PostgreSQL"
        )
        last_pending_claim_at = 0.0
        last_heartbeat_at = 0.0
        last_generation_prune_at = 0.0

        while not STOP_REQUESTED.is_set():
            try:
                now = time.monotonic()
                if now - last_heartbeat_at >= WORKER_HEARTBEAT_INTERVAL_SECONDS:
                    last_metadata = embedding_queue_metadata(conn)
                    record_worker_heartbeat("running", last_metadata)
                    last_heartbeat_at = now

                if (
                    now - last_generation_prune_at
                    >= EMBEDDING_GENERATION_PRUNE_INTERVAL_SECONDS
                ):
                    detached, deleted = prune_terminal_embedding_generations(conn)
                    if detached or deleted:
                        logger.info(
                            "Embedding generation retention detached %s predecessors "
                            "and deleted %s terminal generations",
                            detached,
                            deleted,
                        )
                    last_generation_prune_at = now

                stale_messages = []
                if now - last_pending_claim_at >= 30:
                    stale_messages = claim_stale_messages(r)
                    last_pending_claim_at = now

                entries = r.xreadgroup(
                    GROUP_NAME,
                    CONSUMER_NAME,
                    {STREAM_KEY: ">"},
                    count=1,
                    block=500,
                )

                messages = list(stale_messages)
                for _, stream_messages in entries:
                    messages.extend(stream_messages)

                generation_worked = False
                if messages:
                    for message_id, fields in messages:
                        conn = process_message(conn, r, message_id, fields)
                        generation_worked = (
                            process_one_embedding_generation_batch(conn)
                            or generation_worked
                        )
                else:
                    generation_worked = process_one_embedding_generation_batch(conn)

                # Alternating one durable generation batch with each Redis job
                # prevents either queue from monopolizing this worker. When the
                # Redis side is idle, one generation batch is still attempted.
                if not messages and not generation_worked:
                    STOP_REQUESTED.wait(0.5)

            except Exception as loop_error:
                logger.error("Worker main loop error: %s", loop_error, exc_info=True)
                if STOP_REQUESTED.is_set():
                    break
                try:
                    conn = reconnect_database(conn)
                except Exception as reconnect_error:
                    logger.warning("Database reconnect failed: %s", reconnect_error)
                STOP_REQUESTED.wait(2)
    finally:
        try:
            record_worker_heartbeat("draining", last_metadata)
        except Exception as error:
            logger.warning("Could not record draining heartbeat: %s", error)
        try:
            close_openai_client()
        except Exception as error:
            logger.warning("Could not close the OpenAI client cleanly: %s", error)
        try:
            conn.close()
        except Exception as error:
            logger.warning("Could not close the database connection cleanly: %s", error)
        try:
            r.close()
        except Exception as error:
            logger.warning("Could not close the Redis connection cleanly: %s", error)
        try:
            record_worker_heartbeat("stopped", last_metadata)
        except Exception as error:
            logger.warning("Could not record stopped heartbeat: %s", error)
        logger.info("Embedding worker stopped")

if __name__ == "__main__":
    run_worker()
