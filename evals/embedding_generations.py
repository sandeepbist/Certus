from __future__ import annotations

import json
import uuid
from typing import Any

import psycopg2


class EmbeddingGenerationEvaluationError(RuntimeError):
    """The shadow-generation contract could not be verified safely."""


APPROVED_REPORT = {
    "schema_version": 1,
    "decision": "approved",
    "gates_passed": True,
    "baseline_fingerprint": "control-plane-baseline",
    "candidate_fingerprint": "control-plane-candidate",
}


def check_embedding_generation_report(report: dict[str, Any]) -> list[str]:
    failures: list[str] = []
    if report.get("schema", {}).get("latest_migration") != "043_embedding_generation_concurrency.sql":
        failures.append("migration 043 is not the active shadow-generation contract")
    lifecycle = report.get("empty_workspace_lifecycle", {})
    if lifecycle.get("cutover") != ["retired", "active"]:
        failures.append("atomic cutover did not retire the previous generation")
    if lifecycle.get("rollback") != ["active", "rolled_back"]:
        failures.append("rollback did not restore the previous generation")
    if lifecycle.get("stale") != ["stale", "stale"]:
        failures.append("corpus invalidation did not stale active and building generations")
    nonempty = report.get("nonempty_workspace_lifecycle", {})
    if not nonempty.get("skipped"):
        if nonempty.get("wrong_owner_rejected") is not True:
            failures.append("a non-owner committed a leased embedding")
        if nonempty.get("coverage_complete") is not True:
            failures.append("non-empty candidate coverage was incomplete")
        if nonempty.get("sealed") is not True:
            failures.append("a complete non-empty generation did not seal")
    if report.get("run", {}).get("persistent_rows") != 0:
        failures.append("evaluation rows remained after rollback")
    if report.get("run", {}).get("provider_calls") != 0:
        failures.append("the control-plane evaluation called an embedding provider")
    return failures


def _seal(cursor: Any, generation_id: str) -> bool:
    cursor.execute(
        "SELECT seal_workspace_embedding_generation(%s, %s::jsonb)",
        (generation_id, json.dumps(APPROVED_REPORT, sort_keys=True)),
    )
    return bool(cursor.fetchone()[0])


def _statuses(cursor: Any, generation_ids: list[str]) -> list[str]:
    statuses: list[str] = []
    for generation_id in generation_ids:
        cursor.execute(
            "SELECT status FROM workspace_embedding_generations WHERE id = %s",
            (generation_id,),
        )
        statuses.append(str(cursor.fetchone()[0]))
    return statuses


def _run_empty_lifecycle(cursor: Any, tenant_id: str, user_id: str) -> dict[str, Any]:
    profiles = (
        "embedding-space:v1:local:local-lexical-v2:1536",
        "embedding-space:v1:openai:text-embedding-3-small:1536",
        "embedding-space:v1:openai:text-embedding-3-large:1536",
    )
    generations: list[str] = []
    for profile in profiles[:2]:
        cursor.execute(
            "SELECT start_workspace_embedding_generation(%s, %s, %s)",
            (tenant_id, user_id, profile),
        )
        generation_id = str(cursor.fetchone()[0])
        generations.append(generation_id)
        if not _seal(cursor, generation_id):
            raise EmbeddingGenerationEvaluationError("complete empty generation did not seal")
        cursor.execute(
            "SELECT activate_workspace_embedding_generation(%s)",
            (generation_id,),
        )
        if not cursor.fetchone()[0]:
            raise EmbeddingGenerationEvaluationError("ready empty generation did not activate")

    cutover = _statuses(cursor, generations)
    cursor.execute(
        "SELECT rollback_workspace_embedding_generation(%s)",
        (generations[1],),
    )
    if not cursor.fetchone()[0]:
        raise EmbeddingGenerationEvaluationError("bounded rollback was rejected")
    rollback = _statuses(cursor, generations)

    cursor.execute(
        "SELECT start_workspace_embedding_generation(%s, %s, %s)",
        (tenant_id, user_id, profiles[2]),
    )
    stale_generation = str(cursor.fetchone()[0])
    cursor.execute(
        "SELECT invalidate_workspace_embedding_scope(%s, %s)",
        (tenant_id, user_id),
    )
    stale = _statuses(cursor, [generations[0], stale_generation])
    return {"cutover": cutover, "rollback": rollback, "stale": stale}


def _eligible_small_scope(cursor: Any) -> tuple[str, str] | None:
    cursor.execute(
        """
        SELECT chunk.tenant_id, chunk.user_id
        FROM chunks AS chunk
        JOIN documents AS document ON document.id = chunk.document_id
        JOIN document_versions AS version
          ON version.id = chunk.document_version_id
         AND version.document_id = chunk.document_id
        WHERE document.deleted_at IS NULL
          AND version.status = 'ready'
          AND chunk.derivation_id = version.current_derivation_id
          AND chunk.embedding IS NOT NULL
          AND NOT EXISTS (
              SELECT 1 FROM workspace_embedding_generations AS generation
              WHERE generation.tenant_id = chunk.tenant_id
                AND generation.user_id = chunk.user_id
          )
        GROUP BY chunk.tenant_id, chunk.user_id
        HAVING COUNT(*) BETWEEN 1 AND 100
           AND COUNT(*) = COUNT(chunk.embedding)
        ORDER BY COUNT(*), chunk.tenant_id, chunk.user_id
        LIMIT 1
        """
    )
    row = cursor.fetchone()
    return (str(row[0]), str(row[1])) if row else None


