import logging
import uuid
from datetime import datetime
from typing import List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, field_validator

from app.core.db import get_db_cursor
from app.core.identity import RequestIdentity, require_request_identity
from app.retrieval.graphrag import bounded_graph_query, get_graph_driver


logger = logging.getLogger("orchestration_tasks")
router = APIRouter(prefix="", tags=["Tasks"])

TaskStatus = Literal["pending", "in_progress", "completed", "cancelled"]
TaskPriority = Literal["low", "medium", "high", "urgent"]


def normalize_tags(tags: List[str]) -> List[str]:
    normalized: List[str] = []
    seen = set()
    for tag in tags:
        clean_tag = tag.strip().casefold()
        if clean_tag and clean_tag not in seen:
            normalized.append(clean_tag)
            seen.add(clean_tag)
    if len(normalized) > 20 or any(len(tag) > 64 for tag in normalized):
        raise ValueError("Tasks support at most 20 tags of 64 characters each")
    return normalized


class TaskCreate(BaseModel):
    title: str = Field(min_length=1, max_length=500)
    description: str = Field(default="", max_length=5_000)
    priority: TaskPriority = "medium"
    due_date: Optional[datetime] = None
    tags: List[str] = Field(default_factory=list)

    @field_validator("title")
    @classmethod
    def clean_title(cls, value: str) -> str:
        clean_value = value.strip()
        if not clean_value:
            raise ValueError("Task title cannot be blank")
        return clean_value

    @field_validator("tags")
    @classmethod
    def clean_tags(cls, value: List[str]) -> List[str]:
        return normalize_tags(value)


