from __future__ import annotations

import json
import uuid
from typing import Any

import psycopg2

from services.shared.document_retrieval import build_document_semantic_query
from services.shared.embedding_generation_worker import (
    activate_next_embedding_generation_refresh,
    claim_next_embedding_generation_batch,
    qualify_next_embedding_generation,
    record_embedding_generation_batch,
    record_embedding_generation_failure,
    start_next_embedding_generation_refresh,
)
from services.shared.embeddings import (
    LOCAL_EMBEDDING_PROFILE,
    local_lexical_embedding,
)
from services.shared.pgvector_policy import configure_filtered_hnsw


class EmbeddingGenerationEvaluationError(RuntimeError):
    """The shadow-generation contract could not be verified safely."""


def check_embedding_generation_report(report: dict[str, Any]) -> list[str]:
    failures: list[str] = []
    if (
        report.get("schema", {}).get("latest_migration")
        != "065_embedding_generation_pause_resume.sql"
    ):
        failures.append("migration 065 is not the active embedding-generation contract")
    lifecycle = report.get("empty_workspace_lifecycle", {})
    if lifecycle.get("cutover") != ["retired", "active"]:
        failures.append("atomic cutover did not retire the previous generation")
    if lifecycle.get("rollback") != ["active", "rolled_back"]:
        failures.append("rollback did not restore the previous generation")
    if lifecycle.get("stale") != ["active", "stale"]:
        failures.append(
            "corpus invalidation did not retain the active baseline and stale open work"
        )
    if lifecycle.get("forged_report_rejected") is not True:
        failures.append("activation accepted a caller-authored evaluation report")
    if lifecycle.get("automatic_refresh") is not True:
        failures.append("an empty revision-lagged workspace did not refresh automatically")
    if lifecycle.get("stale_paused_resume_rejected") is not True:
        failures.append("a corpus-stale paused generation resumed")
    nonempty = report.get("nonempty_workspace_lifecycle", {})
    if not nonempty.get("skipped"):
        if nonempty.get("wrong_owner_rejected") is not True:
            failures.append("a non-owner committed a leased embedding")
        if nonempty.get("coverage_complete") is not True:
            failures.append("non-empty candidate coverage was incomplete")
        if nonempty.get("sealed") is not True:
            failures.append("a complete non-empty generation did not seal")
        if nonempty.get("activated") is not True:
            failures.append("a sealed non-empty generation did not activate")
        if nonempty.get("post_activation_insert_rejected") is not True:
            failures.append("an active generation accepted a late candidate insert")
        if nonempty.get("serving_query_uses_hnsw") is not True:
            failures.append("active-generation retrieval did not use its serving HNSW graph")
        if nonempty.get("served_generation_bound") is not True:
            failures.append("active-generation retrieval returned an unbound vector")
        if nonempty.get("serving_membership_complete") is not True:
            failures.append("active generation did not publish its complete vector set")
        if nonempty.get("cutover_serving_counts") != [0, nonempty.get("chunk_count")]:
            failures.append("cutover did not move serving membership atomically")
        if nonempty.get("rollback_serving_counts") != [nonempty.get("chunk_count"), 0]:
            failures.append("rollback did not restore serving membership atomically")
        if nonempty.get("active_baseline_retained_after_corpus_change") is not True:
            failures.append("corpus change removed the approved active ANN baseline")
        if nonempty.get("automatic_refresh_reused_all_vectors") is not True:
            failures.append("automatic same-profile refresh did not reuse unchanged vectors")
        if nonempty.get("automatic_refresh_provider_candidates") != 0:
            failures.append("automatic unchanged-corpus refresh scheduled provider work")
        if nonempty.get("automatic_refresh_activated") is not True:
            failures.append("qualified automatic same-profile refresh did not cut over")
        if nonempty.get("automatic_refresh_cutover_counts") != [
            0,
            nonempty.get("automatic_refresh_chunk_count"),
        ]:
            failures.append("automatic refresh cutover did not switch serving membership")
        if nonempty.get("manual_generation_required_operator_activation") is not True:
            failures.append("automatic cutover accepted an operator-authored generation")
        if nonempty.get("automatic_refresh_waited_for_processing") is not True:
            failures.append("automatic refresh started while document processing was open")
        if nonempty.get("pause_released_inflight_lease") is not True:
            failures.append("pausing did not release an in-flight candidate lease")
        if nonempty.get("paused_claim_blocked") is not True:
            failures.append("a paused generation admitted candidate work")
        if nonempty.get("resume_reopened_generation") is not True:
            failures.append("a current paused generation did not resume")
        if int(nonempty.get("worker_dispatch_batches", 0)) < 1:
            failures.append("the generation worker scheduler did not dispatch a batch")
        if nonempty.get("attempt_ceiling_failed_generation") is not True:
            failures.append("the generation worker attempt ceiling did not fail closed")
        if nonempty.get("integrity_rejected_zero_vectors") is not True:
            failures.append("integrity qualification accepted zero cosine vectors")
    if report.get("run", {}).get("persistent_rows") != 0:
        failures.append("evaluation rows remained after rollback")
    if report.get("run", {}).get("provider_calls") != 0:
        failures.append("the control-plane evaluation called an embedding provider")
    return failures


