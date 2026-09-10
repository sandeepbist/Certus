import asyncio
import logging
import os
import re
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Literal, Optional

import psycopg2
from neo4j import GraphDatabase, Query
from psycopg2.extras import RealDictCursor
from temporalio.client import Client
from temporalio.common import WorkflowIDConflictPolicy, WorkflowIDReusePolicy


logger = logging.getLogger("certus_tool_service")

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://nexus:nexus_dev_password@localhost:5432/nexus",
)
NEO4J_URI = os.getenv("NEO4J_URI", "bolt://localhost:7687")
NEO4J_USER = os.getenv("NEO4J_USER", "neo4j")
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD", "nexus_neo4j_dev")
TEMPORAL_ADDRESS = os.getenv("TEMPORAL_ADDRESS", "localhost:7233")
TEMPORAL_NAMESPACE = os.getenv("TEMPORAL_NAMESPACE", "nexus")
TEMPORAL_TASK_QUEUE = os.getenv("TEMPORAL_TASK_QUEUE", "certus-workflows")


def _bounded_environment_integer(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError as error:
        raise ValueError(f"{name} must be an integer") from error
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


DB_CONNECT_TIMEOUT_SECONDS = _bounded_environment_integer(
    "MCP_DB_CONNECT_TIMEOUT_SECONDS", 3, 1, 30
)
DB_STATEMENT_TIMEOUT_MS = _bounded_environment_integer(
    "MCP_DB_STATEMENT_TIMEOUT_MS", 10_000, 100, 120_000
)
DB_LOCK_TIMEOUT_MS = _bounded_environment_integer(
    "MCP_DB_LOCK_TIMEOUT_MS", 3_000, 100, 30_000
)
NEO4J_CONNECT_TIMEOUT_SECONDS = _bounded_environment_integer(
    "MCP_NEO4J_CONNECT_TIMEOUT_SECONDS", 3, 1, 30
)
NEO4J_ACQUISITION_TIMEOUT_SECONDS = _bounded_environment_integer(
    "MCP_NEO4J_ACQUISITION_TIMEOUT_SECONDS", 5, 1, 60
)
NEO4J_QUERY_TIMEOUT_SECONDS = _bounded_environment_integer(
    "MCP_NEO4J_QUERY_TIMEOUT_SECONDS", 10, 1, 120
)
NEO4J_POOL_SIZE = _bounded_environment_integer("MCP_NEO4J_POOL_SIZE", 20, 1, 100)
TEMPORAL_CONNECT_TIMEOUT_SECONDS = _bounded_environment_integer(
    "MCP_TEMPORAL_CONNECT_TIMEOUT_SECONDS", 5, 1, 30
)
TEMPORAL_RPC_TIMEOUT_SECONDS = _bounded_environment_integer(
    "MCP_TEMPORAL_RPC_TIMEOUT_SECONDS", 10, 1, 60
)

_graph_driver = None
_graph_driver_lock = threading.Lock()


def graph_driver():
    """Return the process-wide thread-safe Neo4j connection pool."""

    global _graph_driver
    if _graph_driver is None:
        with _graph_driver_lock:
            if _graph_driver is None:
                _graph_driver = GraphDatabase.driver(
                    NEO4J_URI,
                    auth=(NEO4J_USER, NEO4J_PASSWORD),
                    connection_timeout=NEO4J_CONNECT_TIMEOUT_SECONDS,
                    connection_acquisition_timeout=NEO4J_ACQUISITION_TIMEOUT_SECONDS,
                    max_connection_pool_size=NEO4J_POOL_SIZE,
                    max_transaction_retry_time=NEO4J_QUERY_TIMEOUT_SECONDS,
                )
    return _graph_driver


def close_graph_driver() -> None:
    global _graph_driver
    with _graph_driver_lock:
        driver = _graph_driver
        _graph_driver = None
    if driver is not None:
        driver.close()


class TemporalClientProvider:
    """Lazily reuse one SDK client while keeping optional reminders degradable."""

    def __init__(self) -> None:
        self._client: Optional[Client] = None
        self._lock = asyncio.Lock()
        self.status = "unseen"

    async def get(self) -> Client:
        if self._client is not None:
            return self._client
        async with self._lock:
            if self._client is None:
                try:
                    self._client = await asyncio.wait_for(
                        Client.connect(TEMPORAL_ADDRESS, namespace=TEMPORAL_NAMESPACE),
                        timeout=TEMPORAL_CONNECT_TIMEOUT_SECONDS,
                    )
                except Exception:
                    self.status = "not_ready"
                    raise
                self.status = "ready"
        return self._client

    def record_start(self, succeeded: bool) -> None:
        self.status = "ready" if succeeded else "not_ready"


temporal_clients = TemporalClientProvider()

SummaryStyle = Literal["brief", "detailed", "bullet_points"]


class ToolExecutionError(RuntimeError):
    """Safe, caller-readable tool execution error."""


class ToolInputError(ToolExecutionError):
    """The caller can correct the request and retry."""


class ToolNotFoundError(ToolExecutionError):
    """A tenant-scoped application record was not found."""


class ToolUnavailableError(ToolExecutionError):
    """A required backing service is temporarily unavailable."""


@dataclass(frozen=True)
class RequestIdentity:
    user_id: str
    tenant_id: str

    @classmethod
    def from_values(cls, user_id: str, tenant_id: str) -> "RequestIdentity":
        clean_user_id = user_id.strip()
        clean_tenant_id = tenant_id.strip()
        if not clean_user_id or not clean_tenant_id:
            raise ToolInputError("A trusted user and tenant identity is required")
        if len(clean_user_id) > 255 or len(clean_tenant_id) > 255:
            raise ToolInputError("The trusted request identity is invalid")
        return cls(user_id=clean_user_id, tenant_id=clean_tenant_id)


def database_connection():
    return psycopg2.connect(
        DATABASE_URL,
        connect_timeout=DB_CONNECT_TIMEOUT_SECONDS,
        application_name="certus-mcp-tools",
        options=(
            f"-c statement_timeout={DB_STATEMENT_TIMEOUT_MS} "
            f"-c lock_timeout={DB_LOCK_TIMEOUT_MS}"
        ),
    )


def database_is_ready() -> bool:
    try:
        with database_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute("SET LOCAL statement_timeout = '2000ms'")
                cursor.execute("SELECT 1")
                return cursor.fetchone() == (1,)
    except psycopg2.Error:
        return False


def normalize_tags(tags: Optional[List[str]]) -> List[str]:
    normalized: List[str] = []
    seen: set[str] = set()
    for tag in tags or []:
        clean_tag = tag.strip().casefold()
        if clean_tag and clean_tag not in seen:
            normalized.append(clean_tag)
            seen.add(clean_tag)
    if len(normalized) > 20 or any(len(tag) > 64 for tag in normalized):
        raise ToolInputError("Tasks support at most 20 tags of 64 characters each")
    return normalized


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def search_notes(
    query: str,
    identity: RequestIdentity,
    top_k: int = 5,
) -> Dict[str, Any]:
    clean_query = query.strip()
    if not clean_query:
        raise ToolInputError("search_notes requires a non-empty query")
    result_limit = max(1, min(int(top_k), 20))

    try:
        with database_connection() as connection:
            with connection.cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute(
                    """
                    SELECT c.id AS chunk_id, c.document_id,
                           c.document_version_id, version.version_number,
                           version.title AS document_title,
                           version.content_hash, version.source_time,
                           version.recorded_at,
                           (d.current_version_id = version.id) AS is_current_version,
                           c.content, c.page_number, c.section_title,
                           ts_rank_cd(
                               c.search_vector,
                               websearch_to_tsquery('english', %s)
                           ) AS relevance
                    FROM chunks c
                    JOIN documents d ON d.id = c.document_id
                    JOIN document_versions AS version
                      ON version.id = c.document_version_id
                     AND version.document_id = c.document_id
                    WHERE c.tenant_id = %s AND c.user_id = %s
                      AND d.tenant_id = %s AND d.user_id = %s
                      AND d.deleted_at IS NULL
                      AND version.tenant_id = %s AND version.user_id = %s
                      AND version.status = 'ready'
                      AND c.derivation_id = version.current_derivation_id
                      AND (
                          c.search_vector @@ websearch_to_tsquery('english', %s)
                          OR c.content ILIKE %s ESCAPE '\\'
                      )
                    ORDER BY relevance DESC, c.chunk_index ASC
                    LIMIT %s
                    """,
                    (
                        clean_query,
                        identity.tenant_id,
                        identity.user_id,
                        identity.tenant_id,
                        identity.user_id,
                        identity.tenant_id,
                        identity.user_id,
                        clean_query,
                        f"%{_escape_like(clean_query)}%",
                        result_limit,
                    ),
                )
                rows = cursor.fetchall()
    except psycopg2.Error as error:
        logger.exception("Note search failed")
        raise ToolUnavailableError("Workspace search is temporarily unavailable") from error

    results = [
        {
            "chunk_id": str(row["chunk_id"]),
            "document_id": str(row["document_id"]),
            "document_version_id": str(row["document_version_id"]),
            "version_number": int(row["version_number"]),
            "document_title": row["document_title"],
            "content_hash": row["content_hash"],
            "source_time": (
                row["source_time"].isoformat() if row.get("source_time") else None
            ),
            "recorded_at": row["recorded_at"].isoformat(),
            "is_current_version": bool(row["is_current_version"]),
            "content": row["content"][:1_500],
            "section_title": row.get("section_title") or "",
            "page_number": row.get("page_number"),
            "relevance": round(float(row.get("relevance") or 0), 4),
        }
        for row in rows
    ]
    return {
        "tool": "search_notes",
        "query": clean_query,
        "retrieval_mode": "postgres_full_text",
        "results_count": len(results),
        "results": results,
    }


def _sync_task_to_graph(task: Dict[str, Any], identity: RequestIdentity) -> str:
    try:
        with graph_driver().session() as session:
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
                    WITH task
                    UNWIND $tags AS tag
                    MERGE (entity:Entity {tenant_id: $tenant_id, normalized_name: tag})
                    ON CREATE SET entity.name = tag, entity.type = 'TAG', entity.mention_count = 0
                    MERGE (task)-[:RELATES_TO]->(entity)
                    """,
                    timeout=NEO4J_QUERY_TIMEOUT_SECONDS,
                ),
                tenant_id=identity.tenant_id,
                user_id=identity.user_id,
                task_id=str(task["id"]),
                title=task["title"],
                priority=task["priority"],
                tags=task["tags"],
            ).consume()
        return "synced"
    except Exception as error:
        logger.warning("Task graph synchronization degraded: %s", error)
        return "degraded"


def create_task(
    title: str,
    identity: RequestIdentity,
    description: str = "",
    priority: str = "medium",
    tags: Optional[List[str]] = None,
) -> Dict[str, Any]:
    clean_title = title.strip()
    clean_description = description.strip()
    if not clean_title:
        raise ToolInputError("create_task requires a non-empty title")
    if len(clean_title) > 500:
        raise ToolInputError("Task titles must be 500 characters or fewer")
    if len(clean_description) > 5_000:
        raise ToolInputError("Task descriptions must be 5,000 characters or fewer")
    if priority not in {"low", "medium", "high", "urgent"}:
        raise ToolInputError("Task priority must be low, medium, high, or urgent")

    tag_list = normalize_tags(tags)
    task_id = str(uuid.uuid4())
    try:
        with database_connection() as connection:
            with connection.cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute(
                    """
                    INSERT INTO tasks (
                        id, user_id, tenant_id, title, description, status,
                        priority, tags, version, created_at, updated_at
                    ) VALUES (%s, %s, %s, %s, %s, 'pending', %s, %s, 1, NOW(), NOW())
                    RETURNING id, title, description, status, priority, tags, version,
                              created_at, updated_at
                    """,
                    (
                        task_id,
                        identity.user_id,
                        identity.tenant_id,
                        clean_title,
                        clean_description,
                        priority,
                        tag_list,
                    ),
                )
                task = dict(cursor.fetchone())
                notification_id = str(
                    uuid.uuid5(uuid.NAMESPACE_URL, f"certus:agent-task:{task_id}")
                )
                cursor.execute(
                    """
                    INSERT INTO notifications (
                        id, user_id, organization_id, type, title, body,
                        metadata, action_url, is_read, created_at
                    ) VALUES (
                        %s, %s, %s, 'task_created_by_agent',
                        'Task created by agent', %s,
                        jsonb_build_object('task_id', %s::text),
                        '/tasks', false, NOW()
                    )
                    ON CONFLICT (id) DO NOTHING
                    """,
                    (
                        notification_id,
                        identity.user_id,
                        identity.tenant_id,
                        clean_title,
                        task_id,
                    ),
                )
    except psycopg2.Error as error:
        logger.exception("Task creation failed")
        raise ToolUnavailableError("Task creation is temporarily unavailable") from error

    graph_sync = _sync_task_to_graph(task, identity)
    return {
        "tool": "create_task",
        "task_id": str(task["id"]),
        "title": task["title"],
        "description": task["description"],
        "priority": task["priority"],
        "tags": task["tags"],
        "status": "created",
        "version": task["version"],
        "graph_sync": graph_sync,
    }


_SENTENCE_BOUNDARY = re.compile(r"(?<=[.!?])\s+")


def extractive_summary(text: str, style: SummaryStyle) -> tuple[str, List[str]]:
    clean_text = " ".join(text.split())
    if not clean_text:
        return "This document does not contain extractable text.", []

    sentence_limit = {"brief": 3, "bullet_points": 6, "detailed": 10}[style]
    character_limit = {"brief": 700, "bullet_points": 1_400, "detailed": 2_400}[style]
    candidates = [sentence.strip() for sentence in _SENTENCE_BOUNDARY.split(clean_text) if sentence.strip()]
    if len(candidates) == 1:
        candidates = [
            clean_text[index:index + 320].strip()
            for index in range(0, min(len(clean_text), character_limit), 320)
        ]

    selected: List[str] = []
    used = 0
    for sentence in candidates:
        bounded = sentence[:500].rstrip()
        if not bounded:
            continue
        projected = used + len(bounded)
        if selected and projected > character_limit:
            break
        selected.append(bounded)
        used = projected
        if len(selected) >= sentence_limit:
            break

    if style == "bullet_points":
        return "\n".join(f"• {sentence}" for sentence in selected), selected
    return " ".join(selected), selected


def summarize_document(
    document_id: str,
    identity: RequestIdentity,
    style: SummaryStyle = "bullet_points",
) -> Dict[str, Any]:
    try:
        with database_connection() as connection:
            with connection.cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute(
                    """
                    SELECT document.id, version.id AS document_version_id,
                           version.version_number, version.title,
                           parsed.content_text AS parsed_text,
                           version.recorded_at, version.source_time,
                           version.current_derivation_id
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
                    (document_id, identity.tenant_id, identity.user_id),
                )
                document = cursor.fetchone()
                if not document:
                    raise ToolNotFoundError("Document not found")
                cursor.execute(
                    """
                    SELECT id, content
                    FROM chunks
                    WHERE document_id = %s
                      AND document_version_id = %s
                      AND derivation_id = %s
                      AND tenant_id = %s AND user_id = %s
                    ORDER BY chunk_index ASC
                    LIMIT 40
                    """,
                    (
                        document_id,
                        str(document["document_version_id"]),
                        str(document["current_derivation_id"]),
                        identity.tenant_id,
                        identity.user_id,
                    ),
                )
                chunks = cursor.fetchall()
    except ToolExecutionError:
        raise
    except psycopg2.Error as error:
        logger.exception("Document summary lookup failed")
        raise ToolUnavailableError("Document summarization is temporarily unavailable") from error

    source_text = " ".join(chunk["content"] for chunk in chunks) or (document["parsed_text"] or "")
    summary, key_points = extractive_summary(source_text, style)
    return {
        "tool": "summarize_document",
        "document_id": str(document["id"]),
        "document_version_id": str(document["document_version_id"]),
        "version_number": int(document["version_number"]),
        "document_title": document["title"],
        "source_time": (
            document["source_time"].isoformat() if document.get("source_time") else None
        ),
        "recorded_at": document["recorded_at"].isoformat(),
        "style": style,
        "summary_mode": "extractive",
        "summary": summary,
        "key_points": key_points,
        "source_chunk_ids": [str(chunk["id"]) for chunk in chunks],
        "total_chunks_analyzed": len(chunks),
    }


