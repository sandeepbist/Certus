import json
import logging
import os
import uuid
import sys
from pathlib import Path
from typing import Any, Dict

from psycopg2.extras import RealDictCursor
from dotenv import load_dotenv
from temporalio import activity
from temporalio.exceptions import ApplicationError


MODULE_PATH = Path(__file__).resolve()
REPO_ROOT = next(
    (parent for parent in MODULE_PATH.parents if (parent / "services" / "shared").exists()),
    MODULE_PATH.parents[1],
)
sys.path.insert(0, str(REPO_ROOT))

from services.shared.automation import condition_matches, extract_summary
from services.shared.worker_runtime import bounded_int_env, connect_database
from services.shared.webhooks import (
    UnsafeWebhookTarget,
    WebhookConfigurationError,
    canonical_payload,
    decrypt_signing_secret,
    send_signed_webhook,
)


load_dotenv(REPO_ROOT / ".env")

logger = logging.getLogger("certus_workflow_activities")
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://nexus:nexus_dev_password@localhost:5432/nexus")
NEO4J_URI = os.getenv("NEO4J_URI", "bolt://localhost:7687")
NEO4J_USER = os.getenv("NEO4J_USER", "neo4j")
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD", "nexus_neo4j_dev")
NODE_ENV = os.getenv("NODE_ENV", "development")
INTERNAL_SERVICE_TOKEN = os.getenv("INTERNAL_SERVICE_TOKEN", "")
WEBHOOK_ENCRYPTION_KEY = os.getenv("WEBHOOK_ENCRYPTION_KEY", "")
WEBHOOK_ALLOW_PRIVATE_TARGETS = os.getenv(
    "WEBHOOK_ALLOW_PRIVATE_TARGETS", "false"
).strip().casefold() in {"1", "true", "yes"}
DATABASE_CONNECT_TIMEOUT_SECONDS = bounded_int_env(
    "WORKFLOWS_DB_CONNECT_TIMEOUT_SECONDS", 3, 1, 30
)
NEO4J_TIMEOUT_SECONDS = bounded_int_env("WORKFLOWS_NEO4J_TIMEOUT_SECONDS", 5, 2, 30)
WORKER_DB_STATEMENT_TIMEOUT_MS = bounded_int_env(
    "WORKER_DB_STATEMENT_TIMEOUT_MS", 15_000, 1_000, 120_000
)
WORKER_DB_LOCK_TIMEOUT_MS = bounded_int_env(
    "WORKER_DB_LOCK_TIMEOUT_MS", 3_000, 500, 30_000
)


def database_connection():
    return connect_database(
        DATABASE_URL,
        application_name="certus-workflow-activity",
        connect_timeout_seconds=DATABASE_CONNECT_TIMEOUT_SECONDS,
        statement_timeout_ms=WORKER_DB_STATEMENT_TIMEOUT_MS,
        lock_timeout_ms=WORKER_DB_LOCK_TIMEOUT_MS,
    )


@activity.defn(name="deliver_reminder")
def deliver_reminder(payload: Dict[str, Any]) -> Dict[str, Any]:
    reminder_id = payload["reminder_id"]
    notification_id = payload["notification_id"]
    tenant_id = payload["tenant_id"]
    user_id = payload["user_id"]

    with database_connection() as connection:
        with connection.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute(
                """
                SELECT status FROM reminders
                WHERE id = %s AND tenant_id = %s AND user_id = %s
                FOR UPDATE
                """,
                (reminder_id, tenant_id, user_id),
            )
            reminder = cursor.fetchone()
            if not reminder:
                raise RuntimeError("Reminder record was not found")
            if reminder["status"] == "delivered":
                return {"status": "delivered", "reminder_id": reminder_id, "idempotent": True}

            cursor.execute(
                """
                INSERT INTO notifications (
                    id, user_id, organization_id, type, title, body,
                    metadata, action_url, is_read, created_at
                ) VALUES (
                    %s, %s, %s, 'reminder_due', 'Reminder', %s,
                    %s::jsonb, '/tasks', false, NOW()
                )
                ON CONFLICT (id) DO NOTHING
                """,
                (
                    notification_id,
                    user_id,
                    tenant_id,
                    payload["message"],
                    json.dumps({"reminder_id": reminder_id, "workflow_id": payload["workflow_id"]}),
                ),
            )
            cursor.execute(
                """
                UPDATE reminders
                SET status = 'delivered', delivered_at = NOW(), updated_at = NOW(), error_message = NULL
                WHERE id = %s AND tenant_id = %s AND user_id = %s
                """,
                (reminder_id, tenant_id, user_id),
            )

    return {"status": "delivered", "reminder_id": reminder_id, "notification_id": notification_id}


