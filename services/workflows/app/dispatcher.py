import asyncio
import json
import logging
import os
import uuid
import time
from typing import Any, Dict, List

from psycopg2.extras import RealDictCursor
import redis.asyncio as redis
from temporalio.client import Client
from temporalio.common import WorkflowIDConflictPolicy, WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError

from services.shared.worker_runtime import (
    bounded_int_env,
    bounded_retention_days_env,
    connect_database,
    redis_connection_options,
)


logger = logging.getLogger("certus_automation_dispatcher")
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://nexus:nexus_dev_password@localhost:5432/nexus")
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")
TASK_QUEUE = os.getenv("TEMPORAL_TASK_QUEUE", "certus-workflows")
STREAM_KEY = "automation:events"
GROUP_NAME = "automation-dispatchers"
PENDING_IDLE_MS = 60_000
OUTBOX_DISPATCH_LOCK_TIMEOUT_SECONDS = 60
OUTBOX_DISPATCH_BATCH_SIZE = 100
OUTBOX_PUBLISHED_RECOVERY_SECONDS = int(
    os.getenv("OUTBOX_PUBLISHED_RECOVERY_SECONDS", "300")
)
AUTOMATION_EVENT_RETENTION_DAYS = bounded_retention_days_env(
    "AUTOMATION_EVENT_RETENTION_DAYS", 30
)
EMBEDDING_STREAM_KEY = "embedding:jobs"
EMBEDDING_JOB_RETENTION_DAYS = bounded_retention_days_env(
    "EMBEDDING_JOB_RETENTION_DAYS", 7
)
EMBEDDING_PROCESSING_LEASE_SECONDS = int(
    os.getenv("EMBEDDING_PROCESSING_LEASE_SECONDS", "300")
)
WEBHOOK_DISPATCH_BATCH_SIZE = 20
WEBHOOK_DISPATCH_LOCK_TIMEOUT_SECONDS = 60
NOTIFICATION_DISPATCH_BATCH_SIZE = 100
NOTIFICATION_DISPATCH_LOCK_TIMEOUT_SECONDS = 60
NOTIFICATION_GATEWAY_STREAM_KEY = "notifications:gateway"
NOTIFICATION_TENANT_STREAM_MAX_LENGTH = 10_000
NOTIFICATION_GATEWAY_STREAM_MAX_LENGTH = 50_000
NOTIFICATION_EVENT_RETENTION_DAYS = bounded_retention_days_env(
    "NOTIFICATION_EVENT_RETENTION_DAYS", 30
)
NOTIFICATION_EVENT_PRUNE_INTERVAL_SECONDS = 60 * 60
NOTIFICATION_EVENT_PRUNE_BATCH_SIZE = 10_000
REALTIME_DISPATCH_BATCH_SIZE = 100
REALTIME_DISPATCH_LOCK_TIMEOUT_SECONDS = 60
REALTIME_GATEWAY_STREAM_KEY = "realtime:gateway"
REALTIME_TENANT_STREAM_MAX_LENGTH = 10_000
REALTIME_GATEWAY_STREAM_MAX_LENGTH = 50_000
REALTIME_EVENT_RETENTION_DAYS = bounded_retention_days_env(
    "REALTIME_EVENT_RETENTION_DAYS", 30
)
REALTIME_EVENT_PRUNE_INTERVAL_SECONDS = 60 * 60
REALTIME_EVENT_PRUNE_BATCH_SIZE = 10_000
DATABASE_CONNECT_TIMEOUT_SECONDS = bounded_int_env(
    "WORKFLOWS_DB_CONNECT_TIMEOUT_SECONDS", 3, 1, 30
)
REDIS_CONNECT_TIMEOUT_SECONDS = bounded_int_env(
    "WORKFLOWS_REDIS_CONNECT_TIMEOUT_SECONDS", 3, 1, 30
)
REDIS_SOCKET_TIMEOUT_SECONDS = bounded_int_env(
    "WORKFLOWS_REDIS_SOCKET_TIMEOUT_SECONDS", 5, 3, 60
)
WORKER_DB_STATEMENT_TIMEOUT_MS = bounded_int_env(
    "WORKER_DB_STATEMENT_TIMEOUT_MS", 15_000, 1_000, 120_000
)
WORKER_DB_LOCK_TIMEOUT_MS = bounded_int_env(
    "WORKER_DB_LOCK_TIMEOUT_MS", 3_000, 500, 30_000
)

