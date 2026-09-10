from __future__ import annotations

import json
import math
import random
import statistics
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Sequence

import psycopg2
from psycopg2.extras import execute_values

from evals.retrieval import _build_chunks, load_dataset
from services.shared.embeddings import LOCAL_EMBEDDING_PROFILE, local_lexical_embedding
from services.shared.retrieval import reciprocal_rank_fusion
from services.shared.pgvector_policy import (
    HNSW_EF_SEARCH,
    HNSW_MAX_SCAN_TUPLES,
    HNSW_SCAN_MEM_MULTIPLIER,
    configure_filtered_hnsw,
)
from services.shared.document_retrieval import (
    build_document_lexical_queries,
    build_document_semantic_query,
    extract_temporal_years,
    infer_temporal_authority,
)


VECTOR_DIMENSIONS = 1536
DEFAULT_EVALUATION_LIMIT = 20
MINIMUM_PGVECTOR_VERSION = (0, 8, 0)
DEFAULT_MINIMUM_RECALL = 0.98
SEED_MANIFEST = "evals/datasets/certus_seed_v1/manifest.json"


class PgvectorEvaluationError(RuntimeError):
    """The filtered-ANN evaluation could not produce trustworthy evidence."""


@dataclass(frozen=True)
class Scope:
    tenant_id: str
    user_id: str
    embedding_profile: str


@dataclass(frozen=True)
class QueryCase:
    case_id: str
    scope: Scope
    vector: tuple[float, ...]
    requested_count: int = DEFAULT_EVALUATION_LIMIT
    tag: str | None = None
    recorded_day_min: int | None = None
    recorded_day_max: int | None = None


@dataclass(frozen=True)
class Candidate:
    vector: tuple[float, ...]
    tag: str
    recorded_day: int


@dataclass(frozen=True)
class ProductionQueryCase:
    case_id: str
    scope: Scope
    vector: tuple[float, ...]
    requested_count: int
    document_ids: tuple[str, ...] = ()
    titles: tuple[str, ...] = ()
    years: tuple[int, ...] = ()
    year_start: int | None = None
    year_end: int | None = None
    time_start: datetime | None = None
    time_end: datetime | None = None
    temporal_authority: str = "effective"
    version_scope: str = "all"


def recall_at_k(
    exact_ranking: Sequence[int],
    approximate_ranking: Sequence[int],
    cutoff: int,
) -> float:
    if cutoff <= 0:
        raise ValueError("Recall cutoff must be positive")
    expected = set(exact_ranking[:cutoff])
    if not expected:
        return 1.0
    observed = set(approximate_ranking[:cutoff])
    return len(expected & observed) / len(expected)


def result_is_complete(
    approximate_ranking: Sequence[int],
    *,
    eligible_count: int,
    requested_count: int,
) -> bool:
    if eligible_count < 0 or requested_count <= 0:
        raise ValueError("Eligible and requested counts must be valid")
    return len(approximate_ranking) == min(eligible_count, requested_count)


def _normalized_vector(rng: random.Random) -> tuple[float, ...]:
    values = [rng.gauss(0.0, 1.0) for _ in range(VECTOR_DIMENSIONS)]
    norm = math.sqrt(sum(value * value for value in values))
    return tuple(value / norm for value in values)


def _vector_literal(vector: Sequence[float]) -> str:
    if len(vector) != VECTOR_DIMENSIONS:
        raise PgvectorEvaluationError(
            f"Expected {VECTOR_DIMENSIONS} dimensions, got {len(vector)}"
        )
    return "[" + ",".join(f"{value:.9g}" for value in vector) + "]"


def _version_tuple(raw_version: str) -> tuple[int, int, int]:
    parts = raw_version.split(".")
    try:
        values = tuple(int(part) for part in parts[:3])
    except ValueError as error:
        raise PgvectorEvaluationError(
            f"Unrecognized pgvector version: {raw_version}"
        ) from error
    return (values + (0, 0, 0))[:3]


def _contains_index_scan(plan: Any, index_name: str) -> bool:
    if isinstance(plan, dict):
        if plan.get("Index Name") == index_name:
            return True
        return any(_contains_index_scan(value, index_name) for value in plan.values())
    if isinstance(plan, list):
        return any(_contains_index_scan(value, index_name) for value in plan)
    return False


def _build_rows_and_cases() -> tuple[list[tuple[Any, ...]], list[QueryCase]]:
    rng = random.Random(20260830)
    rows: list[tuple[Any, ...]] = []
    candidates_by_scope: dict[Scope, list[Candidate]] = {}
    next_id = 1

    def add_rows(
        count: int,
        scope: Scope,
        *,
        ready: bool = True,
        current: bool = True,
    ) -> None:
        nonlocal next_id
        scoped_candidates = candidates_by_scope.setdefault(scope, [])
        for offset in range(count):
            vector = _normalized_vector(rng)
            tag = "rare" if offset % 17 == 0 else "common"
            recorded_day = offset % 365
            rows.append((
                next_id,
                scope.tenant_id,
                scope.user_id,
                scope.embedding_profile,
                ready,
                current,
                tag,
                recorded_day,
                _vector_literal(vector),
            ))
            if ready and current:
                scoped_candidates.append(Candidate(vector, tag, recorded_day))
            next_id += 1

    target = Scope(
        "tenant-alpha", "user-alpha", LOCAL_EMBEDDING_PROFILE.identifier
    )
    other_user = Scope(
        "tenant-alpha", "user-beta", LOCAL_EMBEDDING_PROFILE.identifier
    )
    noisy_tenant = Scope(
        "tenant-noisy-a", "user-noisy", LOCAL_EMBEDDING_PROFILE.identifier
    )
    noisy_tenant_b = Scope(
        "tenant-noisy-b", "user-noisy", LOCAL_EMBEDDING_PROFILE.identifier
    )
    noisy_tenant_c = Scope(
        "tenant-noisy-c", "user-noisy", LOCAL_EMBEDDING_PROFILE.identifier
    )
    sparse_tenant = Scope(
        "tenant-sparse", "user-sparse", LOCAL_EMBEDDING_PROFILE.identifier
    )
    wrong_profile = Scope(
        "tenant-alpha",
        "user-alpha",
        "embedding-space:v1:openai:text-embedding-3-small:1536",
    )

    add_rows(160, target)
    add_rows(40, target, ready=False)
    add_rows(40, target, current=False)
    add_rows(40, wrong_profile)
    add_rows(160, other_user)
    add_rows(900, noisy_tenant)
    add_rows(900, noisy_tenant_b)
    add_rows(900, noisy_tenant_c)
    add_rows(30, sparse_tenant)

    def candidate(scope: Scope, offset: int) -> Candidate:
        return candidates_by_scope[scope][offset]

    cases = [
        QueryCase("target-broad-k20", target, candidate(target, 0).vector),
        QueryCase(
            "target-rare-tag-k20", target, candidate(target, 34).vector,
            tag="rare",
        ),
        QueryCase(
            "target-date-window-k20", target, candidate(target, 119).vector,
            recorded_day_min=115, recorded_day_max=125,
        ),
        QueryCase(
            "other-user-k5", other_user, candidate(other_user, 8).vector,
            requested_count=5,
        ),
        QueryCase(
            "large-tenant-k50", noisy_tenant, candidate(noisy_tenant, 511).vector,
            requested_count=50,
        ),
        QueryCase(
            "sparse-among-noisy-k20", sparse_tenant,
            candidate(sparse_tenant, 12).vector,
        ),
    ]
    return rows, cases