@activity.defn(name="record_reminder_failure")
def record_reminder_failure(payload: Dict[str, Any]) -> Dict[str, Any]:
    with database_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE reminders
                SET status = 'failed', error_message = %s, updated_at = NOW()
                WHERE id = %s AND tenant_id = %s AND user_id = %s
                """,
                (
                    f"Temporal activity failed after retries ({payload.get('error_type', 'ActivityError')})",
                    payload["reminder_id"],
                    payload["tenant_id"],
                    payload["user_id"],
                ),
            )
    return {"status": "failed", "reminder_id": payload["reminder_id"]}


def sync_automation_task_to_graph(task: Dict[str, Any], tenant_id: str, user_id: str) -> None:
    try:
        from neo4j import GraphDatabase, Query

        with GraphDatabase.driver(
            NEO4J_URI,
            auth=(NEO4J_USER, NEO4J_PASSWORD),
            connection_timeout=NEO4J_TIMEOUT_SECONDS - 1,
            connection_acquisition_timeout=NEO4J_TIMEOUT_SECONDS,
            max_transaction_retry_time=NEO4J_TIMEOUT_SECONDS,
        ) as driver:
            with driver.session() as session:
                session.run(
                    Query(
                        """
                    MERGE (tenant:Tenant {id: $tenant_id})
                    MERGE (user:User {id: $user_id})
                    MERGE (task:Task {id: $task_id})
                    SET task.tenant_id = $tenant_id,
                        task.title = $title,
                        task.status = 'pending',
                        task.priority = $priority,
                        task.updated_at = datetime()
                    MERGE (user)-[:MEMBER_OF]->(tenant)
                    MERGE (tenant)-[:CONTAINS]->(task)
                    MERGE (user)-[:OWNS]->(task)
                        """,
                        timeout=NEO4J_TIMEOUT_SECONDS,
                    ),
                    tenant_id=tenant_id,
                    user_id=user_id,
                    task_id=str(task["id"]),
                    title=task["title"],
                    priority=task["priority"],
                )
    except Exception as error:
        logger.warning("Automation task graph sync degraded: %s", error)


@activity.defn(name="execute_automation")
def execute_automation(payload: Dict[str, Any]) -> Dict[str, Any]:
    execution_id = payload["execution_id"]
    rule_id = payload["rule_id"]
    tenant_id = payload["tenant_id"]
    user_id = payload["user_id"]
    event = payload["event"]
    workflow_id = payload["workflow_id"]
    created_task: Dict[str, Any] | None = None

    with database_connection() as connection:
        with connection.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute(
                """
                SELECT id, name, trigger_event, trigger_conditions, actions, is_enabled
                FROM automation_rules
                WHERE id = %s AND tenant_id = %s AND user_id = %s
                """,
                (rule_id, tenant_id, user_id),
            )
            rule = cursor.fetchone()
            if not rule or not rule["is_enabled"]:
                return {"status": "skipped", "reason": "rule_disabled_or_deleted"}

            cursor.execute(
                """
                INSERT INTO automation_executions (
                    id, rule_id, user_id, tenant_id, trigger_event, trigger_event_id,
                    workflow_id, workflow_run_id, source_document_id, status, started_at, updated_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'running', NOW(), NOW())
                ON CONFLICT (id) DO UPDATE SET updated_at = NOW()
                RETURNING status, result
                """,
                (
                    execution_id,
                    rule_id,
                    user_id,
                    tenant_id,
                    event["type"],
                    event["event_id"],
                    workflow_id,
                    activity.info().workflow_run_id,
                    event.get("document_id"),
                ),
            )
            existing_execution = cursor.fetchone()
            if existing_execution["status"] in {"completed", "skipped"}:
                return dict(existing_execution["result"] or {"status": existing_execution["status"]})

            if not condition_matches(dict(rule["trigger_conditions"] or {}), event):
                result = {"status": "skipped", "reason": "condition_not_matched"}
                cursor.execute(
                    """
                    UPDATE automation_executions
                    SET status = 'skipped', result = %s::jsonb, completed_at = NOW(), updated_at = NOW()
                    WHERE id = %s
                    """,
                    (json.dumps(result), execution_id),
                )
                return result

            results = []
            for action in list(rule["actions"] or []):
                action_type = action.get("type")
                if action_type == "summarize_and_create_task":
                    cursor.execute(
                        """
                        SELECT document.id, document.title,
                               parsed.content_text AS parsed_text
                        FROM documents AS document
                        JOIN document_versions AS version
                          ON version.id = document.current_version_id
                         AND version.document_id = document.id
                        JOIN document_parsed_artifacts AS parsed
                          ON parsed.document_version_id = version.id
                         AND parsed.document_id = document.id
                         AND parsed.tenant_id = document.tenant_id
                         AND parsed.user_id = document.user_id
                        WHERE document.id = %s
                          AND document.tenant_id = %s
                          AND document.user_id = %s
                          AND document.deleted_at IS NULL
                        """,
                        (event.get("document_id"), tenant_id, user_id),
                    )
                    document = cursor.fetchone()
                    if not document:
                        raise RuntimeError("Automation source document was not found")
                    task_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"certus:{execution_id}:task"))
                    title = f"Review summary: {document['title']}"
                    description = extract_summary(document["parsed_text"] or "")
                    cursor.execute(
                        """
                        INSERT INTO tasks (
                            id, user_id, tenant_id, title, description, status, priority,
                            tags, source_document_id, version, created_at, updated_at
                        ) VALUES (%s, %s, %s, %s, %s, 'pending', 'medium', %s, %s, 1, NOW(), NOW())
                        ON CONFLICT (id) DO UPDATE SET updated_at = tasks.updated_at
                        RETURNING id, title, priority
                        """,
                        (
                            task_id,
                            user_id,
                            tenant_id,
                            title,
                            description,
                            ["automation", "summary"],
                            str(document["id"]),
                        ),
                    )
                    created_task = dict(cursor.fetchone())
                    results.append({"action": action_type, "task_id": task_id})
                elif action_type == "notify":
                    results.append({"action": "notify"})
                else:
                    raise ValueError(f"Unsupported automation action: {action_type}")

            notification_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"certus:{execution_id}:notification"))
            cursor.execute(
                """
                INSERT INTO notifications (
                    id, user_id, organization_id, type, title, body,
                    metadata, action_url, is_read, created_at
                ) VALUES (
                    %s, %s, %s, 'automation_completed', %s, %s,
                    %s::jsonb, '/automations', false, NOW()
                )
                ON CONFLICT (id) DO NOTHING
                """,
                (
                    notification_id,
                    user_id,
                    tenant_id,
                    f"Automation completed: {rule['name']}",
                    f"Processed event {event['event_id']} successfully.",
                    json.dumps({"execution_id": execution_id, "rule_id": rule_id}),
                ),
            )
            result = {"status": "completed", "actions": results, "notification_id": notification_id}
            cursor.execute(
                """
                UPDATE automation_executions
                SET status = 'completed', result = %s::jsonb, created_task_id = %s,
                    completed_at = NOW(), updated_at = NOW(), error_message = NULL
                WHERE id = %s
                """,
                (json.dumps(result), str(created_task["id"]) if created_task else None, execution_id),
            )
            cursor.execute(
                """
                UPDATE automation_rules
                SET execution_count = execution_count + 1,
                    last_executed_at = NOW(), updated_at = NOW()
                WHERE id = %s AND tenant_id = %s AND user_id = %s
                """,
                (rule_id, tenant_id, user_id),
            )

    if created_task:
        sync_automation_task_to_graph(created_task, tenant_id, user_id)
    return result


@activity.defn(name="record_automation_failure")
def record_automation_failure(payload: Dict[str, Any]) -> Dict[str, Any]:
    error_message = f"Temporal activity failed after retries ({payload.get('error_type', 'ActivityError')})"
    event = payload["event"]
    with database_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO automation_executions (
                    id, rule_id, user_id, tenant_id, trigger_event, trigger_event_id,
                    workflow_id, workflow_run_id, source_document_id, status,
                    error_message, started_at, completed_at, updated_at
                ) VALUES (
                    %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, 'failed',
                    %s, NOW(), NOW(), NOW()
                )
                ON CONFLICT (id) DO UPDATE SET
                    status = 'failed',
                    error_message = EXCLUDED.error_message,
                    completed_at = NOW(),
                    updated_at = NOW()
                """,
                (
                    payload["execution_id"],
                    payload["rule_id"],
                    payload["user_id"],
                    payload["tenant_id"],
                    event["type"],
                    event["event_id"],
                    payload["workflow_id"],
                    activity.info().workflow_run_id,
                    event.get("document_id"),
                    error_message,
                ),
            )
    return {"status": "failed", "execution_id": payload["execution_id"]}