if not 60 <= EMBEDDING_PROCESSING_LEASE_SECONDS <= 3_600:
    raise ValueError("EMBEDDING_PROCESSING_LEASE_SECONDS must be between 60 and 3600")
if not 60 <= OUTBOX_PUBLISHED_RECOVERY_SECONDS <= 3_600:
    raise ValueError("OUTBOX_PUBLISHED_RECOVERY_SECONDS must be between 60 and 3600")


def database_connection():
    return connect_database(
        DATABASE_URL,
        application_name="certus-workflows",
        connect_timeout_seconds=DATABASE_CONNECT_TIMEOUT_SECONDS,
        statement_timeout_ms=WORKER_DB_STATEMENT_TIMEOUT_MS,
        lock_timeout_ms=WORKER_DB_LOCK_TIMEOUT_MS,
    )


def async_redis_client():
    return redis.from_url(
        REDIS_URL,
        **redis_connection_options(
            connect_timeout_seconds=REDIS_CONNECT_TIMEOUT_SECONDS,
            socket_timeout_seconds=REDIS_SOCKET_TIMEOUT_SECONDS,
        ),
    )


def claim_automation_events() -> List[Dict[str, Any]]:
    with database_connection() as connection:
        with connection.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute(
                """
                WITH candidates AS (
                    SELECT id
                    FROM automation_events
                    WHERE (
                            status = 'pending' AND available_at <= NOW()
                          )
                       OR (
                            status = 'publishing'
                            AND locked_at < NOW() - (%s * INTERVAL '1 second')
                          )
                       OR (
                            status = 'published'
                            AND published_at < NOW() - (%s * INTERVAL '1 second')
                          )
                    ORDER BY available_at, created_at, id
                    FOR UPDATE SKIP LOCKED
                    LIMIT %s
                )
                UPDATE automation_events AS event
                SET status = 'publishing', locked_at = NOW(),
                    publish_attempts = event.publish_attempts + 1,
                    last_error = NULL, updated_at = NOW()
                FROM candidates
                WHERE event.id = candidates.id
                RETURNING event.id, event.payload, event.publish_attempts
                """,
                (
                    OUTBOX_DISPATCH_LOCK_TIMEOUT_SECONDS,
                    OUTBOX_PUBLISHED_RECOVERY_SECONDS,
                    OUTBOX_DISPATCH_BATCH_SIZE,
                ),
            )
            return [dict(event) for event in cursor.fetchall()]


def mark_automation_event_published(event_id: str, redis_stream_id: str) -> None:
    with database_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE automation_events
                SET status = 'published', redis_stream_id = %s,
                    published_at = NOW(),
                    locked_at = NULL, last_error = NULL, updated_at = NOW()
                WHERE id = %s AND status IN ('publishing', 'published')
                """,
                (redis_stream_id, event_id),
            )


def release_automation_event(event_id: str, publish_attempts: int, error: Exception) -> None:
    backoff_seconds = min(300, 2 ** min(publish_attempts, 8))
    with database_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE automation_events
                SET status = 'pending', locked_at = NULL,
                    available_at = NOW() + (%s * INTERVAL '1 second'),
                    last_error = %s, updated_at = NOW()
                WHERE id = %s AND status = 'publishing'
                """,
                (backoff_seconds, str(error)[:1_000], event_id),
            )


def mark_automation_event_processed(event_id: str) -> None:
    with database_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE automation_events
                SET status = 'processed', processed_at = COALESCE(processed_at, NOW()),
                    locked_at = NULL, last_error = NULL, updated_at = NOW()
                WHERE id = %s AND status IN ('publishing', 'published', 'processed')
                """,
                (event_id,),
            )


def prune_processed_automation_events() -> int:
    with database_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                WITH expired AS (
                    SELECT id
                    FROM automation_events
                    WHERE status = 'processed'
                      AND processed_at < NOW() - (%s * INTERVAL '1 day')
                    ORDER BY processed_at, id
                    LIMIT %s
                )
                DELETE FROM automation_events AS event
                USING expired
                WHERE event.id = expired.id
                """,
                (AUTOMATION_EVENT_RETENTION_DAYS, NOTIFICATION_EVENT_PRUNE_BATCH_SIZE),
            )
            return cursor.rowcount