def parse_reminder_time(trigger_time: str, now: Optional[datetime] = None) -> datetime:
    clean_trigger = trigger_time.strip()
    relative = re.fullmatch(
        r"in\s+(\d+)\s*(second|seconds|minute|minutes|hour|hours|day|days)",
        clean_trigger,
        re.IGNORECASE,
    )
    current_time = now or datetime.now(timezone.utc)
    if current_time.tzinfo is None:
        current_time = current_time.replace(tzinfo=timezone.utc)
    current_time = current_time.astimezone(timezone.utc)

    if relative:
        quantity = int(relative.group(1))
        unit = relative.group(2).casefold().rstrip("s")
        multipliers = {"second": 1, "minute": 60, "hour": 3_600, "day": 86_400}
        scheduled_for = current_time + timedelta(seconds=quantity * multipliers[unit])
    else:
        try:
            scheduled_for = datetime.fromisoformat(clean_trigger.replace("Z", "+00:00"))
        except ValueError as error:
            raise ToolInputError(
                "trigger_time must be an ISO 8601 timestamp with timezone or a relative value such as 'in 15 minutes'"
            ) from error
        if scheduled_for.tzinfo is None:
            raise ToolInputError("ISO trigger_time must include a timezone")
        scheduled_for = scheduled_for.astimezone(timezone.utc)

    if scheduled_for < current_time - timedelta(seconds=1):
        raise ToolInputError("trigger_time cannot be in the past")
    if scheduled_for > current_time + timedelta(days=366):
        raise ToolInputError("Reminders can be scheduled at most 366 days ahead")
    return scheduled_for