def _configure_exact(cursor: Any) -> None:
    cursor.execute("SET LOCAL enable_indexscan = off")
    cursor.execute("SET LOCAL enable_bitmapscan = off")
    cursor.execute("SET LOCAL enable_seqscan = on")


def _configure_approximate(cursor: Any) -> None:
    cursor.execute("SET LOCAL enable_indexscan = on")
    cursor.execute("SET LOCAL enable_bitmapscan = on")
    cursor.execute("SET LOCAL enable_seqscan = off")
    configure_filtered_hnsw(cursor)


RANKING_SQL = """
    SELECT id
    FROM certus_pgvector_recall
    WHERE tenant_id = %s
      AND user_id = %s
      AND embedding_profile = %s
      AND is_ready
      AND is_current
      AND (%s::text IS NULL OR tag = %s)
      AND (%s::integer IS NULL OR recorded_day >= %s)
      AND (%s::integer IS NULL OR recorded_day <= %s)
    ORDER BY embedding <=> %s::vector
    LIMIT %s
"""


def _ranking_params(case: QueryCase) -> tuple[Any, ...]:
    return (
        case.scope.tenant_id,
        case.scope.user_id,
        case.scope.embedding_profile,
        case.tag,
        case.tag,
        case.recorded_day_min,
        case.recorded_day_min,
        case.recorded_day_max,
        case.recorded_day_max,
        _vector_literal(case.vector),
        case.requested_count,
    )


def _ranking(cursor: Any, case: QueryCase, *, approximate: bool) -> tuple[list[int], float]:
    if approximate:
        _configure_approximate(cursor)
    else:
        _configure_exact(cursor)
    started = time.perf_counter()
    cursor.execute(
        RANKING_SQL,
        _ranking_params(case),
    )
    ranking = [int(row[0]) for row in cursor.fetchall()]
    return ranking, (time.perf_counter() - started) * 1000


def _eligible_count_for_case(cursor: Any, case: QueryCase) -> int:
    cursor.execute(
        """
        SELECT COUNT(*)
        FROM certus_pgvector_recall
        WHERE tenant_id = %s
          AND user_id = %s
          AND embedding_profile = %s
          AND is_ready
          AND is_current
          AND (%s::text IS NULL OR tag = %s)
          AND (%s::integer IS NULL OR recorded_day >= %s)
          AND (%s::integer IS NULL OR recorded_day <= %s)
        """,
        (
            case.scope.tenant_id,
            case.scope.user_id,
            case.scope.embedding_profile,
            case.tag,
            case.tag,
            case.recorded_day_min,
            case.recorded_day_min,
            case.recorded_day_max,
            case.recorded_day_max,
        ),
    )
    return int(cursor.fetchone()[0])


def _explain_profile(cursor: Any, case: QueryCase) -> dict[str, Any]:
    _configure_approximate(cursor)
    cursor.execute(
        "EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, FORMAT JSON) " + RANKING_SQL,
        _ranking_params(case),
    )
    payload = cursor.fetchone()[0]
    document = payload[0] if isinstance(payload, list) else payload
    plan = document["Plan"]
    return {
        "uses_hnsw": _contains_index_scan(document, "certus_pgvector_recall_hnsw"),
        "actual_rows": int(plan.get("Actual Rows", 0)),
        "planning_time_ms": round(float(document.get("Planning Time", 0.0)), 6),
        "execution_time_ms": round(float(document.get("Execution Time", 0.0)), 6),
        "buffers": {
            key: int(plan.get(label, 0))
            for key, label in (
                ("shared_hit", "Shared Hit Blocks"),
                ("shared_read", "Shared Read Blocks"),
                ("local_hit", "Local Hit Blocks"),
                ("local_read", "Local Read Blocks"),
                ("temp_read", "Temp Read Blocks"),
                ("temp_written", "Temp Written Blocks"),
            )
        },
    }


def _fixture_uuid(kind: str, identifier: int) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"certus-pgvector-eval:{kind}:{identifier}"))