def claim_embedding_jobs() -> List[Dict[str, Any]]:
    with database_connection() as connection:
        with connection.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute(
                """
                WITH candidates AS (
                    SELECT job.id
                    FROM document_embedding_jobs AS job
                    JOIN documents AS document ON document.id = job.document_id
                    JOIN document_versions AS version
                      ON version.id = job.document_version_id
                     AND version.document_id = job.document_id
                    JOIN document_derivations AS derivation
                      ON derivation.id = job.derivation_id
                     AND derivation.document_version_id = job.document_version_id
                     AND derivation.document_id = job.document_id
                    WHERE document.deleted_at IS NULL
                      AND (
                            (
                                version.current_derivation_id = job.derivation_id
                                AND version.processing_generation = job.processing_generation
                                AND version.status = 'processing'
                            )
                            OR version.pending_derivation_id = job.derivation_id
                      )
                      AND derivation.processing_generation = job.processing_generation
                      AND derivation.status = 'processing'
                      AND (
                            (job.status = 'pending' AND job.available_at <= NOW())
                         OR (
                              job.status = 'publishing'
                              AND job.locked_at < NOW() - (%s * INTERVAL '1 second')
                            )
                         OR (
                              job.status = 'processing'
                              AND job.locked_at < NOW() - (%s * INTERVAL '1 second')
                            )
                         OR (
                              job.status = 'published'
                              AND job.published_at < NOW() - (%s * INTERVAL '1 second')
                            )
                      )
                    ORDER BY job.available_at, job.created_at, job.id
                    FOR UPDATE OF job SKIP LOCKED
                    LIMIT %s
                )
                UPDATE document_embedding_jobs AS job
                SET status = 'publishing', locked_at = NOW(),
                    processing_owner = NULL,
                    publish_attempts = job.publish_attempts + 1,
                    last_error = NULL, updated_at = NOW()
                FROM candidates
                WHERE job.id = candidates.id
                RETURNING job.id, job.document_id, job.document_version_id,
                          job.derivation_id, job.tenant_id, job.user_id,
                          job.processing_generation, job.batch_start, job.batch_end,
                          job.total_chunks, job.embedding_profile,
                          job.publish_attempts
                """,
                (
                    OUTBOX_DISPATCH_LOCK_TIMEOUT_SECONDS,
                    EMBEDDING_PROCESSING_LEASE_SECONDS,
                    OUTBOX_PUBLISHED_RECOVERY_SECONDS,
                    OUTBOX_DISPATCH_BATCH_SIZE,
                ),
            )
            return [dict(job) for job in cursor.fetchall()]


def mark_embedding_job_published(job_id: str, redis_stream_id: str) -> None:
    with database_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE document_embedding_jobs
                SET status = 'published', redis_stream_id = %s,
                    published_at = NOW(),
                    locked_at = NULL, processing_owner = NULL,
                    last_error = NULL, updated_at = NOW()
                WHERE id = %s AND status IN ('publishing', 'published')
                """,
                (redis_stream_id, job_id),
            )


def release_embedding_job(job_id: str, publish_attempts: int, error: Exception) -> None:
    backoff_seconds = min(300, 2 ** min(publish_attempts, 8))
    with database_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE document_embedding_jobs
                SET status = 'pending', locked_at = NULL, processing_owner = NULL,
                    available_at = NOW() + (%s * INTERVAL '1 second'),
                    last_error = %s, updated_at = NOW()
                WHERE id = %s AND status = 'publishing'
                """,
                (backoff_seconds, str(error)[:1_000], job_id),
            )


