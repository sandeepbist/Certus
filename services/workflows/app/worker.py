import asyncio
import concurrent.futures
import logging
import os
import signal
from contextlib import suppress
from pathlib import Path

from dotenv import load_dotenv
from temporalio.client import Client
from temporalio.worker import Worker

from app.activities import (
    deliver_webhook_event,
    deliver_reminder,
    execute_automation,
    list_webhook_targets,
    record_automation_failure,
    record_webhook_event_result,
    record_reminder_failure,
)
from app.dispatcher import (
    AutomationDispatcher,
    AutomationEventOutboxDispatcher,
    EmbeddingJobDispatcher,
    NotificationEventDispatcher,
    RealtimeEventDispatcher,
    WebhookEventDispatcher,
)
from app.workflows import AutomationWorkflow, ReminderWorkflow, WebhookEventWorkflow
from services.shared.worker_runtime import (
    WorkerIdentity,
    bounded_int_env,
    connect_database,
    write_worker_heartbeat,
)


MODULE_PATH = Path(__file__).resolve()
REPO_ROOT = next(
    (parent for parent in MODULE_PATH.parents if (parent / ".env").exists()),
    MODULE_PATH.parents[1],
)
load_dotenv(REPO_ROOT / ".env")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("certus_temporal_worker")

TEMPORAL_ADDRESS = os.getenv("TEMPORAL_ADDRESS", "localhost:7233")
TEMPORAL_NAMESPACE = os.getenv("TEMPORAL_NAMESPACE", "nexus")
TASK_QUEUE = os.getenv("TEMPORAL_TASK_QUEUE", "certus-workflows")
DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://nexus:nexus_dev_password@localhost:5432/nexus",
)
DATABASE_CONNECT_TIMEOUT_SECONDS = bounded_int_env(
    "WORKFLOWS_DB_CONNECT_TIMEOUT_SECONDS", 3, 1, 30
)
DEPENDENCY_CONNECT_TIMEOUT_SECONDS = bounded_int_env(
    "WORKFLOWS_DEPENDENCY_CONNECT_TIMEOUT_SECONDS", 5, 1, 30
)
WORKER_HEARTBEAT_INTERVAL_SECONDS = bounded_int_env(
    "WORKER_HEARTBEAT_INTERVAL_SECONDS", 10, 5, 60
)
WORKER_IDENTITY = WorkerIdentity("workflow-worker")


async def connect_temporal() -> Client:
    delay = 1
    while True:
        try:
            return await asyncio.wait_for(
                Client.connect(TEMPORAL_ADDRESS, namespace=TEMPORAL_NAMESPACE),
                timeout=DEPENDENCY_CONNECT_TIMEOUT_SECONDS,
            )
        except Exception as error:
            logger.warning("Temporal connection failed; retrying in %ss: %s", delay, error)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30)