class TaskUpdate(BaseModel):
    version: int = Field(ge=1)
    title: Optional[str] = Field(default=None, min_length=1, max_length=500)
    description: Optional[str] = Field(default=None, max_length=5_000)
    status: Optional[TaskStatus] = None
    priority: Optional[TaskPriority] = None
    due_date: Optional[datetime] = None
    tags: Optional[List[str]] = None

    @field_validator("title")
    @classmethod
    def clean_title(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        clean_value = value.strip()
        if not clean_value:
            raise ValueError("Task title cannot be blank")
        return clean_value

    @field_validator("tags")
    @classmethod
    def clean_tags(cls, value: Optional[List[str]]) -> Optional[List[str]]:
        return normalize_tags(value) if value is not None else None


TASK_FIELDS = """
    id, title, description, status, priority, due_date, tags, version,
    source_document_id, source_agent_run_id, created_at, updated_at
"""


def sync_task_graph(task: dict, identity: RequestIdentity) -> None:
    try:
        with get_graph_driver().session() as session:
            session.run(
                bounded_graph_query(
                    """
                    MERGE (tenant:Tenant {id: $tenant_id})
                    MERGE (user:User {id: $user_id})
                    MERGE (task:Task {id: $task_id})
                    SET task.tenant_id = $tenant_id,
                        task.title = $title,
                        task.status = $status,
                        task.priority = $priority,
                        task.updated_at = datetime()
                    MERGE (user)-[:MEMBER_OF]->(tenant)
                    MERGE (tenant)-[:CONTAINS]->(task)
                    MERGE (user)-[:OWNS]->(task)
                    WITH task
                    OPTIONAL MATCH (task)-[relationship:RELATES_TO]->(:Entity)
                    DELETE relationship
                    """
                ),
                tenant_id=identity.tenant_id,
                user_id=identity.user_id,
                task_id=str(task["id"]),
                title=task["title"],
                status=task["status"],
                priority=task["priority"],
            ).consume()
            if task.get("tags"):
                session.run(
                    bounded_graph_query(
                        """
                        MATCH (task:Task {id: $task_id, tenant_id: $tenant_id})
                        UNWIND $tags AS tag
                        MERGE (entity:Entity {tenant_id: $tenant_id, normalized_name: tag})
                        ON CREATE SET entity.name = tag, entity.type = 'TAG', entity.mention_count = 0
                        MERGE (task)-[:RELATES_TO]->(entity)
                        """
                    ),
                    task_id=str(task["id"]),
                    tenant_id=identity.tenant_id,
                    tags=task["tags"],
                ).consume()
    except Exception as error:
        logger.warning("Task graph synchronization degraded: %s", error)


@router.get("/tasks")
def list_tasks(
    status: Optional[TaskStatus] = None,
    q: Optional[str] = Query(default=None, max_length=500),
    limit: int = Query(default=50, ge=1, le=100),
    page_cursor: uuid.UUID | None = Query(default=None, alias="cursor"),
    identity: RequestIdentity = Depends(require_request_identity),
):
    filters = ["tenant_id = %s", "user_id = %s"]
    params: list[object] = [identity.tenant_id, identity.user_id]
    if status:
        filters.append("status = %s")
        params.append(status)
    clean_query = q.strip() if q else ""
    if clean_query:
        filters.append(
            "(search_vector @@ websearch_to_tsquery('english', %s) OR %s = ANY(tags))"
        )
        params.extend([clean_query, clean_query.casefold()])
    where_clause = " AND ".join(filters)

    with get_db_cursor() as cursor:
        if page_cursor is not None:
            cursor.execute(
                f"""
                SELECT created_at, id
                FROM tasks
                WHERE id = %s AND {where_clause}
                """,
                [str(page_cursor), *params],
            )
            anchor = cursor.fetchone()
            if not anchor:
                raise HTTPException(
                    status_code=422,
                    detail="The task page cursor is invalid.",
                )
        else:
            anchor = None

        page_filter = ""
        page_params = list(params)
        if anchor:
            page_filter = "AND (created_at, id) < (%s, %s)"
            page_params.extend([anchor["created_at"], anchor["id"]])
        page_params.append(limit + 1)
        cursor.execute(
            f"""
            SELECT {TASK_FIELDS}
            FROM tasks
            WHERE {where_clause}
              {page_filter}
            ORDER BY created_at DESC, id DESC
            LIMIT %s
            """,
            page_params,
        )
        rows = cursor.fetchall()
        has_more = len(rows) > limit
        tasks = [dict(task) for task in rows[:limit]]
        cursor.execute(
            """
            SELECT COUNT(*) FILTER (WHERE status = 'pending') AS pending,
                   COUNT(*) FILTER (WHERE status = 'in_progress') AS in_progress,
                   COUNT(*) FILTER (WHERE status = 'completed') AS completed,
                   COUNT(*) FILTER (WHERE status = 'cancelled') AS cancelled
            FROM tasks
            WHERE tenant_id = %s AND user_id = %s
            """,
            (identity.tenant_id, identity.user_id),
        )
        counts = dict(cursor.fetchone())
        return {
            "tasks": tasks,
            "status_counts": counts,
            "pagination": {
                "limit": limit,
                "next_cursor": str(tasks[-1]["id"]) if has_more else None,
            },
        }


@router.post("/tasks", status_code=201)
def create_task(
    request: TaskCreate,
    identity: RequestIdentity = Depends(require_request_identity),
):
    task_id = str(uuid.uuid4())
    with get_db_cursor() as cursor:
        cursor.execute(
            f"""
            INSERT INTO tasks (
                id, user_id, tenant_id, title, description, status,
                priority, due_date, tags, version, created_at, updated_at
            ) VALUES (%s, %s, %s, %s, %s, 'pending', %s, %s, %s, 1, NOW(), NOW())
            RETURNING {TASK_FIELDS}
            """,
            (
                task_id,
                identity.user_id,
                identity.tenant_id,
                request.title,
                request.description,
                request.priority,
                request.due_date,
                request.tags,
            ),
        )
        task = dict(cursor.fetchone())
    sync_task_graph(task, identity)
    return {"task": task}


@router.get("/tasks/{task_id}")
def get_task(
    task_id: uuid.UUID,
    identity: RequestIdentity = Depends(require_request_identity),
):
    with get_db_cursor() as cursor:
        cursor.execute(
            f"SELECT {TASK_FIELDS} FROM tasks WHERE id = %s AND tenant_id = %s AND user_id = %s",
            (str(task_id), identity.tenant_id, identity.user_id),
        )
        task = cursor.fetchone()
        if not task:
            raise HTTPException(status_code=404, detail="Task not found")
        return {"task": dict(task)}


@router.patch("/tasks/{task_id}")
def update_task(
    task_id: uuid.UUID,
    request: TaskUpdate,
    identity: RequestIdentity = Depends(require_request_identity),
):
    updates = request.model_dump(exclude={"version"}, exclude_unset=True)
    if not updates:
        raise HTTPException(status_code=422, detail="At least one task field must be updated")

    assignments = [f"{field} = %s" for field in updates]
    params = list(updates.values())
    params.extend([str(task_id), identity.tenant_id, identity.user_id, request.version])

    with get_db_cursor() as cursor:
        cursor.execute(
            f"""
            UPDATE tasks
            SET {', '.join(assignments)}, version = version + 1, updated_at = NOW()
            WHERE id = %s AND tenant_id = %s AND user_id = %s AND version = %s
            RETURNING {TASK_FIELDS}
            """,
            params,
        )
        task = cursor.fetchone()
        if not task:
            cursor.execute(
                "SELECT version FROM tasks WHERE id = %s AND tenant_id = %s AND user_id = %s",
                (str(task_id), identity.tenant_id, identity.user_id),
            )
            current = cursor.fetchone()
            if not current:
                raise HTTPException(status_code=404, detail="Task not found")
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "version_conflict",
                    "message": "The task changed since it was loaded.",
                    "current_version": current["version"],
                },
            )
        task_dict = dict(task)
    sync_task_graph(task_dict, identity)
    return {"task": task_dict}


@router.delete("/tasks/{task_id}")
def delete_task(
    task_id: uuid.UUID,
    identity: RequestIdentity = Depends(require_request_identity),
):
    with get_db_cursor(dict_cursor=False) as cursor:
        cursor.execute(
            "DELETE FROM tasks WHERE id = %s AND tenant_id = %s AND user_id = %s RETURNING id",
            (str(task_id), identity.tenant_id, identity.user_id),
        )
        if not cursor.fetchone():
            raise HTTPException(status_code=404, detail="Task not found")

    try:
        with get_graph_driver().session() as session:
            session.run(
                bounded_graph_query(
                    "MATCH (task:Task {id: $task_id, tenant_id: $tenant_id}) "
                    "DETACH DELETE task"
                ),
                task_id=str(task_id),
                tenant_id=identity.tenant_id,
            ).consume()
    except Exception as error:
        logger.warning("Task graph deletion degraded: %s", error)

    return {"deleted": True, "task_id": str(task_id)}