def _qualify(cursor: Any, generation_id: str) -> bool:
    cursor.execute(
        "SELECT embedding_profile FROM workspace_embedding_generations WHERE id = %s",
        (generation_id,),
    )
    profile = str(cursor.fetchone()[0])
    qualified_id = qualify_next_embedding_generation(
        cursor,
        embedding_profile=profile,
    )
    if qualified_id != generation_id:
        return False
    cursor.execute(
        "SELECT evaluation_report FROM workspace_embedding_generations WHERE id = %s",
        (generation_id,),
    )
    report = cursor.fetchone()[0]
    return (
        report.get("evaluation_profile") == "embedding_generation_integrity_v1"
        and report.get("evaluation_source") == "database_authoritative"
        and report.get("quality_claim") == "not_evaluated"
        and report.get("gates_passed") is True
    )


def _qualify_eventually(cursor: Any, generation_id: str) -> bool:
    """Let the fair scheduler service older complete work before this target."""
    for _ in range(10):
        if _qualify(cursor, generation_id):
            return True
        cursor.execute(
            "SELECT status FROM workspace_embedding_generations WHERE id = %s",
            (generation_id,),
        )
        if cursor.fetchone()[0] != "building":
            return False
    return False


def _statuses(cursor: Any, generation_ids: list[str]) -> list[str]:
    statuses: list[str] = []
    for generation_id in generation_ids:
        cursor.execute(
            "SELECT status FROM workspace_embedding_generations WHERE id = %s",
            (generation_id,),
        )
        statuses.append(str(cursor.fetchone()[0]))
    return statuses


def _contains_index_scan(plan: Any, index_name: str) -> bool:
    if isinstance(plan, dict):
        if plan.get("Index Name") == index_name:
            return True
        return any(_contains_index_scan(value, index_name) for value in plan.values())
    if isinstance(plan, list):
        return any(_contains_index_scan(value, index_name) for value in plan)
    return False


def _serving_counts(cursor: Any, generation_ids: list[str]) -> list[int]:
    counts: list[int] = []
    for generation_id in generation_ids:
        cursor.execute(
            """
            SELECT COUNT(*)
            FROM chunk_embedding_vectors
            WHERE generation_id = %s AND is_serving = true
            """,
            (generation_id,),
        )
        counts.append(int(cursor.fetchone()[0]))
    return counts