async def schedule_reminder(
    message: str,
    identity: RequestIdentity,
    trigger_time: str = "in 1 hour",
) -> Dict[str, Any]:
    clean_message = message.strip()
    if not clean_message:
        raise ToolInputError("schedule_reminder requires a non-empty message")
    if len(clean_message) > 2_000:
        raise ToolInputError("Reminder messages must be 2,000 characters or fewer")

    scheduled_for = parse_reminder_time(trigger_time)
    reminder_id = str(uuid.uuid4())
    notification_id = str(uuid.uuid4())
    workflow_id = f"certus-reminder-{reminder_id}"
    try:
        with database_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO reminders (
                        id, user_id, tenant_id, workflow_id, notification_id,
                        message, scheduled_for, status, created_at, updated_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, 'starting', NOW(), NOW())
                    """,
                    (
                        reminder_id,
                        identity.user_id,
                        identity.tenant_id,
                        workflow_id,
                        notification_id,
                        clean_message,
                        scheduled_for,
                    ),
                )
    except psycopg2.Error as error:
        logger.exception("Reminder persistence failed")
        raise ToolUnavailableError("Reminder scheduling is temporarily unavailable") from error

    try:
        client = await temporal_clients.get()
        handle = await client.start_workflow(
            "certus.reminder.v1",
            {
                "reminder_id": reminder_id,
                "notification_id": notification_id,
                "workflow_id": workflow_id,
                "tenant_id": identity.tenant_id,
                "user_id": identity.user_id,
                "message": clean_message,
                "scheduled_for": scheduled_for.isoformat(),
            },
            id=workflow_id,
            task_queue=TEMPORAL_TASK_QUEUE,
            id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
            id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
            rpc_timeout=timedelta(seconds=TEMPORAL_RPC_TIMEOUT_SECONDS),
        )
        temporal_clients.record_start(True)
    except Exception as error:
        temporal_clients.record_start(False)
        logger.exception("Temporal reminder start failed")
        try:
            with database_connection() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        UPDATE reminders
                        SET status = 'failed', error_message = %s, updated_at = NOW()
                        WHERE id = %s AND tenant_id = %s AND user_id = %s
                        """,
                        (type(error).__name__, reminder_id, identity.tenant_id, identity.user_id),
                    )
        except psycopg2.Error:
            logger.exception("Could not mirror failed reminder start")
        raise ToolUnavailableError("The durable workflow engine could not accept the reminder") from error

    try:
        with database_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE reminders
                    SET status = 'scheduled', workflow_run_id = %s, updated_at = NOW()
                    WHERE id = %s AND tenant_id = %s AND user_id = %s
                    """,
                    (
                        handle.first_execution_run_id,
                        reminder_id,
                        identity.tenant_id,
                        identity.user_id,
                    ),
                )
    except psycopg2.Error as error:
        logger.exception("Could not mirror accepted Temporal reminder")
        raise ToolUnavailableError(
            "The reminder was accepted, but its local status could not be updated"
        ) from error

    return {
        "tool": "schedule_reminder",
        "reminder_id": reminder_id,
        "workflow_id": workflow_id,
        "workflow_run_id": handle.first_execution_run_id,
        "message": clean_message,
        "scheduled_for": scheduled_for.isoformat(),
        "status": "scheduled",
        "engine": "temporal",
    }


def graph_query(
    entity_name: str,
    identity: RequestIdentity,
    depth: int = 2,
) -> Dict[str, Any]:
    clean_entity_name = entity_name.strip()
    if not clean_entity_name:
        raise ToolInputError("graph_query requires a non-empty entity_name")
    traversal_depth = max(1, min(int(depth), 2))

    try:
        triples: List[str] = []
        connected: set[str] = set()
        seen_edges: set[tuple[str, str]] = set()
        with graph_driver().session() as session:
            direct_results = session.run(
                Query(
                    """
                    MATCH (user:User {id: $user_id})-[:OWNS]->(document:Document {tenant_id: $tenant_id})
                          -[:MENTIONS]->(root:Entity {tenant_id: $tenant_id, normalized_name: $normalized_name})
                    WHERE document.deleted_at IS NULL
                    MATCH (document)-[:MENTIONS]->(target:Entity {tenant_id: $tenant_id})
                    WHERE target <> root
                    RETURN root.name AS root, target.name AS target
                    LIMIT 40
                    """,
                    timeout=NEO4J_QUERY_TIMEOUT_SECONDS,
                ),
                normalized_name=clean_entity_name.casefold(),
                user_id=identity.user_id,
                tenant_id=identity.tenant_id,
            )
            middle_entities: set[str] = set()
            for record in direct_results:
                root = record["root"]
                target = record["target"]
                edge = tuple(sorted((root.casefold(), target.casefold())))
                if edge in seen_edges:
                    continue
                seen_edges.add(edge)
                triples.append(f"({root}) -[CO_MENTIONED]- ({target})")
                connected.add(target)
                middle_entities.add(target.casefold())

            if traversal_depth == 2 and middle_entities:
                second_results = session.run(
                    Query(
                        """
                        MATCH (user:User {id: $user_id})-[:OWNS]->(document:Document {tenant_id: $tenant_id})
                              -[:MENTIONS]->(root:Entity {tenant_id: $tenant_id})
                        WHERE document.deleted_at IS NULL
                          AND root.normalized_name IN $middle_entities
                        MATCH (document)-[:MENTIONS]->(target:Entity {tenant_id: $tenant_id})
                        WHERE target <> root
                        RETURN root.name AS root, target.name AS target
                        LIMIT 40
                        """,
                        timeout=NEO4J_QUERY_TIMEOUT_SECONDS,
                    ),
                    middle_entities=sorted(middle_entities),
                    user_id=identity.user_id,
                    tenant_id=identity.tenant_id,
                )
                for record in second_results:
                    root = record["root"]
                    target = record["target"]
                    if target.casefold() == clean_entity_name.casefold():
                        continue
                    edge = tuple(sorted((root.casefold(), target.casefold())))
                    if edge in seen_edges:
                        continue
                    seen_edges.add(edge)
                    triples.append(f"({root}) -[CO_MENTIONED]- ({target})")
                    connected.add(target)
    except Exception as error:
        logger.exception("Knowledge graph query failed")
        raise ToolUnavailableError("Knowledge graph traversal is temporarily unavailable") from error

    return {
        "tool": "graph_query",
        "entity": clean_entity_name,
        "depth": traversal_depth,
        "connected_entities": sorted(connected),
        "relation_types": ["CO_MENTIONED"] if triples else [],
        "graph_triples": triples,
    }