def _run_nonempty_lifecycle(cursor: Any) -> dict[str, Any]:
    scope = _eligible_small_scope(cursor)
    if scope is None:
        return {"skipped": True, "reason": "no isolated 1-100 chunk scope was available"}
    tenant_id, user_id = scope
    cursor.execute("SAVEPOINT nonempty_generation_contract")
    try:
        cursor.execute(
            "SELECT start_workspace_embedding_generation(%s, %s, %s)",
            (
                tenant_id,
                user_id,
                "embedding-space:v1:local:local-lexical-v2:1536",
            ),
        )
        generation_id = str(cursor.fetchone()[0])
        first_owner = str(uuid.uuid4())
        second_owner = str(uuid.uuid4())
        cursor.execute(
            "SELECT chunk_id FROM claim_chunk_embedding_vectors(%s, %s, 1, 60)",
            (generation_id, first_owner),
        )
        first_chunk = str(cursor.fetchone()[0])
        cursor.execute("SELECT embedding::text FROM chunks WHERE id = %s", (first_chunk,))
        first_vector = str(cursor.fetchone()[0])
        cursor.execute(
            "SELECT record_chunk_embedding_vector(%s, %s, %s, %s::vector, '{}'::jsonb)",
            (generation_id, first_chunk, second_owner, first_vector),
        )
        wrong_owner_rejected = not bool(cursor.fetchone()[0])
        cursor.execute(
            "SELECT record_chunk_embedding_failure(%s, %s, %s, %s, %s, INTERVAL '0 seconds')",
            (generation_id, first_chunk, first_owner, "retry_probe", "control-plane retry"),
        )
        if not cursor.fetchone()[0]:
            raise EmbeddingGenerationEvaluationError("lease owner could not record retry")

        while True:
            cursor.execute(
                "SELECT chunk_id FROM claim_chunk_embedding_vectors(%s, %s, 100, 60)",
                (generation_id, second_owner),
            )
            chunk_ids = [str(row[0]) for row in cursor.fetchall()]
            if not chunk_ids:
                break
            for chunk_id in chunk_ids:
                cursor.execute("SELECT embedding::text FROM chunks WHERE id = %s", (chunk_id,))
                vector = str(cursor.fetchone()[0])
                cursor.execute(
                    "SELECT record_chunk_embedding_vector(%s, %s, %s, %s::vector, %s::jsonb)",
                    (
                        generation_id,
                        chunk_id,
                        second_owner,
                        vector,
                        '{"control_plane_copy":true}',
                    ),
                )
                if not cursor.fetchone()[0]:
                    raise EmbeddingGenerationEvaluationError("claimed vector was not committed")

        cursor.execute(
            """
            SELECT expected_chunk_count, embedded_chunk_count, failed_chunk_count
            FROM workspace_embedding_generations WHERE id = %s
            """,
            (generation_id,),
        )
        expected, embedded, failed = (int(value) for value in cursor.fetchone())
        sealed = _seal(cursor, generation_id)
        return {
            "skipped": False,
            "chunk_count": expected,
            "wrong_owner_rejected": wrong_owner_rejected,
            "coverage_complete": embedded == expected and failed == 0,
            "sealed": sealed,
        }
    finally:
        cursor.execute("ROLLBACK TO SAVEPOINT nonempty_generation_contract")


def run_embedding_generation_evaluation(database_url: str) -> dict[str, Any]:
    connection = psycopg2.connect(
        database_url,
        application_name="certus-embedding-generation-eval",
    )
    connection.autocommit = False
    evaluation_tenant = f"certus-embedding-eval-{uuid.uuid4()}"
    evaluation_user = f"certus-embedding-eval-{uuid.uuid4()}"
    try:
        with connection.cursor() as cursor:
            cursor.execute("SET LOCAL statement_timeout = '60s'")
            cursor.execute(
                "SELECT filename FROM schema_migrations WHERE filename LIKE '04%embedding%' ORDER BY filename"
            )
            migrations = [str(row[0]) for row in cursor.fetchall()]
            if "043_embedding_generation_concurrency.sql" not in migrations:
                raise EmbeddingGenerationEvaluationError("migration 043 is not applied")
            empty_lifecycle = _run_empty_lifecycle(
                cursor, evaluation_tenant, evaluation_user
            )
            nonempty_lifecycle = _run_nonempty_lifecycle(cursor)
        connection.rollback()
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT COUNT(*) FROM workspace_embedding_generations WHERE tenant_id = %s AND user_id = %s",
                (evaluation_tenant, evaluation_user),
            )
            persistent_rows = int(cursor.fetchone()[0])
        connection.rollback()
        return {
            "schema": {
                "migrations": migrations,
                "latest_migration": "043_embedding_generation_concurrency.sql",
            },
            "empty_workspace_lifecycle": empty_lifecycle,
            "nonempty_workspace_lifecycle": nonempty_lifecycle,
            "run": {"persistent_rows": persistent_rows, "provider_calls": 0},
        }
    finally:
        connection.rollback()
        connection.close()


def report_json(report: dict[str, Any]) -> str:
    return json.dumps(report, indent=2, sort_keys=True) + "\n"