def _create_joined_production_fixture(
    cursor: Any,
    rows: Sequence[tuple[Any, ...]],
) -> None:
    cursor.execute("""
        CREATE TEMP TABLE documents (
            id UUID PRIMARY KEY,
            tenant_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            current_version_id UUID NOT NULL,
            deleted_at TIMESTAMPTZ
        ) ON COMMIT DROP;
        CREATE TEMP TABLE document_versions (
            id UUID PRIMARY KEY,
            document_id UUID NOT NULL,
            tenant_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            version_number INTEGER NOT NULL,
            title TEXT,
            content_hash TEXT NOT NULL,
            source_time TIMESTAMPTZ,
            recorded_at TIMESTAMPTZ NOT NULL,
            status TEXT NOT NULL,
            current_derivation_id UUID NOT NULL
        ) ON COMMIT DROP;
        CREATE TEMP TABLE document_derivations (
            id UUID PRIMARY KEY,
            document_version_id UUID NOT NULL,
            document_id UUID NOT NULL,
            tenant_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            input_parsed_artifact_id UUID NOT NULL
        ) ON COMMIT DROP;
        CREATE TEMP TABLE chunks (
            id INTEGER PRIMARY KEY,
            document_id UUID NOT NULL,
            document_version_id UUID NOT NULL,
            derivation_id UUID NOT NULL,
            tenant_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            content TEXT NOT NULL,
            page_number INTEGER,
            section_title TEXT,
            start_char INTEGER,
            end_char INTEGER,
            text_locator_status TEXT NOT NULL,
            text_locator_profile TEXT NOT NULL,
            embedding_profile TEXT NOT NULL,
            embedding vector(1536),
            search_vector tsvector GENERATED ALWAYS AS (
                to_tsvector('english', content)
            ) STORED
        ) ON COMMIT DROP
    """)

    documents: list[tuple[Any, ...]] = []
    versions: list[tuple[Any, ...]] = []
    derivations: list[tuple[Any, ...]] = []
    chunks: list[tuple[Any, ...]] = []
    for row in rows:
        (
            identifier,
            tenant_id,
            user_id,
            embedding_profile,
            is_ready,
            is_current,
            _tag,
            recorded_day,
            embedding,
        ) = row
        document_id = _fixture_uuid("document", int(identifier))
        version_id = _fixture_uuid("version", int(identifier))
        derivation_id = _fixture_uuid("derivation", int(identifier))
        active_derivation_id = (
            derivation_id
            if is_current
            else _fixture_uuid("active-derivation", int(identifier))
        )
        current_version_id = (
            _fixture_uuid("current-version", int(identifier))
            if int(identifier) == 1
            else version_id
        )
        documents.append((document_id, tenant_id, user_id, current_version_id))
        versions.append((
            version_id,
            document_id,
            tenant_id,
            user_id,
            1,
            f"Evaluation document {identifier}",
            f"{int(identifier):064x}",
            "2024-06-01T00:00:00+00:00" if int(identifier) == 1 else None,
            int(recorded_day),
            "ready" if is_ready else "processing",
            active_derivation_id,
        ))
        derivations.append((
            derivation_id,
            version_id,
            document_id,
            tenant_id,
            user_id,
            _fixture_uuid("artifact", int(identifier)),
        ))
        content = f"Evaluation chunk {identifier}"
        if int(identifier) in {1, 161, 201, 281, 441}:
            content = "Fiscal invariant literal 100%_complete evidence"
            if int(identifier) == 1:
                content += " historicalsnapshot"
        elif int(identifier) == 2:
            content = "Literal decoy 100xxcomplete evidence"
        chunks.append((
            int(identifier),
            document_id,
            version_id,
            derivation_id,
            tenant_id,
            user_id,
            content,
            embedding_profile,
            embedding,
        ))

    target_row = next(row for row in rows if int(row[0]) == 1)
    as_of_version_id = _fixture_uuid("as-of-version", 1)
    as_of_derivation_id = _fixture_uuid("as-of-derivation", 1)
    versions.append((
        as_of_version_id,
        _fixture_uuid("document", 1),
        target_row[1],
        target_row[2],
        2,
        "Evaluation document 1 newer version",
        "f" * 64,
        "2025-06-01T00:00:00+00:00",
        366,
        "ready",
        as_of_derivation_id,
    ))
    derivations.append((
        as_of_derivation_id,
        as_of_version_id,
        _fixture_uuid("document", 1),
        target_row[1],
        target_row[2],
        _fixture_uuid("as-of-artifact", 1),
    ))
    chunks.append((
        -1,
        _fixture_uuid("document", 1),
        as_of_version_id,
        as_of_derivation_id,
        target_row[1],
        target_row[2],
        "Newer as-of version decoy historicalsnapshot",
        target_row[3],
        target_row[8],
    ))

    execute_values(
        cursor,
        """
        INSERT INTO documents (id, tenant_id, user_id, current_version_id)
        VALUES %s
        """,
        documents,
        page_size=250,
    )
    execute_values(
        cursor,
        """
        INSERT INTO document_versions (
            id, document_id, tenant_id, user_id, version_number, title,
            content_hash, source_time, recorded_at, status, current_derivation_id
        ) VALUES %s
        """,
        versions,
        template=(
            "(%s, %s, %s, %s, %s, %s, %s, %s::timestamptz, "
            "TIMESTAMPTZ '2026-01-01 00:00:00+00' + (%s * INTERVAL '1 day'), %s, %s)"
        ),
        page_size=250,
    )
    execute_values(
        cursor,
        """
        INSERT INTO document_derivations (
            id, document_version_id, document_id, tenant_id, user_id,
            input_parsed_artifact_id
        ) VALUES %s
        """,
        derivations,
        page_size=250,
    )
    execute_values(
        cursor,
        """
        INSERT INTO chunks (
            id, document_id, document_version_id, derivation_id,
            tenant_id, user_id, content, text_locator_status,
            text_locator_profile, embedding_profile, embedding
        ) VALUES %s
        """,
        chunks,
        template=(
            "(%s, %s, %s, %s, %s, %s, %s, 'exact', "
            "'unicode_code_point:zero_based_half_open:v1', %s, %s::vector)"
        ),
        page_size=50,
    )
    cursor.execute("""
        CREATE INDEX certus_joined_chunks_hnsw_local
        ON chunks USING hnsw (embedding vector_cosine_ops)
        WITH (m = 16, ef_construction = 200)
        WHERE embedding IS NOT NULL
          AND embedding_profile = 'embedding-space:v1:local:local-lexical-v2:1536'
    """)
    cursor.execute("""
        CREATE INDEX certus_joined_chunks_fts
        ON chunks USING gin (search_vector)
    """)
    cursor.execute("ANALYZE documents")
    cursor.execute("ANALYZE document_versions")
    cursor.execute("ANALYZE document_derivations")
    cursor.execute("ANALYZE chunks")