def prune_terminal_embedding_jobs() -> int:
    with database_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                WITH expired AS (
                    SELECT id
                    FROM document_embedding_jobs
                    WHERE status IN ('processed', 'failed', 'obsolete')
                      AND processed_at < NOW() - (%s * INTERVAL '1 day')
                    ORDER BY processed_at, id
                    LIMIT %s
                )
                DELETE FROM document_embedding_jobs AS job
                USING expired
                WHERE job.id = expired.id
                """,
                (EMBEDDING_JOB_RETENTION_DAYS, NOTIFICATION_EVENT_PRUNE_BATCH_SIZE),
            )
            return cursor.rowcount


def load_matching_rules(event: Dict[str, Any]) -> List[Dict[str, Any]]:
    with database_connection() as connection:
        with connection.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute(
                """
                SELECT id
                FROM automation_rules
                WHERE tenant_id = %s
                  AND user_id = %s
                  AND trigger_event = %s
                  AND is_enabled = true
                ORDER BY created_at
                """,
                (event["tenant_id"], event["user_id"], event["type"]),
            )
            return [dict(rule) for rule in cursor.fetchall()]


def claim_webhook_events() -> List[Dict[str, Any]]:
    """Claim queue rows without blocking another dispatcher process."""
    with database_connection() as connection:
        with connection.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute(
                """
                WITH candidates AS (
                    SELECT id
                    FROM webhook_events
                    WHERE (
                            status = 'pending'
                            AND available_at <= NOW()
                          )
                       OR (
                            status = 'dispatching'
                            AND locked_at < NOW() - (%s * INTERVAL '1 second')
                          )
                    ORDER BY available_at, created_at, id
                    FOR UPDATE SKIP LOCKED
                    LIMIT %s
                )
                UPDATE webhook_events AS event
                SET status = 'dispatching',
                    locked_at = NOW(),
                    dispatch_attempts = event.dispatch_attempts + 1,
                    updated_at = NOW(),
                    last_error = NULL
                FROM candidates
                WHERE event.id = candidates.id
                RETURNING event.id, event.organization_id AS tenant_id,
                          event.user_id, event.event_type, event.payload,
                          event.dispatch_attempts,
                          EXTRACT(EPOCH FROM event.created_at)::BIGINT AS created_at
                """,
                (WEBHOOK_DISPATCH_LOCK_TIMEOUT_SECONDS, WEBHOOK_DISPATCH_BATCH_SIZE),
            )
            return [dict(event) for event in cursor.fetchall()]


def mark_webhook_event_dispatched(
    event_id: str,
    workflow_id: str,
    workflow_run_id: str,
) -> None:
    with database_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE webhook_events
                SET status = 'dispatched', workflow_id = %s,
                    workflow_run_id = COALESCE(NULLIF(%s, ''), workflow_run_id),
                    dispatched_at = COALESCE(dispatched_at, NOW()), locked_at = NULL,
                    updated_at = NOW(), last_error = NULL
                WHERE id = %s AND status IN ('dispatching', 'dispatched')
                """,
                (workflow_id, workflow_run_id, event_id),
            )


def release_webhook_event(event_id: str, dispatch_attempts: int, error: Exception) -> None:
    backoff_seconds = min(300, 2 ** min(dispatch_attempts, 8))
    with database_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE webhook_events
                SET status = 'pending', locked_at = NULL,
                    available_at = NOW() + (%s * INTERVAL '1 second'),
                    last_error = %s, updated_at = NOW()
                WHERE id = %s AND status = 'dispatching'
                """,
                (backoff_seconds, str(error)[:1_000], event_id),
            )


def claim_notification_events() -> List[Dict[str, Any]]:
    """Claim committed notification outbox rows for Redis publication."""
    with database_connection() as connection:
        with connection.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute(
                """
                WITH candidates AS (
                    SELECT id
                    FROM notification_events
                    WHERE (
                            status = 'pending'
                            AND available_at <= NOW()
                          )
                       OR (
                            status = 'publishing'
                            AND locked_at < NOW() - (%s * INTERVAL '1 second')
                          )
                    ORDER BY available_at, created_at, id
                    FOR UPDATE SKIP LOCKED
                    LIMIT %s
                )
                UPDATE notification_events AS event
                SET status = 'publishing',
                    locked_at = NOW(),
                    publish_attempts = event.publish_attempts + 1,
                    updated_at = NOW(),
                    last_error = NULL
                FROM candidates
                WHERE event.id = candidates.id
                RETURNING event.id, event.notification_id,
                          event.organization_id AS tenant_id,
                          event.user_id, event.event_type, event.payload,
                          event.publish_attempts,
                          EXTRACT(EPOCH FROM event.created_at)::BIGINT AS created_at
                """,
                (
                    NOTIFICATION_DISPATCH_LOCK_TIMEOUT_SECONDS,
                    NOTIFICATION_DISPATCH_BATCH_SIZE,
                ),
            )
            return [dict(event) for event in cursor.fetchall()]


def mark_notification_event_published(event_id: str, redis_stream_id: str) -> None:
    with database_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE notification_events
                SET status = 'published', redis_stream_id = %s,
                    published_at = COALESCE(published_at, NOW()), locked_at = NULL,
                    updated_at = NOW(), last_error = NULL
                WHERE id = %s AND status IN ('publishing', 'published')
                """,
                (redis_stream_id, event_id),
            )