@activity.defn(name="list_webhook_targets")
def list_webhook_targets(payload: Dict[str, Any]) -> Dict[str, Any]:
    with database_connection() as connection:
        with connection.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute(
                """
                SELECT webhook.id
                FROM webhook_events AS event
                JOIN webhooks AS webhook
                  ON webhook.organization_id = event.organization_id
                 AND webhook.user_id = event.user_id
                 AND webhook.id = ANY(event.target_webhook_ids)
                WHERE event.id = %s
                  AND event.organization_id = %s
                  AND event.user_id = %s
                  AND webhook.is_enabled = true
                ORDER BY webhook.created_at, webhook.id
                """,
                (payload["event_id"], payload["tenant_id"], payload["user_id"]),
            )
            return {"webhook_ids": [str(row["id"]) for row in cursor.fetchall()]}


def _record_webhook_attempt_failure(
    *,
    attempt_id: str,
    webhook_id: str,
    error_message: str,
) -> None:
    with database_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE webhook_deliveries
                SET success = false, error_message = %s, delivered_at = NOW()
                WHERE id = %s
                """,
                (error_message, attempt_id),
            )
            cursor.execute(
                """
                UPDATE webhooks
                SET last_triggered_at = NOW(), failure_count = failure_count + 1,
                    last_error = %s, updated_at = NOW()
                WHERE id = %s
                """,
                (error_message, webhook_id),
            )


@activity.defn(name="deliver_webhook_event")
def deliver_webhook_event(payload: Dict[str, Any]) -> Dict[str, Any]:
    event_id = payload["event_id"]
    webhook_id = payload["webhook_id"]
    attempt_number = activity.info().attempt
    delivery_key = str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"certus:webhook-delivery:{webhook_id}:{event_id}",
        )
    )
    attempt_id = str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"certus:webhook-delivery-attempt:{webhook_id}:{event_id}:{attempt_number}",
        )
    )

    with database_connection() as connection:
        with connection.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute(
                """
                SELECT webhook.id, webhook.url, webhook.secret,
                       event.event_type, event.payload,
                       EXTRACT(EPOCH FROM event.created_at)::BIGINT AS created_at
                FROM webhook_events AS event
                JOIN webhooks AS webhook
                  ON webhook.id = %s
                 AND webhook.organization_id = event.organization_id
                 AND webhook.user_id = event.user_id
                 AND webhook.id = ANY(event.target_webhook_ids)
                WHERE event.id = %s
                  AND event.organization_id = %s
                  AND event.user_id = %s
                  AND webhook.is_enabled = true
                """,
                (webhook_id, event_id, payload["tenant_id"], payload["user_id"]),
            )
            target = cursor.fetchone()
            if not target:
                return {
                    "status": "skipped",
                    "webhook_id": webhook_id,
                    "reason": "webhook_disabled_or_deleted",
                }

            event_data = dict(target["payload"] or {})
            created_at = int(target["created_at"])
            body = canonical_payload(
                target["event_type"],
                event_data,
                delivery_key,
                created_at=created_at,
            )
            cursor.execute(
                """
                INSERT INTO webhook_deliveries (
                    id, webhook_id, event_id, delivery_key, event_type, payload,
                    success, attempt_number, attempted_at
                ) VALUES (%s, %s, %s, %s, %s, %s::jsonb, false, %s, NOW())
                ON CONFLICT (webhook_id, event_id, attempt_number)
                    WHERE event_id IS NOT NULL
                DO UPDATE SET attempted_at = CASE
                    WHEN webhook_deliveries.success THEN webhook_deliveries.attempted_at
                    ELSE NOW()
                END
                RETURNING success
                """,
                (
                    attempt_id,
                    webhook_id,
                    event_id,
                    delivery_key,
                    target["event_type"],
                    body.decode(),
                    attempt_number,
                ),
            )
            if cursor.fetchone()["success"]:
                return {
                    "status": "delivered",
                    "webhook_id": webhook_id,
                    "delivery_id": delivery_key,
                    "idempotent": True,
                }

    try:
        secret = decrypt_signing_secret(
            target["secret"],
            WEBHOOK_ENCRYPTION_KEY,
            INTERNAL_SERVICE_TOKEN,
            NODE_ENV,
        )
        outcome = send_signed_webhook(
            url=target["url"],
            secret=secret,
            event_type=target["event_type"],
            data=event_data,
            delivery_id=delivery_key,
            created_at=created_at,
            allow_private=WEBHOOK_ALLOW_PRIVATE_TARGETS,
        )
    except (UnsafeWebhookTarget, WebhookConfigurationError) as error:
        error_message = str(error)[:500]
        _record_webhook_attempt_failure(
            attempt_id=attempt_id,
            webhook_id=webhook_id,
            error_message=error_message,
        )
        raise ApplicationError(
            error_message,
            type="WebhookConfigurationError",
            non_retryable=True,
        ) from error

    with database_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE webhook_deliveries
                SET response_status = %s, response_body = %s, success = %s,
                    error_message = %s, duration_ms = %s, delivered_at = NOW()
                WHERE id = %s
                """,
                (
                    outcome.response_status,
                    outcome.response_body,
                    outcome.success,
                    outcome.error_message,
                    outcome.duration_ms,
                    attempt_id,
                ),
            )
            cursor.execute(
                """
                UPDATE webhooks
                SET last_triggered_at = NOW(),
                    failure_count = CASE WHEN %s THEN 0 ELSE failure_count + 1 END,
                    last_error = %s, updated_at = NOW()
                WHERE id = %s
                """,
                (outcome.success, outcome.error_message, webhook_id),
            )

    if not outcome.success:
        raise ApplicationError(
            outcome.error_message or "Webhook delivery failed",
            type="WebhookEndpointUnavailable" if outcome.retryable else "WebhookRejected",
            non_retryable=not outcome.retryable,
        )
    return {
        "status": "delivered",
        "webhook_id": webhook_id,
        "delivery_id": delivery_key,
        "response_status": outcome.response_status,
        "duration_ms": outcome.duration_ms,
    }


@activity.defn(name="record_webhook_event_result")
def record_webhook_event_result(payload: Dict[str, Any]) -> Dict[str, Any]:
    status = "failed" if payload.get("failed", 0) else "completed"
    result = {
        "status": status,
        "target_count": payload.get("target_count", 0),
        "delivered": payload.get("delivered", 0),
        "skipped": payload.get("skipped", 0),
        "failed": payload.get("failed", 0),
    }
    with database_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE webhook_events
                SET status = %s, result = %s::jsonb, last_error = %s,
                    completed_at = NOW(), updated_at = NOW(), locked_at = NULL
                WHERE id = %s AND organization_id = %s AND user_id = %s
                """,
                (
                    status,
                    json.dumps(result),
                    payload.get("last_error"),
                    payload["event_id"],
                    payload["tenant_id"],
                    payload["user_id"],
                ),
            )
    return result