def _run_joined_production_cases(
    cursor: Any,
    rows: Sequence[tuple[Any, ...]],
    cases: Sequence[QueryCase],
) -> list[dict[str, Any]]:
    _create_joined_production_fixture(cursor, rows)
    production_cases = [
        ProductionQueryCase(
            case_id=case.case_id,
            scope=case.scope,
            vector=case.vector,
            requested_count=case.requested_count,
        )
        for case in cases
        if case.tag is None and case.recorded_day_min is None
    ]
    target_case = cases[0]
    production_cases.append(ProductionQueryCase(
        case_id="target-explicit-document",
        scope=target_case.scope,
        vector=target_case.vector,
        requested_count=20,
        document_ids=(_fixture_uuid("document", 1),),
    ))
    production_cases.append(ProductionQueryCase(
        case_id="target-explicit-title",
        scope=target_case.scope,
        vector=target_case.vector,
        requested_count=20,
        titles=("EVALUATION DOCUMENT 1",),
    ))
    production_cases.extend((
        ProductionQueryCase(
            case_id="target-source-year-2024",
            scope=target_case.scope,
            vector=target_case.vector,
            requested_count=20,
            document_ids=(_fixture_uuid("document", 1),),
            years=(2024,),
            temporal_authority="source",
        ),
        ProductionQueryCase(
            case_id="target-recorded-authority-2026",
            scope=target_case.scope,
            vector=target_case.vector,
            requested_count=20,
            document_ids=(_fixture_uuid("document", 1),),
            years=(2026,),
            temporal_authority="recorded",
        ),
        ProductionQueryCase(
            case_id="target-recorded-year-fallback-2026",
            scope=target_case.scope,
            vector=target_case.vector,
            requested_count=20,
            years=(2026,),
        ),
        ProductionQueryCase(
            case_id="target-current-version-only",
            scope=target_case.scope,
            vector=target_case.vector,
            requested_count=20,
            version_scope="current",
        ),
        ProductionQueryCase(
            case_id="target-source-as-of-2024",
            scope=target_case.scope,
            vector=target_case.vector,
            requested_count=20,
            document_ids=(_fixture_uuid("document", 1),),
            years=(2024,),
            temporal_authority="source",
            version_scope="as_of",
        ),
        ProductionQueryCase(
            case_id="target-recorded-as-of-2026",
            scope=target_case.scope,
            vector=target_case.vector,
            requested_count=20,
            document_ids=(_fixture_uuid("document", 1),),
            years=(2026,),
            temporal_authority="recorded",
            version_scope="as_of",
        ),
        ProductionQueryCase(
            case_id="target-source-before-2025",
            scope=target_case.scope,
            vector=target_case.vector,
            requested_count=20,
            document_ids=(_fixture_uuid("document", 1),),
            year_end=2024,
            temporal_authority="source",
        ),
        ProductionQueryCase(
            case_id="target-source-after-2024",
            scope=target_case.scope,
            vector=target_case.vector,
            requested_count=20,
            document_ids=(_fixture_uuid("document", 1),),
            year_start=2025,
            temporal_authority="source",
        ),
        ProductionQueryCase(
            case_id="target-source-between-2024-2025",
            scope=target_case.scope,
            vector=target_case.vector,
            requested_count=20,
            document_ids=(_fixture_uuid("document", 1),),
            year_start=2024,
            year_end=2025,
            temporal_authority="source",
        ),
        ProductionQueryCase(
            case_id="target-source-on-2024-06-01",
            scope=target_case.scope,
            vector=target_case.vector,
            requested_count=20,
            document_ids=(_fixture_uuid("document", 1),),
            time_start=datetime(2024, 6, 1, tzinfo=timezone.utc),
            time_end=datetime(2024, 6, 2, tzinfo=timezone.utc),
            temporal_authority="source",
        ),
        ProductionQueryCase(
            case_id="target-source-after-2024-06-01",
            scope=target_case.scope,
            vector=target_case.vector,
            requested_count=20,
            document_ids=(_fixture_uuid("document", 1),),
            time_start=datetime(2024, 6, 2, tzinfo=timezone.utc),
            temporal_authority="source",
        ),
        ProductionQueryCase(
            case_id="target-source-between-dates",
            scope=target_case.scope,
            vector=target_case.vector,
            requested_count=20,
            document_ids=(_fixture_uuid("document", 1),),
            time_start=datetime(2024, 6, 1, tzinfo=timezone.utc),
            time_end=datetime(2025, 6, 2, tzinfo=timezone.utc),
            temporal_authority="source",
        ),
        ProductionQueryCase(
            case_id="target-source-as-of-2024-06-01",
            scope=target_case.scope,
            vector=target_case.vector,
            requested_count=20,
            document_ids=(_fixture_uuid("document", 1),),
            time_end=datetime(2024, 6, 2, tzinfo=timezone.utc),
            temporal_authority="source",
            version_scope="as_of",
        ),
        ProductionQueryCase(
            case_id="target-source-at-offset-instant",
            scope=target_case.scope,
            vector=target_case.vector,
            requested_count=20,
            document_ids=(_fixture_uuid("document", 1),),
            time_start=datetime(2024, 6, 1, tzinfo=timezone.utc),
            time_end=(
                datetime(2024, 6, 1, tzinfo=timezone.utc)
                + timedelta(microseconds=1)
            ),
            temporal_authority="source",
        ),
        ProductionQueryCase(
            case_id="target-source-after-instant",
            scope=target_case.scope,
            vector=target_case.vector,
            requested_count=20,
            document_ids=(_fixture_uuid("document", 1),),
            time_start=(
                datetime(2024, 6, 1, tzinfo=timezone.utc)
                + timedelta(microseconds=1)
            ),
            temporal_authority="source",
        ),
        ProductionQueryCase(
            case_id="target-source-between-instants",
            scope=target_case.scope,
            vector=target_case.vector,
            requested_count=20,
            document_ids=(_fixture_uuid("document", 1),),
            time_start=datetime(2024, 6, 1, tzinfo=timezone.utc),
            time_end=(
                datetime(2025, 6, 1, tzinfo=timezone.utc)
                + timedelta(microseconds=1)
            ),
            temporal_authority="source",
        ),
        ProductionQueryCase(
            case_id="target-source-as-of-instant",
            scope=target_case.scope,
            vector=target_case.vector,
            requested_count=20,
            document_ids=(_fixture_uuid("document", 1),),
            time_end=(
                datetime(2024, 6, 1, tzinfo=timezone.utc)
                + timedelta(microseconds=1)
            ),
            temporal_authority="source",
            version_scope="as_of",
        ),
    ))

    reports: list[dict[str, Any]] = []
    for case in production_cases:
        query = build_document_semantic_query(
            vector_literal=_vector_literal(case.vector),
            tenant_id=case.scope.tenant_id,
            user_id=case.scope.user_id,
            embedding_profile=case.scope.embedding_profile,
            minimum_similarity=-1.0,
            document_ids=case.document_ids,
            titles=case.titles,
            candidate_limit=case.requested_count,
            years=case.years,
            year_start=case.year_start,
            year_end=case.year_end,
            time_start=case.time_start,
            time_end=case.time_end,
            temporal_authority=case.temporal_authority,
            version_scope=case.version_scope,
        )
        _configure_exact(cursor)
        cursor.execute(query.sql, query.params)
        exact = [int(row[0]) for row in cursor.fetchall()]

        _configure_approximate(cursor)
        cursor.execute(query.sql, query.params)
        approximate = [int(row[0]) for row in cursor.fetchall()]

        _configure_approximate(cursor)
        cursor.execute(
            "EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, FORMAT JSON) " + query.sql,
            query.params,
        )
        payload = cursor.fetchone()[0]
        document = payload[0] if isinstance(payload, list) else payload
        reports.append({
            "case_id": case.case_id,
            "requested_count": case.requested_count,
            "exact_ranking": exact,
            "ann_ranking": approximate,
            "recall_at_20": round(recall_at_k(exact, approximate, 20), 6),
            "complete": len(approximate) == len(exact),
            "eligible_count": len(exact),
            "document_filter_count": len(case.document_ids),
            "title_filter_count": len(case.titles),
            "temporal_years": list(case.years),
            "temporal_year_start": case.year_start,
            "temporal_year_end": case.year_end,
            "temporal_time_start": (
                case.time_start.isoformat() if case.time_start else None
            ),
            "temporal_time_end": (
                case.time_end.isoformat() if case.time_end else None
            ),
            "temporal_authority": case.temporal_authority,
            "version_scope": case.version_scope,
            "excluded_historical_probe": (
                1 not in exact and -1 not in exact
                if case.version_scope == "current" else None
            ),
            "selected_as_of_probe": (
                exact == [1] if case.version_scope == "as_of" else None
            ),
            "selected_year_bound_probe": (
                exact == [1] if case.case_id == "target-source-before-2025"
                else exact == [-1] if case.case_id == "target-source-after-2024"
                else set(exact) == {-1, 1}
                if case.case_id == "target-source-between-2024-2025"
                else None
            ),
            "selected_date_probe": (
                exact == [1]
                if case.case_id in {
                    "target-source-on-2024-06-01",
                    "target-source-as-of-2024-06-01",
                }
                else exact == [-1]
                if case.case_id == "target-source-after-2024-06-01"
                else set(exact) == {-1, 1}
                if case.case_id == "target-source-between-dates"
                else None
            ),
            "selected_instant_probe": (
                exact == [1]
                if case.case_id in {
                    "target-source-at-offset-instant",
                    "target-source-as-of-instant",
                }
                else exact == [-1]
                if case.case_id == "target-source-after-instant"
                else set(exact) == {-1, 1}
                if case.case_id == "target-source-between-instants"
                else None
            ),
            "selected_title_probe": (
                exact == [1] if case.case_id == "target-explicit-title" else None
            ),
            "ann_plan": {
                "uses_hnsw": _contains_index_scan(
                    document,
                    "certus_joined_chunks_hnsw_local",
                ),
                "planning_time_ms": round(
                    float(document.get("Planning Time", 0.0)),
                    6,
                ),
                "execution_time_ms": round(
                    float(document.get("Execution Time", 0.0)),
                    6,
                ),
            },
        })
    return reports