def release_notification_event(event_id: str, publish_attempts: int, error: Exception) -> None:
    backoff_seconds = min(300, 2 ** min(publish_attempts, 8))
    with database_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE notification_events
                SET status = 'pending', locked_at = NULL,
                    available_at = NOW() + (%s * INTERVAL '1 second'),
                    last_error = %s, updated_at = NOW()
                WHERE id = %s AND status = 'publishing'
                """,
                (backoff_seconds, str(error)[:1_000], event_id),
            )


def prune_published_notification_events() -> int:
    with database_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                WITH expired AS (
                    SELECT id
                    FROM notification_events
                    WHERE status = 'published'
                      AND published_at < NOW() - (%s * INTERVAL '1 day')
                    ORDER BY published_at, id
                    LIMIT %s
                )
                DELETE FROM notification_events AS event
                USING expired
                WHERE event.id = expired.id
                """,
                (
                    NOTIFICATION_EVENT_RETENTION_DAYS,
                    NOTIFICATION_EVENT_PRUNE_BATCH_SIZE,
                ),
            )
            return cursor.rowcount


def claim_realtime_events() -> List[Dict[str, Any]]:
    """Claim committed product-state events for Redis publication."""
    with database_connection() as connection:
        with connection.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute(
                """
                WITH candidates AS (
                    SELECT id
                    FROM realtime_events
                    WHERE (
                            status = 'pending'
                            AND available_at <= NOW()
                          )
                       OR (
                            status = 'publishing'
                            AND locked_at < NOW() - (%s * INTERVAL '1 second')
                          )
                    ORDER BY available_at, created_at, id
                    FOR UPDATE SKIP LOCKED
                    LIMIT %s
                )
                UPDATE realtime_events AS event
                SET status = 'publishing',
                    locked_at = NOW(),
                    publish_attempts = event.publish_attempts + 1,
                    updated_at = NOW(),
                    last_error = NULL
                FROM candidates
                WHERE event.id = candidates.id
                RETURNING event.id, event.organization_id AS tenant_id,
                          event.user_id, event.channel, event.event_type,
                          event.payload, event.publish_attempts,
                          EXTRACT(EPOCH FROM event.created_at)::BIGINT AS created_at
                """,
                (REALTIME_DISPATCH_LOCK_TIMEOUT_SECONDS, REALTIME_DISPATCH_BATCH_SIZE),
            )
            return [dict(event) for event in cursor.fetchall()]


def mark_realtime_event_published(event_id: str, redis_stream_id: str) -> None:
    with database_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE realtime_events
                SET status = 'published', redis_stream_id = %s,
                    published_at = COALESCE(published_at, NOW()), locked_at = NULL,
                    updated_at = NOW(), last_error = NULL
                WHERE id = %s AND status IN ('publishing', 'published')
                """,
                (redis_stream_id, event_id),
            )


def release_realtime_event(event_id: str, publish_attempts: int, error: Exception) -> None:
    backoff_seconds = min(300, 2 ** min(publish_attempts, 8))
    with database_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE realtime_events
                SET status = 'pending', locked_at = NULL,
                    available_at = NOW() + (%s * INTERVAL '1 second'),
                    last_error = %s, updated_at = NOW()
                WHERE id = %s AND status = 'publishing'
                """,
                (backoff_seconds, str(error)[:1_000], event_id),
            )