def workflow_queue_metadata() -> dict:
    with connect_database(
        DATABASE_URL,
        application_name="certus-workflow-worker-health",
        connect_timeout_seconds=DATABASE_CONNECT_TIMEOUT_SECONDS,
    ) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SET LOCAL statement_timeout = '2000ms'")
            cursor.execute(
                """
                SELECT jsonb_build_object(
                    'automation', jsonb_build_object(
                        'outstanding', LEAST(10000, COUNT(*) FILTER (
                            WHERE queue_name = 'automation'
                        )),
                        'depth_capped', COUNT(*) FILTER (
                            WHERE queue_name = 'automation'
                        ) > 10000,
                        'oldest_outstanding_seconds', COALESCE(MAX(age_seconds) FILTER (
                            WHERE queue_name = 'automation'
                        ), 0)
                    ),
                    'embedding_publication', jsonb_build_object(
                        'outstanding', LEAST(10000, COUNT(*) FILTER (
                            WHERE queue_name = 'embedding_publication'
                        )),
                        'depth_capped', COUNT(*) FILTER (
                            WHERE queue_name = 'embedding_publication'
                        ) > 10000,
                        'oldest_outstanding_seconds', COALESCE(MAX(age_seconds) FILTER (
                            WHERE queue_name = 'embedding_publication'
                        ), 0)
                    ),
                    'webhook', jsonb_build_object(
                        'outstanding', LEAST(10000, COUNT(*) FILTER (
                            WHERE queue_name = 'webhook'
                        )),
                        'depth_capped', COUNT(*) FILTER (
                            WHERE queue_name = 'webhook'
                        ) > 10000,
                        'oldest_outstanding_seconds', COALESCE(MAX(age_seconds) FILTER (
                            WHERE queue_name = 'webhook'
                        ), 0)
                    ),
                    'notification', jsonb_build_object(
                        'outstanding', LEAST(10000, COUNT(*) FILTER (
                            WHERE queue_name = 'notification'
                        )),
                        'depth_capped', COUNT(*) FILTER (
                            WHERE queue_name = 'notification'
                        ) > 10000,
                        'oldest_outstanding_seconds', COALESCE(MAX(age_seconds) FILTER (
                            WHERE queue_name = 'notification'
                        ), 0)
                    ),
                    'realtime', jsonb_build_object(
                        'outstanding', LEAST(10000, COUNT(*) FILTER (
                            WHERE queue_name = 'realtime'
                        )),
                        'depth_capped', COUNT(*) FILTER (
                            WHERE queue_name = 'realtime'
                        ) > 10000,
                        'oldest_outstanding_seconds', COALESCE(MAX(age_seconds) FILTER (
                            WHERE queue_name = 'realtime'
                        ), 0)
                    )
                )
                FROM (
                    (
                    SELECT 'automation' AS queue_name,
                           EXTRACT(EPOCH FROM (NOW() - created_at))::BIGINT AS age_seconds
                    FROM automation_events
                    WHERE status IN ('pending', 'publishing', 'published')
                    ORDER BY created_at, id
                    LIMIT 10001
                    )
                    UNION ALL
                    (
                    SELECT 'embedding_publication',
                           EXTRACT(EPOCH FROM (NOW() - created_at))::BIGINT
                    FROM document_embedding_jobs
                    WHERE status IN ('pending', 'publishing')
                    ORDER BY created_at, id
                    LIMIT 10001
                    )
                    UNION ALL
                    (
                    SELECT 'webhook',
                           EXTRACT(EPOCH FROM (NOW() - created_at))::BIGINT
                    FROM webhook_events
                    WHERE status IN ('pending', 'dispatching', 'dispatched')
                    ORDER BY created_at, id
                    LIMIT 10001
                    )
                    UNION ALL
                    (
                    SELECT 'notification',
                           EXTRACT(EPOCH FROM (NOW() - created_at))::BIGINT
                    FROM notification_events
                    WHERE status IN ('pending', 'publishing')
                    ORDER BY created_at, id
                    LIMIT 10001
                    )
                    UNION ALL
                    (
                    SELECT 'realtime',
                           EXTRACT(EPOCH FROM (NOW() - created_at))::BIGINT
                    FROM realtime_events
                    WHERE status IN ('pending', 'publishing')
                    ORDER BY created_at, id
                    LIMIT 10001
                    )
                ) AS queues
                """
            )
            queue_snapshot = cursor.fetchone()[0]
    return {
        "queues": queue_snapshot,
        "temporal_namespace": TEMPORAL_NAMESPACE,
        "task_queue": TASK_QUEUE,
    }


def record_worker_heartbeat(status: str, metadata: dict) -> None:
    write_worker_heartbeat(
        DATABASE_URL,
        WORKER_IDENTITY,
        status,
        metadata,
        connect_timeout_seconds=DATABASE_CONNECT_TIMEOUT_SECONDS,
    )


