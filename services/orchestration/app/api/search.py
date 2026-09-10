from typing import Any, Literal

from fastapi import APIRouter, Depends, Query

from app.core.db import get_db_cursor
from app.core.identity import RequestIdentity, require_request_identity
from app.retrieval.search_view import escape_like, result_count, search_snippet


router = APIRouter(prefix="", tags=["Workspace Search"])
SearchScope = Literal["all", "documents", "tasks", "memories", "chat"]


def _result(
    result_type: str,
    row: dict[str, Any],
    title: str,
    snippet_source: Any,
    query: str,
    href: str,
    metadata: dict[str, Any],
) -> dict[str, Any]:
    return {
        "type": result_type,
        "id": str(row["id"]),
        "title": title,
        "snippet": search_snippet(snippet_source, query),
        "href": href,
        "metadata": metadata,
        "created_at": row["created_at"],
        "rank": float(row.get("rank") or 0),
    }


@router.get("/search")
def search_workspace(
    q: str = Query(min_length=2, max_length=500),
    scope: SearchScope = "all",
    limit_per_type: int = Query(default=8, ge=1, le=25),
    identity: RequestIdentity = Depends(require_request_identity),
):
    query = " ".join(q.split())
    pattern = f"%{escape_like(query)}%"
    requested = {"documents", "tasks", "memories", "chat"} if scope == "all" else {scope}
    rows_by_type: dict[str, list[dict[str, Any]]] = {
        "documents": [],
        "tasks": [],
        "memories": [],
        "chat": [],
    }

    with get_db_cursor() as cursor:
        if "documents" in requested:
            cursor.execute(
                """
                WITH search_query AS (
                    SELECT websearch_to_tsquery('english', %s) AS value
                ),
                matched_chunks AS (
                    SELECT c.document_id, c.document_version_id,
                           version.version_number, version.title AS version_title,
                           version.source_time,
                           version.recorded_at,
                           (document.current_version_id = version.id) AS is_current_version,
                           c.content, c.page_number,
                           ts_rank_cd(c.search_vector, search_query.value) AS rank,
                           ROW_NUMBER() OVER (
                               PARTITION BY c.document_id
                               ORDER BY ts_rank_cd(c.search_vector, search_query.value) DESC,
                                        c.chunk_index ASC
                           ) AS match_position
                    FROM chunks c
                    JOIN documents AS document ON document.id = c.document_id
                    JOIN document_versions AS version
                      ON version.id = c.document_version_id
                     AND version.document_id = c.document_id
                    CROSS JOIN search_query
                    WHERE c.tenant_id = %s AND c.user_id = %s
                      AND document.tenant_id = %s AND document.user_id = %s
                      AND document.deleted_at IS NULL
                      AND version.tenant_id = %s AND version.user_id = %s
                      AND version.status = 'ready'
                      AND c.derivation_id = version.current_derivation_id
                      AND c.search_vector @@ search_query.value
                ),
                matches AS (
                    SELECT d.id, COALESCE(chunk.version_title, d.title) AS title,
                           d.source_type, d.created_at,
                           chunk.content AS matched_content,
                           chunk.page_number,
                           chunk.document_version_id,
                           chunk.version_number,
                           chunk.source_time,
                           chunk.recorded_at,
                           chunk.is_current_version,
                           GREATEST(
                               ts_rank_cd(d.title_search_vector, search_query.value) * 2,
                               COALESCE(chunk.rank, 0)
                           ) AS rank
                    FROM documents d
                    CROSS JOIN search_query
                    LEFT JOIN matched_chunks chunk
                      ON chunk.document_id = d.id AND chunk.match_position = 1
                    WHERE d.tenant_id = %s AND d.user_id = %s
                      AND d.deleted_at IS NULL
                      AND EXISTS (
                          SELECT 1
                          FROM document_versions AS available_version
                          WHERE available_version.document_id = d.id
                            AND available_version.tenant_id = d.tenant_id
                            AND available_version.user_id = d.user_id
                            AND available_version.status = 'ready'
                      )
                      AND (
                          d.title_search_vector @@ search_query.value
                          OR d.title ILIKE %s ESCAPE '\\'
                          OR chunk.document_id IS NOT NULL
                      )
                )
                SELECT *, COUNT(*) OVER() AS total_matches
                FROM matches
                ORDER BY rank DESC, created_at DESC, id DESC
                LIMIT %s
                """,
                (
                    query,
                    identity.tenant_id,
                    identity.user_id,
                    identity.tenant_id,
                    identity.user_id,
                    identity.tenant_id,
                    identity.user_id,
                    identity.tenant_id,
                    identity.user_id,
                    pattern,
                    limit_per_type,
                ),
            )
            rows_by_type["documents"] = [dict(row) for row in cursor.fetchall()]

        if "tasks" in requested:
            cursor.execute(
                """
                WITH search_query AS (
                    SELECT websearch_to_tsquery('english', %s) AS value
                ), matches AS (
                    SELECT task.id, task.title, task.description, task.status,
                           task.priority, task.tags, task.created_at,
                           ts_rank_cd(task.search_vector, search_query.value) AS rank
                    FROM tasks task
                    CROSS JOIN search_query
                    WHERE task.tenant_id = %s AND task.user_id = %s
                      AND (
                          task.search_vector @@ search_query.value
                          OR task.title ILIKE %s ESCAPE '\\'
                          OR %s = ANY(task.tags)
                      )
                )
                SELECT *, COUNT(*) OVER() AS total_matches
                FROM matches
                ORDER BY rank DESC, created_at DESC, id DESC
                LIMIT %s
                """,
                (query, identity.tenant_id, identity.user_id, pattern, query.casefold(), limit_per_type),
            )
            rows_by_type["tasks"] = [dict(row) for row in cursor.fetchall()]

        if "memories" in requested:
            cursor.execute(
                """
                WITH search_query AS (
                    SELECT websearch_to_tsquery('english', %s) AS value
                ), matches AS (
                    SELECT memory.id, memory.fact, memory.category, memory.confidence,
                           memory.created_at,
                           ts_rank_cd(memory.search_vector, search_query.value) AS rank
                    FROM memories memory
                    CROSS JOIN search_query
                    WHERE memory.tenant_id = %s AND memory.user_id = %s
                      AND memory.is_active = true
                      AND memory.search_vector @@ search_query.value
                )
                SELECT *, COUNT(*) OVER() AS total_matches
                FROM matches
                ORDER BY rank DESC, created_at DESC, id DESC
                LIMIT %s
                """,
                (query, identity.tenant_id, identity.user_id, limit_per_type),
            )
            rows_by_type["memories"] = [dict(row) for row in cursor.fetchall()]

        if "chat" in requested:
            cursor.execute(
                """
                WITH search_query AS (
                    SELECT websearch_to_tsquery('english', %s) AS value
                ), matches AS (
                    SELECT run.id, run.input_query, run.output_response, run.model_used,
                           run.status, run.created_at,
                           ts_rank_cd(run.search_vector, search_query.value) AS rank
                    FROM agent_runs run
                    CROSS JOIN search_query
                    WHERE run.tenant_id = %s AND run.user_id = %s
                      AND run.search_vector @@ search_query.value
                )
                SELECT *, COUNT(*) OVER() AS total_matches
                FROM matches
                ORDER BY rank DESC, created_at DESC, id DESC
                LIMIT %s
                """,
                (query, identity.tenant_id, identity.user_id, limit_per_type),
            )
            rows_by_type["chat"] = [dict(row) for row in cursor.fetchall()]

    results: list[dict[str, Any]] = []
    for row in rows_by_type["documents"]:
        document_href = f"/documents/{row['id']}"
        if row.get("version_number"):
            document_href += f"?version={int(row['version_number'])}"
        results.append(
            _result(
                "documents",
                row,
                row["title"] or "Untitled document",
                row.get("matched_content") or row["title"],
                query,
                document_href,
                {
                    "source_type": row["source_type"],
                    "page_number": row.get("page_number"),
                    "document_version_id": (
                        str(row["document_version_id"])
                        if row.get("document_version_id") else None
                    ),
                    "version_number": row.get("version_number"),
                    "source_time": (
                        row["source_time"].isoformat() if row.get("source_time") else None
                    ),
                    "recorded_at": (
                        row["recorded_at"].isoformat() if row.get("recorded_at") else None
                    ),
                    "is_current_version": row.get("is_current_version"),
                },
            )
        )
    for row in rows_by_type["tasks"]:
        results.append(
            _result(
                "tasks",
                row,
                row["title"],
                row.get("description") or " ".join(row.get("tags") or []),
                query,
                "/tasks",
                {"status": row["status"], "priority": row["priority"], "tags": row.get("tags") or []},
            )
        )
    for row in rows_by_type["memories"]:
        results.append(
            _result(
                "memories",
                row,
                row["fact"],
                row["fact"],
                query,
                "/memory",
                {"category": row["category"], "confidence": float(row["confidence"])},
            )
        )
    for row in rows_by_type["chat"]:
        results.append(
            _result(
                "chat",
                row,
                row["input_query"],
                row.get("output_response") or row["input_query"],
                query,
                f"/traces/{row['id']}",
                {"model_used": row.get("model_used"), "status": row["status"]},
            )
        )

    type_order = {"documents": 0, "tasks": 1, "memories": 2, "chat": 3}
    results.sort(key=lambda result: (-result["rank"], type_order[result["type"]], result["title"].casefold()))
    facets = {
        result_type: {
            "count": result_count(rows),
            "returned": len(rows),
            "has_more": result_count(rows) > len(rows),
        }
        for result_type, rows in rows_by_type.items()
    }
    return {"query": query, "scope": scope, "results": results, "facets": facets}