def prune_published_realtime_events() -> int:
    with database_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                WITH expired AS (
                    SELECT id
                    FROM realtime_events
                    WHERE status = 'published'
                      AND published_at < NOW() - (%s * INTERVAL '1 day')
                    ORDER BY published_at, id
                    LIMIT %s
                )
                DELETE FROM realtime_events AS event
                USING expired
                WHERE event.id = expired.id
                """,
                (REALTIME_EVENT_RETENTION_DAYS, REALTIME_EVENT_PRUNE_BATCH_SIZE),
            )
            return cursor.rowcount


class AutomationEventOutboxDispatcher:
    def __init__(self):
        self.redis = async_redis_client()

    async def publish_event(self, event: Dict[str, Any]) -> None:
        event_id = str(event["id"])
        stream_id = await self.redis.xadd(
            STREAM_KEY,
            {
                "outbox_id": event_id,
                "event_json": json.dumps(
                    dict(event["payload"] or {}),
                    separators=(",", ":"),
                ),
            },
        )
        await asyncio.to_thread(
            mark_automation_event_published,
            event_id,
            str(stream_id),
        )

    async def run(self) -> None:
        logger.info("Automation event publisher polling the PostgreSQL outbox")
        last_prune_at = 0.0
        while True:
            now = time.monotonic()
            if now - last_prune_at >= NOTIFICATION_EVENT_PRUNE_INTERVAL_SECONDS:
                pruned_count = await asyncio.to_thread(prune_processed_automation_events)
                if pruned_count:
                    logger.info("Pruned %s processed automation events", pruned_count)
                last_prune_at = now
            events = await asyncio.to_thread(claim_automation_events)
            if not events:
                await asyncio.sleep(1)
                continue
            for event in events:
                try:
                    await self.publish_event(event)
                except Exception as error:
                    await asyncio.to_thread(
                        release_automation_event,
                        str(event["id"]),
                        int(event["publish_attempts"]),
                        error,
                    )
                    logger.exception(
                        "Automation event %s publication failed: %s",
                        event["id"],
                        error,
                    )


class EmbeddingJobDispatcher:
    def __init__(self):
        self.redis = async_redis_client()

    async def publish_job(self, job: Dict[str, Any]) -> None:
        job_id = str(job["id"])
        stream_id = await self.redis.xadd(
            EMBEDDING_STREAM_KEY,
            {
                "outbox_id": job_id,
                "document_id": str(job["document_id"]),
                "document_version_id": str(job["document_version_id"]),
                "derivation_id": str(job["derivation_id"]),
                "tenant_id": job["tenant_id"],
                "user_id": job["user_id"],
                "processing_generation": str(job["processing_generation"]),
                "embedding_profile": job["embedding_profile"],
                "batch_start": str(job["batch_start"]),
                "batch_end": str(job["batch_end"]),
                "total_chunks": str(job["total_chunks"]),
            },
        )
        await asyncio.to_thread(mark_embedding_job_published, job_id, str(stream_id))

    async def run(self) -> None:
        logger.info("Embedding job publisher polling the PostgreSQL outbox")
        last_prune_at = 0.0
        while True:
            now = time.monotonic()
            if now - last_prune_at >= NOTIFICATION_EVENT_PRUNE_INTERVAL_SECONDS:
                pruned_count = await asyncio.to_thread(prune_terminal_embedding_jobs)
                if pruned_count:
                    logger.info("Pruned %s terminal embedding jobs", pruned_count)
                last_prune_at = now
            jobs = await asyncio.to_thread(claim_embedding_jobs)
            if not jobs:
                await asyncio.sleep(1)
                continue
            for job in jobs:
                try:
                    await self.publish_job(job)
                except Exception as error:
                    await asyncio.to_thread(
                        release_embedding_job,
                        str(job["id"]),
                        int(job["publish_attempts"]),
                        error,
                    )
                    logger.exception(
                        "Embedding job %s publication failed: %s",
                        job["id"],
                        error,
                    )


class AutomationDispatcher:
    def __init__(self, temporal_client: Client):
        self.temporal_client = temporal_client
        self.redis = async_redis_client()
        self.consumer_name = f"dispatcher-{os.getpid()}"

    async def ensure_group(self) -> None:
        try:
            await self.redis.xgroup_create(STREAM_KEY, GROUP_NAME, id="0", mkstream=True)
        except Exception as error:
            if "BUSYGROUP" not in str(error):
                raise

    async def dispatch_event(self, event: Dict[str, Any]) -> None:
        rules = await asyncio.to_thread(load_matching_rules, event)
        for rule in rules:
            execution_id = str(
                uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    f"certus:{rule['id']}:{event['event_id']}",
                )
            )
            workflow_id = f"certus-automation-{execution_id}"
            payload = {
                "execution_id": execution_id,
                "workflow_id": workflow_id,
                "rule_id": str(rule["id"]),
                "tenant_id": event["tenant_id"],
                "user_id": event["user_id"],
                "event": event,
            }
            try:
                await self.temporal_client.start_workflow(
                    "certus.automation.v1",
                    payload,
                    id=workflow_id,
                    task_queue=TASK_QUEUE,
                    id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
                    id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
                )
            except WorkflowAlreadyStartedError:
                logger.info("Automation workflow %s was already dispatched", workflow_id)

    async def run(self) -> None:
        await self.ensure_group()
        logger.info("Automation dispatcher listening on Redis stream %s", STREAM_KEY)
        last_pending_claim_at = 0.0
        while True:
            stale_messages = []
            now = time.monotonic()
            if now - last_pending_claim_at >= 30:
                claimed = await self.redis.xautoclaim(
                    STREAM_KEY,
                    GROUP_NAME,
                    self.consumer_name,
                    min_idle_time=PENDING_IDLE_MS,
                    start_id="0-0",
                    count=10,
                )
                stale_messages = claimed[1] if len(claimed) > 1 else []
                last_pending_claim_at = now

            entries = await self.redis.xreadgroup(
                GROUP_NAME,
                self.consumer_name,
                {STREAM_KEY: ">"},
                count=10,
                block=2_000,
            )
            messages = list(stale_messages)
            for _, stream_messages in entries:
                messages.extend(stream_messages)
            for message_id, fields in messages:
                try:
                    event = json.loads(fields["event_json"])
                    await self.dispatch_event(event)
                    outbox_id = fields.get("outbox_id")
                    if outbox_id:
                        await asyncio.to_thread(mark_automation_event_processed, outbox_id)
                    pipeline = self.redis.pipeline(transaction=True)
                    pipeline.xack(STREAM_KEY, GROUP_NAME, message_id)
                    pipeline.xdel(STREAM_KEY, message_id)
                    await pipeline.execute()
                except Exception as error:
                    logger.exception("Automation event %s dispatch failed: %s", message_id, error)


class WebhookEventDispatcher:
    def __init__(self, temporal_client: Client):
        self.temporal_client = temporal_client

    async def dispatch_event(self, event: Dict[str, Any]) -> None:
        event_id = str(event["id"])
        workflow_id = f"certus-webhook-{event_id}"
        payload = {
            "event_id": event_id,
            "workflow_id": workflow_id,
            "tenant_id": event["tenant_id"],
            "user_id": event["user_id"],
            "event_type": event["event_type"],
            "data": dict(event["payload"] or {}),
            "created_at": int(event["created_at"]),
        }
        try:
            handle = await self.temporal_client.start_workflow(
                "certus.webhook-event.v1",
                payload,
                id=workflow_id,
                task_queue=TASK_QUEUE,
                id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
                id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
            )
            await asyncio.to_thread(
                mark_webhook_event_dispatched,
                event_id,
                workflow_id,
                handle.first_execution_run_id,
            )
        except WorkflowAlreadyStartedError:
            await asyncio.to_thread(
                mark_webhook_event_dispatched,
                event_id,
                workflow_id,
                "",
            )
        except Exception as error:
            await asyncio.to_thread(
                release_webhook_event,
                event_id,
                int(event["dispatch_attempts"]),
                error,
            )
            raise

    async def run(self) -> None:
        logger.info("Webhook event dispatcher polling the PostgreSQL outbox")
        while True:
            events = await asyncio.to_thread(claim_webhook_events)
            if not events:
                await asyncio.sleep(1)
                continue
            for event in events:
                try:
                    await self.dispatch_event(event)
                except Exception as error:
                    logger.exception("Webhook event %s dispatch failed: %s", event["id"], error)


class NotificationEventDispatcher:
    def __init__(self):
        self.redis = async_redis_client()

    async def publish_event(self, event: Dict[str, Any]) -> None:
        event_id = str(event["id"])
        tenant_stream_key = f"notifications:{event['tenant_id']}"
        event_json = json.dumps(
            {
                "event_id": event_id,
                "notification_id": str(event["notification_id"]),
                "tenant_id": event["tenant_id"],
                "user_id": event["user_id"],
                "event_type": event["event_type"],
                "notification": dict(event["payload"] or {}),
                "created_at": int(event["created_at"]),
            },
            separators=(",", ":"),
        )
        fields = {"event_id": event_id, "event_json": event_json}
        pipeline = self.redis.pipeline(transaction=True)
        pipeline.xadd(
            tenant_stream_key,
            fields,
            maxlen=NOTIFICATION_TENANT_STREAM_MAX_LENGTH,
            approximate=True,
        )
        pipeline.xadd(
            NOTIFICATION_GATEWAY_STREAM_KEY,
            fields,
            maxlen=NOTIFICATION_GATEWAY_STREAM_MAX_LENGTH,
            approximate=True,
        )
        stream_ids = await pipeline.execute()
        await asyncio.to_thread(
            mark_notification_event_published,
            event_id,
            str(stream_ids[0]),
        )

    async def run(self) -> None:
        logger.info("Notification dispatcher polling the PostgreSQL outbox")
        last_prune_at = 0.0
        while True:
            now = time.monotonic()
            if now - last_prune_at >= NOTIFICATION_EVENT_PRUNE_INTERVAL_SECONDS:
                pruned_count = await asyncio.to_thread(prune_published_notification_events)
                if pruned_count:
                    logger.info("Pruned %s published notification events", pruned_count)
                last_prune_at = now
            events = await asyncio.to_thread(claim_notification_events)
            if not events:
                await asyncio.sleep(1)
                continue
            for event in events:
                try:
                    await self.publish_event(event)
                except Exception as error:
                    await asyncio.to_thread(
                        release_notification_event,
                        str(event["id"]),
                        int(event["publish_attempts"]),
                        error,
                    )
                    logger.exception(
                        "Notification event %s publication failed: %s",
                        event["id"],
                        error,
                    )


class RealtimeEventDispatcher:
    def __init__(self):
        self.redis = async_redis_client()

    async def publish_event(self, event: Dict[str, Any]) -> None:
        event_id = str(event["id"])
        tenant_stream_key = f"realtime:{event['tenant_id']}"
        event_json = json.dumps(
            {
                "event_id": event_id,
                "tenant_id": event["tenant_id"],
                "user_id": event["user_id"],
                "channel": event["channel"],
                "event_type": event["event_type"],
                "data": dict(event["payload"] or {}),
                "created_at": int(event["created_at"]),
            },
            separators=(",", ":"),
        )
        fields = {"event_id": event_id, "event_json": event_json}
        pipeline = self.redis.pipeline(transaction=True)
        pipeline.xadd(
            tenant_stream_key,
            fields,
            maxlen=REALTIME_TENANT_STREAM_MAX_LENGTH,
            approximate=True,
        )
        pipeline.xadd(
            REALTIME_GATEWAY_STREAM_KEY,
            fields,
            maxlen=REALTIME_GATEWAY_STREAM_MAX_LENGTH,
            approximate=True,
        )
        stream_ids = await pipeline.execute()
        await asyncio.to_thread(
            mark_realtime_event_published,
            event_id,
            str(stream_ids[0]),
        )

    async def run(self) -> None:
        logger.info("Realtime product event dispatcher polling the PostgreSQL outbox")
        last_prune_at = 0.0
        while True:
            now = time.monotonic()
            if now - last_prune_at >= REALTIME_EVENT_PRUNE_INTERVAL_SECONDS:
                pruned_count = await asyncio.to_thread(prune_published_realtime_events)
                if pruned_count:
                    logger.info("Pruned %s published realtime product events", pruned_count)
                last_prune_at = now
            events = await asyncio.to_thread(claim_realtime_events)
            if not events:
                await asyncio.sleep(1)
                continue
            for event in events:
                try:
                    await self.publish_event(event)
                except Exception as error:
                    await asyncio.to_thread(
                        release_realtime_event,
                        str(event["id"]),
                        int(event["publish_attempts"]),
                        error,
                    )
                    logger.exception(
                        "Realtime event %s publication failed: %s",
                        event["id"],
                        error,
                    )