def _run_joined_lexical_case(cursor: Any, target_scope: Scope) -> dict[str, Any]:
    queries = build_document_lexical_queries(
        query="fiscal invariant",
        tenant_id=target_scope.tenant_id,
        user_id=target_scope.user_id,
    )
    cursor.execute(queries.full_text.sql, queries.full_text.params)
    full_text = [int(row[0]) for row in cursor.fetchall()]

    literal_queries = build_document_lexical_queries(
        query="100%_complete",
        tenant_id=target_scope.tenant_id,
        user_id=target_scope.user_id,
    )
    cursor.execute(
        literal_queries.literal_phrase.sql,
        literal_queries.literal_phrase.params,
    )
    literal_phrase = [int(row[0]) for row in cursor.fetchall()]

    as_of_queries = build_document_lexical_queries(
        query="historicalsnapshot",
        tenant_id=target_scope.tenant_id,
        user_id=target_scope.user_id,
        document_ids=(_fixture_uuid("document", 1),),
        years=(2024,),
        temporal_authority="source",
        version_scope="as_of",
    )
    cursor.execute(as_of_queries.full_text.sql, as_of_queries.full_text.params)
    as_of_full_text = [int(row[0]) for row in cursor.fetchall()]
    cursor.execute(
        as_of_queries.literal_phrase.sql,
        as_of_queries.literal_phrase.params,
    )
    as_of_literal_phrase = [int(row[0]) for row in cursor.fetchall()]

    bounded_queries = build_document_lexical_queries(
        query="historicalsnapshot",
        tenant_id=target_scope.tenant_id,
        user_id=target_scope.user_id,
        document_ids=(_fixture_uuid("document", 1),),
        year_start=2025,
        temporal_authority="source",
    )
    cursor.execute(bounded_queries.full_text.sql, bounded_queries.full_text.params)
    bounded_full_text = [int(row[0]) for row in cursor.fetchall()]
    cursor.execute(
        bounded_queries.literal_phrase.sql,
        bounded_queries.literal_phrase.params,
    )
    bounded_literal_phrase = [int(row[0]) for row in cursor.fetchall()]

    dated_queries = build_document_lexical_queries(
        query="historicalsnapshot",
        tenant_id=target_scope.tenant_id,
        user_id=target_scope.user_id,
        document_ids=(_fixture_uuid("document", 1),),
        time_start=datetime(2024, 6, 1, tzinfo=timezone.utc),
        time_end=datetime(2024, 6, 2, tzinfo=timezone.utc),
        temporal_authority="source",
    )
    cursor.execute(dated_queries.full_text.sql, dated_queries.full_text.params)
    dated_full_text = [int(row[0]) for row in cursor.fetchall()]
    cursor.execute(
        dated_queries.literal_phrase.sql,
        dated_queries.literal_phrase.params,
    )
    dated_literal_phrase = [int(row[0]) for row in cursor.fetchall()]

    instant_queries = build_document_lexical_queries(
        query="historicalsnapshot",
        tenant_id=target_scope.tenant_id,
        user_id=target_scope.user_id,
        document_ids=(_fixture_uuid("document", 1),),
        time_start=datetime(2024, 6, 1, tzinfo=timezone.utc),
        time_end=(
            datetime(2024, 6, 1, tzinfo=timezone.utc)
            + timedelta(microseconds=1)
        ),
        temporal_authority="source",
    )
    cursor.execute(instant_queries.full_text.sql, instant_queries.full_text.params)
    instant_full_text = [int(row[0]) for row in cursor.fetchall()]
    cursor.execute(
        instant_queries.literal_phrase.sql,
        instant_queries.literal_phrase.params,
    )
    instant_literal_phrase = [int(row[0]) for row in cursor.fetchall()]

    title_queries = build_document_lexical_queries(
        query="historicalsnapshot",
        tenant_id=target_scope.tenant_id,
        user_id=target_scope.user_id,
        titles=("EVALUATION DOCUMENT 1 NEWER VERSION",),
    )
    cursor.execute(title_queries.full_text.sql, title_queries.full_text.params)
    title_full_text = [int(row[0]) for row in cursor.fetchall()]
    cursor.execute(
        title_queries.literal_phrase.sql,
        title_queries.literal_phrase.params,
    )
    title_literal_phrase = [int(row[0]) for row in cursor.fetchall()]

    cursor.execute("SET LOCAL enable_seqscan = off")
    cursor.execute(
        "EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, FORMAT JSON) "
        + queries.full_text.sql,
        queries.full_text.params,
    )
    payload = cursor.fetchone()[0]
    document = payload[0] if isinstance(payload, list) else payload
    return {
        "case_id": "joined-fts-and-literal-phrase",
        "full_text_ranking": full_text,
        "literal_phrase_ranking": literal_phrase,
        "as_of_full_text_ranking": as_of_full_text,
        "as_of_literal_phrase_ranking": as_of_literal_phrase,
        "bounded_full_text_ranking": bounded_full_text,
        "bounded_literal_phrase_ranking": bounded_literal_phrase,
        "dated_full_text_ranking": dated_full_text,
        "dated_literal_phrase_ranking": dated_literal_phrase,
        "instant_full_text_ranking": instant_full_text,
        "instant_literal_phrase_ranking": instant_literal_phrase,
        "title_full_text_ranking": title_full_text,
        "title_literal_phrase_ranking": title_literal_phrase,
        "expected_ranking": [1],
        "scope_correct": (
            full_text == [1]
            and literal_phrase == [1]
            and as_of_full_text == [1]
            and as_of_literal_phrase == [1]
            and bounded_full_text == [-1]
            and bounded_literal_phrase == [-1]
            and dated_full_text == [1]
            and dated_literal_phrase == [1]
            and instant_full_text == [1]
            and instant_literal_phrase == [1]
            and title_full_text == [-1]
            and title_literal_phrase == [-1]
        ),
        "fts_plan": {
            "uses_gin": _contains_index_scan(
                document,
                "certus_joined_chunks_fts",
            ),
            "planning_time_ms": round(float(document.get("Planning Time", 0.0)), 6),
            "execution_time_ms": round(float(document.get("Execution Time", 0.0)), 6),
        },
    }