def _vector_literal(values: list[float]) -> str:
    return f"[{','.join(str(value) for value in values)}]"


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
        if not _qualify(cursor, generation_id):
            raise EmbeddingGenerationEvaluationError(
                "complete empty generation did not qualify"
            )
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
        """
        UPDATE workspace_embedding_generations
        SET status = 'ready',
            evaluation_report = '{"decision":"approved","gates_passed":true}'::jsonb,
            sealed_at = NOW(), updated_at = NOW()
        WHERE id = %s
        """,
        (stale_generation,),
    )
    cursor.execute(
        "SELECT activate_workspace_embedding_generation(%s)",
        (stale_generation,),
    )
    forged_report_rejected = not bool(cursor.fetchone()[0])
    cursor.execute(
        "SELECT invalidate_workspace_embedding_scope(%s, %s)",
        (tenant_id, user_id),
    )
    stale = _statuses(cursor, [generations[0], stale_generation])
    cursor.execute(
        "SELECT start_workspace_embedding_generation(%s, %s, %s)",
        (tenant_id, user_id, profiles[2]),
    )
    paused_stale_generation_id = str(cursor.fetchone()[0])
    cursor.execute(
        "SELECT pause_workspace_embedding_generation(%s, %s, %s, %s)",
        (
            paused_stale_generation_id,
            tenant_id,
            user_id,
            "corpus drift probe",
        ),
    )
    if not cursor.fetchone()[0]:
        raise EmbeddingGenerationEvaluationError("empty generation did not pause")
    cursor.execute(
        "SELECT invalidate_workspace_embedding_scope(%s, %s)",
        (tenant_id, user_id),
    )
    cursor.execute(
        "SELECT resume_workspace_embedding_generation(%s, %s, %s)",
        (paused_stale_generation_id, tenant_id, user_id),
    )
    resumed_stale = bool(cursor.fetchone()[0])
    cursor.execute(
        "SELECT status, is_paused FROM workspace_embedding_generations WHERE id = %s",
        (paused_stale_generation_id,),
    )
    paused_stale_status = cursor.fetchone()
    automatic_generation_id = start_next_embedding_generation_refresh(
        cursor,
        embedding_profile=profiles[0],
        quiet_period_seconds=0,
    )
    if automatic_generation_id is None or not _qualify_eventually(
        cursor, automatic_generation_id
    ):
        raise EmbeddingGenerationEvaluationError(
            "empty revision-lagged workspace did not build an automatic refresh"
        )
    automatic_refresh = (
        activate_next_embedding_generation_refresh(
            cursor,
            embedding_profile=profiles[0],
            rollback_window_hours=168,
        )
        == automatic_generation_id
    )
    return {
        "cutover": cutover,
        "rollback": rollback,
        "stale": stale,
        "forged_report_rejected": forged_report_rejected,
        "automatic_refresh": automatic_refresh,
        "stale_paused_resume_rejected": (
            not resumed_stale and paused_stale_status == ("stale", False)
        ),
    }


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
          AND chunk.embedding_profile = %s
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
        """,
        (LOCAL_EMBEDDING_PROFILE.identifier,),
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

        cursor.execute(
            "SELECT chunk_id FROM claim_chunk_embedding_vectors(%s, %s, 1, 60)",
            (generation_id, second_owner),
        )
        paused_candidate = cursor.fetchone()
        if paused_candidate is None:
            raise EmbeddingGenerationEvaluationError(
                "pause probe could not claim an in-flight candidate"
            )
        paused_chunk_id = str(paused_candidate[0])
        cursor.execute(
            "SELECT pause_workspace_embedding_generation(%s, %s, %s, %s)",
            (generation_id, tenant_id, user_id, "evaluation pause"),
        )
        if not cursor.fetchone()[0]:
            raise EmbeddingGenerationEvaluationError("building generation did not pause")
        cursor.execute(
            """
            SELECT generation.is_paused, candidate.status,
                   candidate.lease_owner, candidate.leased_at
            FROM workspace_embedding_generations AS generation
            JOIN chunk_embedding_vectors AS candidate
              ON candidate.generation_id = generation.id
            WHERE generation.id = %s AND candidate.chunk_id = %s
            """,
            (generation_id, paused_chunk_id),
        )
        paused_state = cursor.fetchone()
        pause_released_inflight_lease = paused_state == (
            True,
            "pending",
            None,
            None,
        )
        cursor.execute(
            "SELECT chunk_id FROM claim_chunk_embedding_vectors(%s, %s, 1, 60)",
            (generation_id, first_owner),
        )
        paused_claim_blocked = cursor.fetchone() is None
        cursor.execute(
            "SELECT resume_workspace_embedding_generation(%s, %s, %s)",
            (generation_id, tenant_id, user_id),
        )
        resume_reopened_generation = bool(cursor.fetchone()[0])

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
        sealed = _qualify(cursor, generation_id)
        cursor.execute(
            "SELECT activate_workspace_embedding_generation(%s)",
            (generation_id,),
        )
        activated = bool(cursor.fetchone()[0])
        cursor.execute(
            """
            SELECT COUNT(*)
            FROM chunk_embedding_vectors
            WHERE generation_id = %s
              AND status = 'embedded'
              AND is_serving = true
              AND embedding_profile = %s
            """,
            (generation_id, LOCAL_EMBEDDING_PROFILE.identifier),
        )
        serving_count = int(cursor.fetchone()[0])

        cursor.execute("SAVEPOINT post_activation_insert_fence")
        try:
            cursor.execute(
                """
                INSERT INTO chunk_embedding_vectors (
                    generation_id, chunk_id, tenant_id, user_id,
                    content_sha256, status, embedding, provider_metadata,
                    attempt_count, embedded_at, embedding_profile, is_serving
                )
                SELECT generation_id, chunk_id, tenant_id, user_id,
                       content_sha256, status, embedding, provider_metadata,
                       attempt_count, embedded_at, embedding_profile, is_serving
                FROM chunk_embedding_vectors
                WHERE generation_id = %s
                LIMIT 1
                """,
                (generation_id,),
            )
        except psycopg2.Error as error:
            post_activation_insert_rejected = (
                "embedding candidates can be added only while a generation is building"
                in str(error)
            )
            cursor.execute("ROLLBACK TO SAVEPOINT post_activation_insert_fence")
        else:
            cursor.execute("ROLLBACK TO SAVEPOINT post_activation_insert_fence")
            post_activation_insert_rejected = False

        cursor.execute(
            "SELECT start_workspace_embedding_generation(%s, %s, %s)",
            (tenant_id, user_id, LOCAL_EMBEDDING_PROFILE.identifier),
        )
        replacement_generation_id = str(cursor.fetchone()[0])
        replacement_owner = str(uuid.uuid4())
        worker_dispatch_batches = 0
        while True:
            candidates = claim_next_embedding_generation_batch(
                cursor,
                embedding_profile=LOCAL_EMBEDDING_PROFILE.identifier,
                lease_owner=replacement_owner,
                batch_size=100,
                lease_seconds=60,
            )
            if not candidates:
                break
            if any(
                candidate.generation_id != replacement_generation_id
                for candidate in candidates
            ):
                raise EmbeddingGenerationEvaluationError(
                    "worker scheduler crossed embedding generations"
                )
            worker_dispatch_batches += 1
            record_embedding_generation_batch(
                cursor,
                candidates=candidates,
                lease_owner=replacement_owner,
                embeddings=[
                    local_lexical_embedding(candidate.embedding_input)
                    for candidate in candidates
                ],
                provider_metadata={"worker_dispatch_probe": True},
            )
        if not _qualify(cursor, replacement_generation_id):
            raise EmbeddingGenerationEvaluationError(
                "complete replacement generation did not seal"
            )
        manual_generation_required_operator_activation = (
            activate_next_embedding_generation_refresh(
                cursor,
                embedding_profile=LOCAL_EMBEDDING_PROFILE.identifier,
                rollback_window_hours=168,
            )
            is None
        )
        cursor.execute(
            "SELECT activate_workspace_embedding_generation(%s)",
            (replacement_generation_id,),
        )
        if not cursor.fetchone()[0]:
            raise EmbeddingGenerationEvaluationError(
                "complete replacement generation did not activate"
            )
        cutover_serving_counts = _serving_counts(
            cursor,
            [generation_id, replacement_generation_id],
        )
        cursor.execute(
            "SELECT rollback_workspace_embedding_generation(%s)",
            (replacement_generation_id,),
        )
        if not cursor.fetchone()[0]:
            raise EmbeddingGenerationEvaluationError(
                "replacement generation did not roll back"
            )
        rollback_serving_counts = _serving_counts(
            cursor,
            [generation_id, replacement_generation_id],
        )

        cursor.execute(
            "SELECT start_workspace_embedding_generation(%s, %s, %s)",
            (tenant_id, user_id, LOCAL_EMBEDDING_PROFILE.identifier),
        )
        exhausted_generation_id = str(cursor.fetchone()[0])
        exhausted_owner = str(uuid.uuid4())
        exhausted_candidates = claim_next_embedding_generation_batch(
            cursor,
            embedding_profile=LOCAL_EMBEDDING_PROFILE.identifier,
            lease_owner=exhausted_owner,
            batch_size=1,
            lease_seconds=60,
        )
        if (
            not exhausted_candidates
            or exhausted_candidates[0].generation_id != exhausted_generation_id
        ):
            raise EmbeddingGenerationEvaluationError(
                "worker scheduler did not claim the exhaustion probe"
            )
        attempt_ceiling_triggered = record_embedding_generation_failure(
            cursor,
            candidates=exhausted_candidates,
            lease_owner=exhausted_owner,
            error_code="evaluation_failure",
            error_message="intentional worker attempt-ceiling probe",
            retry_delay_seconds=0,
            max_attempts=1,
        )
        cursor.execute(
            "SELECT status FROM workspace_embedding_generations WHERE id = %s",
            (exhausted_generation_id,),
        )
        attempt_ceiling_failed_generation = (
            attempt_ceiling_triggered and cursor.fetchone()[0] == "failed"
        )

        cursor.execute(
            "SELECT start_workspace_embedding_generation(%s, %s, %s)",
            (tenant_id, user_id, LOCAL_EMBEDDING_PROFILE.identifier),
        )
        zero_generation_id = str(cursor.fetchone()[0])
        zero_owner = str(uuid.uuid4())
        while True:
            zero_candidates = claim_next_embedding_generation_batch(
                cursor,
                embedding_profile=LOCAL_EMBEDDING_PROFILE.identifier,
                lease_owner=zero_owner,
                batch_size=100,
                lease_seconds=60,
            )
            if not zero_candidates:
                break
            record_embedding_generation_batch(
                cursor,
                candidates=zero_candidates,
                lease_owner=zero_owner,
                embeddings=[
                    [0.0] * LOCAL_EMBEDDING_PROFILE.dimensions
                    for _ in zero_candidates
                ],
                provider_metadata={"integrity_rejection_probe": True},
            )
        _qualify(cursor, zero_generation_id)
        cursor.execute(
            """
            SELECT status, evaluation_report->>'decision'
            FROM workspace_embedding_generations WHERE id = %s
            """,
            (zero_generation_id,),
        )
        zero_status, zero_decision = cursor.fetchone()
        integrity_rejected_zero_vectors = (
            zero_status == "failed" and zero_decision == "rejected"
        )

        semantic_query = build_document_semantic_query(
            vector_literal=first_vector,
            tenant_id=tenant_id,
            user_id=user_id,
            embedding_profile=LOCAL_EMBEDDING_PROFILE.identifier,
            embedding_generation_id=generation_id,
            minimum_similarity=-1.0,
            candidate_limit=min(expected, 20),
        )
        configure_filtered_hnsw(cursor)
        cursor.execute("SET LOCAL enable_seqscan = off")
        cursor.execute(semantic_query.sql, semantic_query.params)
        served_rows = cursor.fetchall()
        cursor.execute(
            "EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, FORMAT JSON) "
            + semantic_query.sql,
            semantic_query.params,
        )
        plan = cursor.fetchone()[0][0]
        cursor.execute("SET LOCAL enable_seqscan = on")

        cursor.execute(
            """
            SELECT version.document_id, version.id, derivation.id,
                   derivation.processing_generation,
                   left(parsed.content_text, 48),
                   COALESCE(MAX(chunk.chunk_index), -1) + 1
            FROM document_versions AS version
            JOIN document_derivations AS derivation
              ON derivation.id = version.current_derivation_id
            JOIN document_parsed_artifacts AS parsed
              ON parsed.id = derivation.input_parsed_artifact_id
            LEFT JOIN chunks AS chunk
              ON chunk.derivation_id = derivation.id
            WHERE version.tenant_id = %s
              AND version.user_id = %s
              AND version.status = 'ready'
              AND parsed.status = 'ready'
              AND char_length(parsed.content_text) >= 1
            GROUP BY version.document_id, version.id, derivation.id,
                     derivation.processing_generation, parsed.content_text
            ORDER BY version.id
            LIMIT 1
            """,
            (tenant_id, user_id),
        )
        delta_source = cursor.fetchone()
        if delta_source is None:
            raise EmbeddingGenerationEvaluationError(
                "non-empty scope had no parsed artifact for the delta reuse probe"
            )
        (
            delta_document_id,
            delta_version_id,
            delta_derivation_id,
            delta_processing_generation,
            delta_content,
            delta_chunk_index,
        ) = delta_source
        cursor.execute(
            """
            INSERT INTO chunks (
                id, document_id, document_version_id, derivation_id,
                tenant_id, user_id, content, embedding, chunk_index,
                token_count, start_char, end_char, language, metadata,
                processing_generation, embedded_at, embedding_profile,
                text_locator_status, text_locator_profile
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s::vector, %s,
                %s, 0, %s, 'en', '{"evaluation_delta":true}'::jsonb,
                %s, NOW(), %s, 'exact',
                'unicode_code_point:zero_based_half_open:v1'
            )
            """,
            (
                str(uuid.uuid4()),
                delta_document_id,
                delta_version_id,
                delta_derivation_id,
                tenant_id,
                user_id,
                str(delta_content),
                _vector_literal(local_lexical_embedding(str(delta_content))),
                int(delta_chunk_index),
                len(str(delta_content).split()),
                len(str(delta_content)),
                delta_processing_generation,
                LOCAL_EMBEDDING_PROFILE.identifier,
            ),
        )
        cursor.execute(
            """
            SELECT COUNT(*)
            FROM chunk_embedding_vectors
            WHERE generation_id = %s AND is_serving = true
            """,
            (generation_id,),
        )
        post_change_serving_count = int(cursor.fetchone()[0])
        cursor.execute(
            """
            WITH target AS MATERIALIZED (
                SELECT derivation.id
                FROM document_derivations AS derivation
                JOIN document_versions AS version
                  ON derivation.id = version.current_derivation_id
                WHERE version.tenant_id = %s
                  AND version.user_id = %s
                  AND version.status = 'ready'
                  AND derivation.status = 'ready'
                ORDER BY derivation.id
                LIMIT 1
            )
            UPDATE document_derivations AS derivation
            SET status = 'processing', updated_at = NOW()
            FROM target
            WHERE derivation.id = target.id
            RETURNING derivation.id
            """,
            (tenant_id, user_id),
        )
        processing_derivation = cursor.fetchone()
        if processing_derivation is None:
            raise EmbeddingGenerationEvaluationError(
                "non-empty scope had no current derivation for the refresh blocker probe"
            )
        blocked_refresh_id = start_next_embedding_generation_refresh(
            cursor,
            embedding_profile=LOCAL_EMBEDDING_PROFILE.identifier,
            quiet_period_seconds=0,
        )
        cursor.execute(
            """
            UPDATE document_derivations
            SET status = 'ready', updated_at = NOW()
            WHERE id = %s
            """,
            (processing_derivation[0],),
        )
        automatic_generation_id = start_next_embedding_generation_refresh(
            cursor,
            embedding_profile=LOCAL_EMBEDDING_PROFILE.identifier,
            quiet_period_seconds=0,
        )
        if automatic_generation_id is None:
            cursor.execute(
                """
                SELECT active.status, active.source_corpus_revision, corpus.revision,
                       (SELECT COUNT(*) FROM workspace_embedding_generations AS open
                        WHERE open.tenant_id = active.tenant_id
                          AND open.user_id = active.user_id
                          AND open.status IN ('building', 'ready')),
                       (SELECT COUNT(*) FROM document_versions AS version
                        WHERE version.tenant_id = active.tenant_id
                          AND version.user_id = active.user_id
                          AND version.status = 'processing'),
                       (SELECT COUNT(*)
                        FROM document_versions AS version
                        JOIN document_derivations AS derivation
                          ON derivation.document_version_id = version.id
                         AND derivation.id IN (
                              version.current_derivation_id,
                              version.pending_derivation_id
                         )
                        WHERE version.tenant_id = active.tenant_id
                          AND version.user_id = active.user_id
                          AND derivation.status = 'processing'),
                       (SELECT COUNT(*)
                        FROM document_embedding_jobs AS job
                        JOIN document_versions AS version
                          ON version.id = job.document_version_id
                         AND job.derivation_id IN (
                              version.current_derivation_id,
                              version.pending_derivation_id
                         )
                        WHERE job.tenant_id = active.tenant_id
                          AND job.user_id = active.user_id
                          AND job.status IN (
                              'pending', 'publishing', 'published', 'processing'
                          ))
                FROM workspace_embedding_generations AS active
                JOIN workspace_embedding_corpus_revisions AS corpus
                  ON corpus.tenant_id = active.tenant_id
                 AND corpus.user_id = active.user_id
                WHERE active.id = %s
                """,
                (generation_id,),
            )
            refresh_state = cursor.fetchone()
            raise EmbeddingGenerationEvaluationError(
                "revision-lagged active generation did not start an automatic refresh; "
                f"state={refresh_state}"
            )
        cursor.execute(
            """
            SELECT creation_reason, expected_chunk_count, embedded_chunk_count,
                   COUNT(*) FILTER (WHERE candidate.status <> 'embedded'),
                   COUNT(*) FILTER (
                       WHERE candidate.provider_metadata->>'reuse_source'
                             = 'active_generation'
                   ),
                   COUNT(*) FILTER (
                       WHERE candidate.provider_metadata->>'reuse_source'
                             = 'canonical_chunk'
                   )
            FROM workspace_embedding_generations AS generation
            LEFT JOIN chunk_embedding_vectors AS candidate
              ON candidate.generation_id = generation.id
            WHERE generation.id = %s
            GROUP BY generation.creation_reason, generation.expected_chunk_count,
                     generation.embedded_chunk_count
            """,
            (automatic_generation_id,),
        )
        (
            automatic_creation_reason,
            automatic_expected,
            automatic_embedded,
            automatic_provider_candidates,
            automatic_active_reused,
            automatic_canonical_reused,
        ) = cursor.fetchone()
        if not _qualify_eventually(cursor, automatic_generation_id):
            cursor.execute(
                """
                SELECT status, expected_chunk_count, embedded_chunk_count,
                       failed_chunk_count, evaluation_report
                FROM workspace_embedding_generations
                WHERE id = %s
                """,
                (automatic_generation_id,),
            )
            automatic_state = cursor.fetchone()
            raise EmbeddingGenerationEvaluationError(
                "fully reused automatic generation did not qualify; "
                f"state={automatic_state}"
            )
        automatically_activated_id = activate_next_embedding_generation_refresh(
            cursor,
            embedding_profile=LOCAL_EMBEDDING_PROFILE.identifier,
            rollback_window_hours=168,
        )
        automatic_refresh_cutover_counts = _serving_counts(
            cursor,
            [generation_id, automatic_generation_id],
        )
        return {
            "skipped": False,
            "chunk_count": expected,
            "wrong_owner_rejected": wrong_owner_rejected,
            "coverage_complete": embedded == expected and failed == 0,
            "sealed": sealed,
            "activated": activated,
            "post_activation_insert_rejected": post_activation_insert_rejected,
            "serving_membership_complete": serving_count == expected,
            "cutover_serving_counts": cutover_serving_counts,
            "rollback_serving_counts": rollback_serving_counts,
            "serving_query_uses_hnsw": _contains_index_scan(
                plan,
                "idx_chunk_embedding_vectors_local_lexical_v2",
            ),
            "served_generation_bound": bool(served_rows) and all(
                str(row[3]) == generation_id for row in served_rows
            ),
            "active_baseline_retained_after_corpus_change": (
                post_change_serving_count == expected
            ),
            "automatic_refresh_reused_all_vectors": (
                automatic_creation_reason == "corpus_refresh"
                and int(automatic_expected) == expected + 1
                and int(automatic_embedded) == expected + 1
                and int(automatic_active_reused) == expected
                and int(automatic_canonical_reused) == 1
            ),
            "automatic_refresh_chunk_count": int(automatic_expected),
            "automatic_refresh_provider_candidates": int(
                automatic_provider_candidates
            ),
            "automatic_refresh_activated": (
                automatically_activated_id == automatic_generation_id
            ),
            "automatic_refresh_cutover_counts": (
                automatic_refresh_cutover_counts
            ),
            "manual_generation_required_operator_activation": (
                manual_generation_required_operator_activation
            ),
            "automatic_refresh_waited_for_processing": (
                blocked_refresh_id is None
            ),
            "pause_released_inflight_lease": pause_released_inflight_lease,
            "paused_claim_blocked": paused_claim_blocked,
            "resume_reopened_generation": resume_reopened_generation,
            "worker_dispatch_batches": worker_dispatch_batches,
            "attempt_ceiling_failed_generation": attempt_ceiling_failed_generation,
            "integrity_rejected_zero_vectors": integrity_rejected_zero_vectors,
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
                "SELECT filename FROM schema_migrations ORDER BY filename"
            )
            migrations = [str(row[0]) for row in cursor.fetchall()]
            required_migrations = {
                "061_trusted_embedding_generation_promotion.sql",
                "062_embedding_generation_scope_serialization.sql",
                "063_active_generation_delta_serving.sql",
                "064_automatic_embedding_generation_refresh.sql",
                "065_embedding_generation_pause_resume.sql",
            }
            if not required_migrations.issubset(migrations):
                raise EmbeddingGenerationEvaluationError(
                    "embedding generation migrations 061 through 065 are not applied"
                )
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
                "latest_migration": "065_embedding_generation_pause_resume.sql",
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