async def heartbeat_loop(stop_event: asyncio.Event, initial_metadata: dict) -> None:
    metadata = initial_metadata
    while not stop_event.is_set():
        try:
            metadata = await asyncio.to_thread(workflow_queue_metadata)
            await asyncio.to_thread(record_worker_heartbeat, "running", metadata)
        except Exception as error:
            logger.warning("Workflow worker heartbeat failed: %s", error)
        try:
            await asyncio.wait_for(
                stop_event.wait(),
                timeout=WORKER_HEARTBEAT_INTERVAL_SECONDS,
            )
        except TimeoutError:
            pass


async def main() -> None:
    client = await connect_temporal()
    automation_dispatcher = AutomationDispatcher(client)
    automation_event_publisher = AutomationEventOutboxDispatcher()
    embedding_job_dispatcher = EmbeddingJobDispatcher()
    webhook_dispatcher = WebhookEventDispatcher(client)
    notification_dispatcher = NotificationEventDispatcher()
    realtime_dispatcher = RealtimeEventDispatcher()
    dispatchers = (
        automation_dispatcher,
        automation_event_publisher,
        embedding_job_dispatcher,
        webhook_dispatcher,
        notification_dispatcher,
        realtime_dispatcher,
    )
    redis_clients = tuple(
        dispatcher.redis for dispatcher in dispatchers if hasattr(dispatcher, "redis")
    )
    await asyncio.wait_for(
        asyncio.gather(*(redis_client.ping() for redis_client in redis_clients)),
        timeout=DEPENDENCY_CONNECT_TIMEOUT_SECONDS,
    )
    metadata = await asyncio.to_thread(workflow_queue_metadata)
    await asyncio.to_thread(record_worker_heartbeat, "starting", metadata)
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    registered_signals = []
    for handled_signal in (signal.SIGINT, signal.SIGTERM):
        with suppress(NotImplementedError):
            loop.add_signal_handler(handled_signal, stop_event.set)
            registered_signals.append(handled_signal)
    logger.info(
        "Starting Temporal worker (namespace=%s, task_queue=%s)",
        TEMPORAL_NAMESPACE,
        TASK_QUEUE,
    )
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as activity_executor:
        async with Worker(
            client,
            task_queue=TASK_QUEUE,
            workflows=[ReminderWorkflow, AutomationWorkflow, WebhookEventWorkflow],
            activities=[
                deliver_webhook_event,
                deliver_reminder,
                execute_automation,
                list_webhook_targets,
                record_reminder_failure,
                record_automation_failure,
                record_webhook_event_result,
            ],
            activity_executor=activity_executor,
            max_concurrent_activities=10,
        ):
            dispatcher_tasks = tuple(
                asyncio.create_task(
                    dispatcher.run(),
                    name=f"dispatcher-{type(dispatcher).__name__}",
                )
                for dispatcher in dispatchers
            )
            heartbeat_task = asyncio.create_task(
                heartbeat_loop(stop_event, metadata),
                name="workflow-worker-heartbeat",
            )
            stop_task = asyncio.create_task(stop_event.wait(), name="workflow-worker-stop")
            try:
                done, _ = await asyncio.wait(
                    (*dispatcher_tasks, heartbeat_task, stop_task),
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if stop_task not in done:
                    for completed_task in done:
                        completed_task.result()
                    raise RuntimeError("A workflow worker task exited unexpectedly")
                logger.info("Shutdown requested; draining the workflow worker")
                await asyncio.to_thread(record_worker_heartbeat, "draining", metadata)
            finally:
                stop_event.set()
                for task in (*dispatcher_tasks, heartbeat_task, stop_task):
                    task.cancel()
                await asyncio.gather(
                    *dispatcher_tasks,
                    heartbeat_task,
                    stop_task,
                    return_exceptions=True,
                )
                for redis_client in redis_clients:
                    await redis_client.aclose()
    try:
        metadata = await asyncio.to_thread(workflow_queue_metadata)
        await asyncio.to_thread(record_worker_heartbeat, "stopped", metadata)
    except Exception as error:
        logger.warning("Could not record stopped workflow heartbeat: %s", error)
    for handled_signal in registered_signals:
        loop.remove_signal_handler(handled_signal)
    logger.info("Workflow worker stopped")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