def _run_seed_corpus_through_production(cursor: Any) -> dict[str, Any]:
    dataset = load_dataset(SEED_MANIFEST)
    corpus_chunks = _build_chunks(dataset)
    document_ids: dict[str, str] = {}
    versions: dict[str, str] = {}
    derivations: dict[str, str] = {}
    for offset, document in enumerate(dataset.documents, start=20_000):
        document_ids[document.document_id] = _fixture_uuid("corpus-document", offset)
        versions[document.document_id] = _fixture_uuid("corpus-version", offset)
        derivations[document.document_id] = _fixture_uuid("corpus-derivation", offset)

    tenant_id = "tenant-corpus"
    user_id = "user-corpus"
    execute_values(cursor, """
        INSERT INTO documents (id, tenant_id, user_id, current_version_id)
        VALUES %s
    """, [
        (document_ids[item.document_id], tenant_id, user_id, versions[item.document_id])
        for item in dataset.documents
    ])
    execute_values(cursor, """
        INSERT INTO document_versions (
            id, document_id, tenant_id, user_id, version_number, title,
            content_hash, source_time, recorded_at, status, current_derivation_id
        ) VALUES %s
    """, [
        (
            versions[item.document_id], document_ids[item.document_id], tenant_id,
            user_id, 1, item.title, item.sha256, item.source_time,
            item.recorded_time, "ready", derivations[item.document_id],
        )
        for item in dataset.documents
    ])
    execute_values(cursor, """
        INSERT INTO document_derivations (
            id, document_version_id, document_id, tenant_id, user_id,
            input_parsed_artifact_id
        ) VALUES %s
    """, [
        (
            derivations[item.document_id], versions[item.document_id],
            document_ids[item.document_id], tenant_id, user_id,
            _fixture_uuid("corpus-artifact", offset),
        )
        for offset, item in enumerate(dataset.documents, start=20_000)
    ])

    integer_to_chunk: dict[int, str] = {}
    chunk_rows = []
    for identifier, chunk in enumerate(corpus_chunks, start=30_000):
        integer_to_chunk[identifier] = chunk.chunk_id
        chunk_rows.append((
            identifier,
            document_ids[chunk.document_id],
            versions[chunk.document_id],
            derivations[chunk.document_id],
            tenant_id,
            user_id,
            chunk.content,
            LOCAL_EMBEDDING_PROFILE.identifier,
            _vector_literal(chunk.vector),
        ))
    execute_values(cursor, """
        INSERT INTO chunks (
            id, document_id, document_version_id, derivation_id,
            tenant_id, user_id, content, text_locator_status,
            text_locator_profile, embedding_profile, embedding
        ) VALUES %s
    """, chunk_rows, template=(
        "(%s, %s, %s, %s, %s, %s, %s, 'exact', "
        "'unicode_code_point:zero_based_half_open:v1', %s, %s::vector)"
    ), page_size=50)
    cursor.execute("ANALYZE chunks")

    reports = []
    recall_at_5_values: list[float] = []
    reciprocal_ranks: list[float] = []
    conflict_complete: list[float] = []
    no_answer_empty: list[float] = []
    for query_spec in dataset.queries:
        temporal_years = extract_temporal_years(query_spec.query)
        temporal_authority = infer_temporal_authority(query_spec.query)
        semantic = build_document_semantic_query(
            vector_literal=_vector_literal(local_lexical_embedding(query_spec.query)),
            tenant_id=tenant_id,
            user_id=user_id,
            embedding_profile=LOCAL_EMBEDDING_PROFILE.identifier,
            minimum_similarity=float(dataset.retrieval["dense_min_similarity"]),
            candidate_limit=int(dataset.retrieval["candidate_limit"]),
            years=temporal_years,
            temporal_authority=temporal_authority,
        )
        _configure_approximate(cursor)
        cursor.execute(semantic.sql, semantic.params)
        dense = [integer_to_chunk[int(row[0])] for row in cursor.fetchall()]
        lexical = build_document_lexical_queries(
            query=query_spec.query,
            tenant_id=tenant_id,
            user_id=user_id,
            candidate_limit=int(dataset.retrieval["candidate_limit"]),
            years=temporal_years,
            temporal_authority=temporal_authority,
        )
        cursor.execute(lexical.full_text.sql, lexical.full_text.params)
        keyword = [integer_to_chunk[int(row[0])] for row in cursor.fetchall()]
        cursor.execute(lexical.literal_phrase.sql, lexical.literal_phrase.params)
        seen = set(keyword)
        keyword.extend(
            chunk_id for chunk_id in (
                integer_to_chunk[int(row[0])] for row in cursor.fetchall()
            ) if chunk_id not in seen
        )
        fused = [item for item, _score in reciprocal_rank_fusion(
            [dense, keyword], rank_constant=float(dataset.retrieval["rrf_k"])
        )][:int(dataset.retrieval["candidate_limit"])]
        evidence_sets = [
            {
                chunk.chunk_id for chunk in corpus_chunks
                if chunk.document_id == evidence.document_id
                and evidence.quote in chunk.content
            }
            for evidence in query_spec.evidence
        ]
        relevant = set().union(*evidence_sets) if evidence_sets else set()
        if query_spec.answerable:
            recall = sum(bool(set(fused[:5]) & items) for items in evidence_sets) / len(evidence_sets)
            first_rank = next((rank for rank, item in enumerate(fused[:20], 1) if item in relevant), None)
            recall_at_5_values.append(recall)
            reciprocal_ranks.append(1.0 / first_rank if first_rank else 0.0)
            if query_spec.expected_answer_status == "conflicting_evidence":
                conflict_complete.append(float(recall == 1.0))
        else:
            recall = None
            no_answer_empty.append(float(not fused))
        reports.append({
            "query_id": query_spec.query_id,
            "expected_answer_status": query_spec.expected_answer_status,
            "temporal_years": list(temporal_years),
            "temporal_authority": temporal_authority,
            "dense_ranking": dense,
            "lexical_ranking": keyword,
            "hybrid_rrf_ranking": fused,
            "evidence_recall_at_5": recall,
        })
    return {
        "dataset_id": dataset.dataset_id,
        "dataset_version": dataset.dataset_version,
        "manifest_sha256": dataset.manifest_sha256,
        "chunk_count": len(corpus_chunks),
        "query_count": len(dataset.queries),
        "evidence_recall_at_5": round(_mean(recall_at_5_values), 6),
        "mrr_at_20": round(_mean(reciprocal_ranks), 6),
        "conflict_complete_at_5": round(_mean(conflict_complete), 6),
        "no_answer_empty_rate": round(_mean(no_answer_empty), 6),
        "queries": reports,
    }


def _mean(values: Iterable[float]) -> float:
    materialized = list(values)
    return statistics.fmean(materialized) if materialized else 0.0


def run_pgvector_evaluation(database_url: str) -> dict[str, Any]:
    rows, cases = _build_rows_and_cases()
    connection = psycopg2.connect(
        database_url,
        application_name="certus-pgvector-recall-eval",
    )
    connection.autocommit = False
    try:
        with connection.cursor() as cursor:
            cursor.execute("SET LOCAL statement_timeout = '60s'")
            cursor.execute("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
            version_row = cursor.fetchone()
            if not version_row:
                raise PgvectorEvaluationError("The pgvector extension is not installed")
            pgvector_version = str(version_row[0])
            if _version_tuple(pgvector_version) < MINIMUM_PGVECTOR_VERSION:
                raise PgvectorEvaluationError(
                    "Filtered iterative scans require pgvector 0.8.0 or newer"
                )

            cursor.execute("""
                CREATE TEMP TABLE certus_pgvector_recall (
                    id INTEGER PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    embedding_profile TEXT NOT NULL,
                    is_ready BOOLEAN NOT NULL,
                    is_current BOOLEAN NOT NULL,
                    tag TEXT NOT NULL,
                    recorded_day INTEGER NOT NULL,
                    embedding vector(1536) NOT NULL
                ) ON COMMIT DROP
            """)
            execute_values(
                cursor,
                """
                INSERT INTO certus_pgvector_recall (
                    id, tenant_id, user_id, embedding_profile,
                    is_ready, is_current, tag, recorded_day, embedding
                ) VALUES %s
                """,
                rows,
                template="(%s, %s, %s, %s, %s, %s, %s, %s, %s::vector)",
                page_size=50,
            )
            cursor.execute("""
                CREATE INDEX certus_pgvector_recall_hnsw
                ON certus_pgvector_recall
                USING hnsw (embedding vector_cosine_ops)
                WITH (m = 16, ef_construction = 200)
            """)
            cursor.execute("ANALYZE certus_pgvector_recall")

            reports: list[dict[str, Any]] = []
            for case in cases:
                exact, exact_ms = _ranking(cursor, case, approximate=False)
                approximate, approximate_ms = _ranking(
                    cursor,
                    case,
                    approximate=True,
                )
                eligible_count = _eligible_count_for_case(cursor, case)
                explain = _explain_profile(cursor, case)
                reports.append({
                    "case_id": case.case_id,
                    "scope": {
                        "tenant_id": case.scope.tenant_id,
                        "user_id": case.scope.user_id,
                        "embedding_profile": case.scope.embedding_profile,
                    },
                    "filters": {
                        "tag": case.tag,
                        "recorded_day_min": case.recorded_day_min,
                        "recorded_day_max": case.recorded_day_max,
                    },
                    "eligible_count": eligible_count,
                    "eligible_fraction": round(eligible_count / len(rows), 8),
                    "requested_count": case.requested_count,
                    "exact_ranking": exact,
                    "ann_ranking": approximate,
                    "recall_at_1": round(recall_at_k(exact, approximate, 1), 6),
                    "recall_at_5": round(recall_at_k(exact, approximate, 5), 6),
                    "recall_at_20": round(recall_at_k(exact, approximate, 20), 6),
                    "complete": result_is_complete(
                        approximate,
                        eligible_count=eligible_count,
                        requested_count=case.requested_count,
                    ),
                    "exact_latency_ms": round(exact_ms, 6),
                    "ann_latency_ms": round(approximate_ms, 6),
                    "ann_plan": explain,
                })

            aggregate = {
                f"mean_recall_at_{cutoff}": round(_mean(
                    float(report[f"recall_at_{cutoff}"])
                    for report in reports
                ), 6)
                for cutoff in (1, 5, 20)
            }
            aggregate["minimum_recall_at_20"] = min(
                float(report["recall_at_20"]) for report in reports
            )
            aggregate["complete_case_rate"] = round(_mean(
                1.0 if report["complete"] else 0.0 for report in reports
            ), 6)
            aggregate["hnsw_plan_case_rate"] = round(_mean(
                1.0 if report["ann_plan"]["uses_hnsw"] else 0.0
                for report in reports
            ), 6)
            aggregate["mean_exact_latency_ms"] = round(_mean(
                float(report["exact_latency_ms"]) for report in reports
            ), 6)
            aggregate["mean_ann_latency_ms"] = round(_mean(
                float(report["ann_latency_ms"]) for report in reports
            ), 6)

            production_query_reports = _run_joined_production_cases(
                cursor,
                rows,
                cases,
            )
            lexical_query_report = _run_joined_lexical_case(cursor, cases[0].scope)
            seed_corpus_report = _run_seed_corpus_through_production(cursor)
            aggregate["production_query_minimum_recall_at_20"] = min(
                float(report["recall_at_20"])
                for report in production_query_reports
            )
            aggregate["production_query_complete_case_rate"] = round(_mean(
                1.0 if report["complete"] else 0.0
                for report in production_query_reports
            ), 6)
            aggregate["production_query_hnsw_plan_case_rate"] = round(_mean(
                1.0 if report["ann_plan"]["uses_hnsw"] else 0.0
                for report in production_query_reports
            ), 6)

            return {
                "schema_version": 12,
                "run": {
                    "runner_id": "certus-filtered-pgvector-recall-v12",
                    "provider_calls": 0,
                    "persistent_rows_written": 0,
                    "random_seed": 20260830,
                    "pgvector_version": pgvector_version,
                    "dimensions": VECTOR_DIMENSIONS,
                    "row_count": len(rows),
                    "case_count": len(cases),
                    "hnsw": {
                        "profile_isolation": "partial_hnsw_per_supported_profile",
                        "iterative_scan": "strict_order",
                        "m": 16,
                        "ef_construction": 200,
                        "ef_search": HNSW_EF_SEARCH,
                        "max_scan_tuples": HNSW_MAX_SCAN_TUPLES,
                        "scan_mem_multiplier": HNSW_SCAN_MEM_MULTIPLIER,
                    },
                },
                "aggregate": aggregate,
                "cases": reports,
                "production_query_cases": production_query_reports,
                "lexical_query_cases": [lexical_query_report],
                "seed_corpus": seed_corpus_report,
            }
    finally:
        connection.rollback()
        connection.close()


def check_pgvector_report(
    report: dict[str, Any],
    minimum_recall: float = DEFAULT_MINIMUM_RECALL,
) -> list[str]:
    failures: list[str] = []
    for case in report.get("cases", []):
        recall = float(case["recall_at_20"])
        if recall < minimum_recall:
            failures.append(
                f"{case['case_id']} recall@20 {recall:.3f} < {minimum_recall:.3f}"
            )
        if not case["complete"]:
            failures.append(f"{case['case_id']} returned an incomplete ANN result")
        if not case.get("ann_plan", {}).get("uses_hnsw", False):
            failures.append(f"{case['case_id']} did not use the HNSW index")
        if int(case.get("eligible_count", 0)) <= 0:
            failures.append(f"{case['case_id']} had no eligible exact-oracle rows")
    if not report.get("cases"):
        failures.append("No pgvector evaluation cases were executed")
    production_cases = report.get("production_query_cases", [])
    for case in production_cases:
        recall = float(case["recall_at_20"])
        if recall < minimum_recall:
            failures.append(
                f"production query {case['case_id']} recall@20 "
                f"{recall:.3f} < {minimum_recall:.3f}"
            )
        if not case["complete"]:
            failures.append(
                f"production query {case['case_id']} returned an incomplete ANN result"
            )
        if not case.get("ann_plan", {}).get("uses_hnsw", False):
            failures.append(
                f"production query {case['case_id']} did not use the HNSW index"
            )
        if int(case.get("eligible_count", 0)) <= 0:
            failures.append(
                f"production query {case['case_id']} had no eligible exact-oracle rows"
            )
    if not production_cases:
        failures.append("No joined production-query cases were executed")
    if int(report.get("schema_version", 0)) >= 4:
        case_ids = {str(case.get("case_id")) for case in production_cases}
        required_temporal_cases = {
            "target-source-year-2024",
            "target-recorded-year-fallback-2026",
        }
        if not required_temporal_cases.issubset(case_ids):
            failures.append("Joined production queries did not cover both temporal authorities")
    if int(report.get("schema_version", 0)) >= 5:
        by_id = {str(case.get("case_id")): case for case in production_cases}
        recorded_case = by_id.get("target-recorded-authority-2026", {})
        if recorded_case.get("temporal_authority") != "recorded":
            failures.append("Joined production queries did not prove recorded-time authority")
    if int(report.get("schema_version", 0)) >= 6:
        by_id = {str(case.get("case_id")): case for case in production_cases}
        current_case = by_id.get("target-current-version-only", {})
        if (
            current_case.get("version_scope") != "current"
            or current_case.get("excluded_historical_probe") is not True
        ):
            failures.append("Joined production queries did not prove current-version scope")
    if int(report.get("schema_version", 0)) >= 7:
        by_id = {str(case.get("case_id")): case for case in production_cases}
        for case_id, authority in (
            ("target-source-as-of-2024", "source"),
            ("target-recorded-as-of-2026", "recorded"),
        ):
            case = by_id.get(case_id, {})
            if (
                case.get("version_scope") != "as_of"
                or case.get("temporal_authority") != authority
                or case.get("selected_as_of_probe") is not True
            ):
                failures.append(
                    f"Joined production queries did not prove {authority}-time as-of scope"
                )
    if int(report.get("schema_version", 0)) >= 8:
        by_id = {str(case.get("case_id")): case for case in production_cases}
        for case_id in (
            "target-source-before-2025",
            "target-source-after-2024",
            "target-source-between-2024-2025",
        ):
            case = by_id.get(case_id, {})
            if (
                case.get("temporal_authority") != "source"
                or case.get("selected_year_bound_probe") is not True
            ):
                failures.append(
                    f"Joined production queries did not prove bounded year scope: {case_id}"
                )
    if int(report.get("schema_version", 0)) >= 9:
        by_id = {str(case.get("case_id")): case for case in production_cases}
        for case_id in (
            "target-source-on-2024-06-01",
            "target-source-after-2024-06-01",
            "target-source-between-dates",
            "target-source-as-of-2024-06-01",
        ):
            case = by_id.get(case_id, {})
            if (
                case.get("temporal_authority") != "source"
                or case.get("selected_date_probe") is not True
            ):
                failures.append(
                    f"Joined production queries did not prove ISO-date scope: {case_id}"
                )
    if int(report.get("schema_version", 0)) >= 10:
        by_id = {str(case.get("case_id")): case for case in production_cases}
        title_case = by_id.get("target-explicit-title", {})
        if (
            title_case.get("title_filter_count") != 1
            or title_case.get("selected_title_probe") is not True
        ):
            failures.append("Joined production queries did not prove exact title scope")
    if int(report.get("schema_version", 0)) >= 11:
        by_id = {str(case.get("case_id")): case for case in production_cases}
        for case_id in (
            "target-source-at-offset-instant",
            "target-source-after-instant",
            "target-source-between-instants",
            "target-source-as-of-instant",
        ):
            case = by_id.get(case_id, {})
            if (
                case.get("temporal_authority") != "source"
                or case.get("selected_instant_probe") is not True
            ):
                failures.append(
                    f"Joined production queries did not prove exact instant scope: {case_id}"
                )
    if int(report.get("schema_version", 0)) >= 12:
        if (
            report.get("run", {}).get("hnsw", {}).get("profile_isolation")
            != "partial_hnsw_per_supported_profile"
        ):
            failures.append("Production HNSW profile isolation was not declared")
    lexical_cases = report.get("lexical_query_cases", [])
    for case in lexical_cases:
        if not case.get("scope_correct", False):
            failures.append(
                f"lexical query {case['case_id']} returned incorrect scoped evidence"
            )
        if not case.get("fts_plan", {}).get("uses_gin", False):
            failures.append(f"lexical query {case['case_id']} did not use the GIN index")
    if not lexical_cases:
        failures.append("No joined lexical-query cases were executed")
    seed_corpus = report.get("seed_corpus")
    if int(report.get("schema_version", 0)) >= 3 and not seed_corpus:
        failures.append("The checksum-bound seed corpus was not executed")
    elif seed_corpus:
        if float(seed_corpus.get("evidence_recall_at_5", 0.0)) < 1.0:
            failures.append("Production RRF seed evidence Recall@5 fell below 1.0")
        if float(seed_corpus.get("mrr_at_20", 0.0)) < 0.95:
            failures.append("Production RRF seed MRR@20 fell below 0.95")
        if float(seed_corpus.get("conflict_complete_at_5", 0.0)) < 1.0:
            failures.append("Production RRF seed conflict completeness fell below 1.0")
    return failures


def report_json(report: dict[str, Any]) -> str:
    return json.dumps(report, indent=2, sort_keys=True) + "\n"
