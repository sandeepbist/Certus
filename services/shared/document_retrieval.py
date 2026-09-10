from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal, Sequence
from uuid import UUID

from services.shared.embeddings import serving_embedding_profile_sql_literal


DEFAULT_SEMANTIC_CANDIDATE_LIMIT = 20
MAX_SEMANTIC_CANDIDATE_LIMIT = 100
MAX_TEMPORAL_YEARS = 10
MAX_TITLE_FILTERS = 5
MAX_TITLE_LENGTH = 500
TEMPORAL_YEAR_PATTERN = re.compile(r"(?<![\d-])(?:1\d{3}|2\d{3})(?![\d-])")
TemporalAuthority = Literal["effective", "source", "recorded"]
VersionScope = Literal["all", "current", "as_of"]


@dataclass(frozen=True)
class DocumentSemanticQuery:
    sql: str
    params: tuple[Any, ...]


@dataclass(frozen=True)
class DocumentLexicalQueries:
    full_text: DocumentSemanticQuery
    literal_phrase: DocumentSemanticQuery


def build_active_embedding_generation_query(
    *,
    tenant_id: str,
    user_id: str,
    embedding_profile: str,
) -> DocumentSemanticQuery:
    """Lock and return the active compatible generation for one search."""
    embedding_profile_literal = serving_embedding_profile_sql_literal(
        embedding_profile
    )
    return DocumentSemanticQuery(
        sql=f"""
            SELECT id
            FROM workspace_embedding_generations
            WHERE tenant_id = %s
              AND user_id = %s
              AND status = 'active'
              AND embedding_profile = {embedding_profile_literal}
            FOR SHARE
        """,
        params=(tenant_id, user_id),
    )


def _literal_like_pattern(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _normalize_years(years: Sequence[int]) -> tuple[int, ...]:
    normalized = tuple(dict.fromkeys(int(year) for year in years))
    if len(normalized) > MAX_TEMPORAL_YEARS:
        raise ValueError(f"years must contain at most {MAX_TEMPORAL_YEARS} values")
    if any(year < 1000 or year > 2999 for year in normalized):
        raise ValueError("years must contain four-digit values between 1000 and 2999")
    return normalized


def _normalize_titles(titles: Sequence[str]) -> tuple[str, ...]:
    if isinstance(titles, (str, bytes)):
        raise ValueError("titles must be a collection of strings")
    normalized: list[str] = []
    for value in titles:
        if not isinstance(value, str):
            raise ValueError("title filters must be strings")
        title = value.strip().casefold()
        if not title or len(title) > MAX_TITLE_LENGTH:
            raise ValueError(
                f"title filters must contain 1 to {MAX_TITLE_LENGTH} characters"
            )
        if title not in normalized:
            normalized.append(title)
        if len(normalized) > MAX_TITLE_FILTERS:
            raise ValueError(f"a query can filter at most {MAX_TITLE_FILTERS} titles")
    return tuple(normalized)


def _normalize_year_bounds(
    year_start: int | None,
    year_end: int | None,
) -> tuple[int | None, int | None]:
    normalized_start = int(year_start) if year_start is not None else None
    normalized_end = int(year_end) if year_end is not None else None
    for value in (normalized_start, normalized_end):
        if value is not None and not 1000 <= value <= 2999:
            raise ValueError("year bounds must be between 1000 and 2999")
    if (
        normalized_start is not None
        and normalized_end is not None
        and normalized_start > normalized_end
    ):
        raise ValueError("year_start must not be later than year_end")
    return normalized_start, normalized_end


def _normalize_time_bounds(
    time_start: datetime | None,
    time_end: datetime | None,
) -> tuple[datetime | None, datetime | None]:
    for value, name in ((time_start, "time_start"), (time_end, "time_end")):
        if value is not None and (
            not isinstance(value, datetime) or value.tzinfo is None
        ):
            raise ValueError(f"{name} must be a timezone-aware datetime")
    normalized_start = time_start.astimezone(timezone.utc) if time_start else None
    normalized_end = time_end.astimezone(timezone.utc) if time_end else None
    if (
        normalized_start is not None
        and normalized_end is not None
        and normalized_start >= normalized_end
    ):
        raise ValueError("time_start must be earlier than time_end")
    return normalized_start, normalized_end


def extract_temporal_years(text: str) -> tuple[int, ...]:
    return _normalize_years(tuple(int(value) for value in TEMPORAL_YEAR_PATTERN.findall(text)))


def infer_temporal_authority(text: str) -> TemporalAuthority:
    normalized = " ".join(text.casefold().split())
    if any(term in normalized for term in (
        "known to certus", "known in", "recorded in", "uploaded in",
        "added in", "ingested in", "known as of", "recorded as of",
        "uploaded as of", "known before", "known after", "known between",
        "known from", "known on",
        "recorded before", "recorded after", "recorded between",
        "recorded from", "recorded on",
        "uploaded before", "uploaded after", "uploaded between",
        "uploaded from", "uploaded on",
        "when did certus learn",
    )):
        return "recorded"
    if any(term in normalized for term in (
        "source time", "effective in", "occurred in", "happened in",
        "true in", "valid in", "effective as of", "occurred as of",
        "happened as of", "true as of", "valid as of", "effective before",
        "effective after", "effective between", "occurred before",
        "occurred after", "occurred between", "true before", "true after",
        "true between", "true from", "valid before", "valid after",
        "valid between", "valid from", "effective on", "occurred on",
        "happened on", "true on", "valid on",
    )):
        return "source"
    return "effective"


def _temporal_filter_sql(
    years: tuple[int, ...],
    year_start: int | None,
    year_end: int | None,
    time_start: datetime | None,
    time_end: datetime | None,
    authority: TemporalAuthority,
    indentation: str,
) -> str:
    if authority not in {"effective", "source", "recorded"}:
        raise ValueError("temporal_authority must be effective, source, or recorded")
    modes = sum((
        bool(years),
        year_start is not None or year_end is not None,
        time_start is not None or time_end is not None,
    ))
    if modes > 1:
        raise ValueError("exact years, year bounds, and time bounds are mutually exclusive")
    if modes == 0:
        return ""
    expression = _temporal_expression(authority, "version")
    if years:
        return (
            f"\n{indentation}AND EXTRACT(YEAR FROM {expression})::integer "
            "= ANY(%s::integer[])"
        )
    if time_start is not None or time_end is not None:
        clauses = []
        if time_start is not None:
            clauses.append(f"\n{indentation}AND {expression} >= %s::timestamptz")
        if time_end is not None:
            clauses.append(f"\n{indentation}AND {expression} < %s::timestamptz")
        return "".join(clauses)
    clauses = []
    if year_start is not None:
        clauses.append(f"\n{indentation}AND {expression} >= %s::timestamptz")
    if year_end is not None:
        clauses.append(f"\n{indentation}AND {expression} < %s::timestamptz")
    return "".join(clauses)


def _temporal_expression(authority: TemporalAuthority, alias: str) -> str:
    if authority not in {"effective", "source", "recorded"}:
        raise ValueError("temporal_authority must be effective, source, or recorded")
    return {
        "effective": f"COALESCE({alias}.source_time, {alias}.recorded_at)",
        "source": f"{alias}.source_time",
        "recorded": f"{alias}.recorded_at",
    }[authority]


def _version_filter_sql(
    scope: VersionScope,
    years: tuple[int, ...],
    as_of_cutoff: datetime | None,
    authority: TemporalAuthority,
    indentation: str,
) -> str:
    if scope == "all":
        return ""
    if scope == "current":
        return f"\n{indentation}AND d.current_version_id = version.id"
    if scope != "as_of":
        raise ValueError("version_scope must be all, current, or as_of")
    if as_of_cutoff is None and len(years) != 1:
        raise ValueError("as_of version scope requires exactly one cutoff")
    if as_of_cutoff is not None and years:
        raise ValueError("as_of version scope accepts one date or one year, not both")
    expression = _temporal_expression(authority, "candidate")
    return f"""
{indentation}AND version.id = (
{indentation}  SELECT candidate.id
{indentation}  FROM document_versions AS candidate
{indentation}  WHERE candidate.document_id = d.id
{indentation}    AND candidate.tenant_id = version.tenant_id
{indentation}    AND candidate.user_id = version.user_id
{indentation}    AND candidate.status = 'ready'
{indentation}    AND candidate.current_derivation_id IS NOT NULL
{indentation}    AND {expression} < %s::timestamptz
{indentation}  ORDER BY {expression} DESC,
{indentation}           candidate.version_number DESC, candidate.id DESC
{indentation}  LIMIT 1
{indentation})"""


def _temporal_params(
    years: tuple[int, ...],
    year_start: int | None,
    year_end: int | None,
    time_start: datetime | None,
    time_end: datetime | None,
    version_scope: VersionScope,
) -> tuple[Any, ...]:
    if version_scope == "as_of":
        cutoff = (
            time_end
            if time_end is not None
            else datetime(years[0] + 1, 1, 1, tzinfo=timezone.utc)
        )
        return (cutoff,)
    if years:
        return ([*years],)
    if time_start is not None or time_end is not None:
        return tuple(
            value for value in (time_start, time_end) if value is not None
        )
    params: list[datetime] = []
    if year_start is not None:
        params.append(datetime(year_start, 1, 1, tzinfo=timezone.utc))
    if year_end is not None:
        params.append(datetime(year_end + 1, 1, 1, tzinfo=timezone.utc))
    return tuple(params)


def build_document_lexical_queries(
    *,
    query: str,
    tenant_id: str,
    user_id: str,
    document_ids: Sequence[str] = (),
    titles: Sequence[str] = (),
    years: Sequence[int] = (),
    year_start: int | None = None,
    year_end: int | None = None,
    time_start: datetime | None = None,
    time_end: datetime | None = None,
    temporal_authority: TemporalAuthority = "effective",
    version_scope: VersionScope = "all",
    candidate_limit: int = DEFAULT_SEMANTIC_CANDIDATE_LIMIT,
) -> DocumentLexicalQueries:
    if not query.strip():
        raise ValueError("query must not be empty")
    if not 1 <= candidate_limit <= MAX_SEMANTIC_CANDIDATE_LIMIT:
        raise ValueError(
            f"candidate_limit must be between 1 and {MAX_SEMANTIC_CANDIDATE_LIMIT}"
        )
    normalized_document_ids = tuple(document_ids)
    normalized_titles = _normalize_titles(titles)
    normalized_years = _normalize_years(years)
    normalized_year_start, normalized_year_end = _normalize_year_bounds(
        year_start, year_end
    )
    normalized_time_start, normalized_time_end = _normalize_time_bounds(
        time_start, time_end
    )
    if version_scope == "as_of" and (
        normalized_year_start is not None
        or normalized_year_end is not None
        or normalized_time_start is not None
    ):
        raise ValueError("as_of version scope requires one date or one year cutoff")
    document_filter_sql = (
        "\n          AND c.document_id = ANY(%s::uuid[])"
        if normalized_document_ids
        else ""
    )
    title_filter_sql = (
        "\n          AND LOWER(BTRIM(version.title)) = ANY(%s::text[])"
        if normalized_titles else ""
    )
    temporal_filter_sql = (
        "" if version_scope == "as_of"
        else _temporal_filter_sql(
            normalized_years,
            normalized_year_start,
            normalized_year_end,
            normalized_time_start,
            normalized_time_end,
            temporal_authority,
            "          ",
        )
    )
    version_filter_sql = _version_filter_sql(
        version_scope,
        normalized_years,
        normalized_time_end,
        temporal_authority,
        "          ",
    )
    columns = """
        c.id AS chunk_id, c.document_id, c.document_version_id,
        version.version_number, version.title AS doc_title,
        version.content_hash, version.source_time, version.recorded_at,
        (d.current_version_id = version.id) AS is_current_version,
        c.derivation_id, derivation.input_parsed_artifact_id,
        c.content, c.page_number, c.section_title,
        c.start_char, c.end_char, c.text_locator_status,
        c.text_locator_profile
    """
    joins_and_scope = f"""
        FROM chunks c
        JOIN documents d ON c.document_id = d.id
        JOIN document_versions AS version
          ON version.id = c.document_version_id
         AND version.document_id = c.document_id
        JOIN document_derivations AS derivation
          ON derivation.id = c.derivation_id
         AND derivation.document_version_id = c.document_version_id
         AND derivation.document_id = c.document_id
         AND derivation.tenant_id = c.tenant_id
         AND derivation.user_id = c.user_id
        WHERE c.tenant_id = %s AND c.user_id = %s
          AND d.tenant_id = %s AND d.user_id = %s
          AND version.tenant_id = %s AND version.user_id = %s
          {document_filter_sql}
          {title_filter_sql}
          {temporal_filter_sql}
          {version_filter_sql}
          AND d.deleted_at IS NULL
          AND version.status = 'ready'
          AND c.derivation_id = version.current_derivation_id
    """
    scope_params: tuple[Any, ...] = (
        tenant_id,
        user_id,
        tenant_id,
        user_id,
        tenant_id,
        user_id,
        *([list(normalized_document_ids)] if normalized_document_ids else []),
        *([list(normalized_titles)] if normalized_titles else []),
        *_temporal_params(
            normalized_years,
            normalized_year_start,
            normalized_year_end,
            normalized_time_start,
            normalized_time_end,
            version_scope,
        ),
    )
    full_text = DocumentSemanticQuery(
        sql=f"""
            SELECT {columns},
                   ts_rank_cd(c.search_vector, websearch_to_tsquery('english', %s))
                     AS fts_rank
            {joins_and_scope}
              AND c.search_vector @@ websearch_to_tsquery('english', %s)
            ORDER BY fts_rank DESC, c.id ASC
            LIMIT %s
        """,
        params=(query, *scope_params, query, candidate_limit),
    )
    literal_phrase = DocumentSemanticQuery(
        sql=f"""
            SELECT {columns}, 0.0::real AS fts_rank
            {joins_and_scope}
              AND c.content ILIKE %s ESCAPE '\\'
            ORDER BY c.id ASC
            LIMIT %s
        """,
        params=(*scope_params, _literal_like_pattern(query), candidate_limit),
    )
    return DocumentLexicalQueries(
        full_text=full_text,
        literal_phrase=literal_phrase,
    )


def build_document_semantic_query(
    *,
    vector_literal: str,
    tenant_id: str,
    user_id: str,
    embedding_profile: str,
    minimum_similarity: float,
    document_ids: Sequence[str] = (),
    titles: Sequence[str] = (),
    years: Sequence[int] = (),
    year_start: int | None = None,
    year_end: int | None = None,
    time_start: datetime | None = None,
    time_end: datetime | None = None,
    temporal_authority: TemporalAuthority = "effective",
    version_scope: VersionScope = "all",
    candidate_limit: int = DEFAULT_SEMANTIC_CANDIDATE_LIMIT,
    embedding_generation_id: str | None = None,
) -> DocumentSemanticQuery:
    """Build the one production semantic-query shape used by serving and evals."""
    if not math.isfinite(minimum_similarity) or not -1.0 <= minimum_similarity <= 1.0:
        raise ValueError("minimum_similarity must be a finite value between -1 and 1")
    if not 1 <= candidate_limit <= MAX_SEMANTIC_CANDIDATE_LIMIT:
        raise ValueError(
            f"candidate_limit must be between 1 and {MAX_SEMANTIC_CANDIDATE_LIMIT}"
        )

    normalized_document_ids = tuple(document_ids)
    normalized_titles = _normalize_titles(titles)
    normalized_years = _normalize_years(years)
    normalized_year_start, normalized_year_end = _normalize_year_bounds(
        year_start, year_end
    )
    normalized_time_start, normalized_time_end = _normalize_time_bounds(
        time_start, time_end
    )
    embedding_profile_literal = serving_embedding_profile_sql_literal(
        embedding_profile
    )
    normalized_generation_id: str | None = None
    if embedding_generation_id is not None:
        try:
            normalized_generation_id = str(UUID(str(embedding_generation_id)))
        except (TypeError, ValueError, AttributeError) as error:
            raise ValueError("embedding_generation_id must be a valid UUID") from error
    if version_scope == "as_of" and (
        normalized_year_start is not None
        or normalized_year_end is not None
        or normalized_time_start is not None
    ):
        raise ValueError("as_of version scope requires one date or one year cutoff")
    document_filter_sql = (
        "\n          AND c.document_id = ANY(%s::uuid[])"
        if normalized_document_ids
        else ""
    )
    title_filter_sql = (
        "\n              AND LOWER(BTRIM(version.title)) = ANY(%s::text[])"
        if normalized_titles else ""
    )
    temporal_filter_sql = (
        "" if version_scope == "as_of"
        else _temporal_filter_sql(
            normalized_years,
            normalized_year_start,
            normalized_year_end,
            normalized_time_start,
            normalized_time_end,
            temporal_authority,
            "              ",
        )
    )
    version_filter_sql = _version_filter_sql(
        version_scope,
        normalized_years,
        normalized_time_end,
        temporal_authority,
        "              ",
    )

    if normalized_generation_id is None:
        embedding_generation_select = "NULL::uuid AS embedding_generation_id,"
        vector_expression = "c.embedding"
        vector_source_sql = "FROM chunks c"
        vector_filter_sql = f"""
              AND c.embedding IS NOT NULL
              AND c.embedding_profile = {embedding_profile_literal}
        """
        generation_params: tuple[Any, ...] = ()
    else:
        embedding_generation_select = (
            "candidate_vector.generation_id AS embedding_generation_id,"
        )
        vector_expression = "candidate_vector.embedding"
        vector_source_sql = f"""
            FROM chunk_embedding_vectors AS candidate_vector
            JOIN workspace_embedding_generations AS embedding_generation
              ON embedding_generation.id = candidate_vector.generation_id
             AND embedding_generation.tenant_id = candidate_vector.tenant_id
             AND embedding_generation.user_id = candidate_vector.user_id
             AND embedding_generation.status = 'active'
             AND embedding_generation.embedding_profile = {embedding_profile_literal}
            JOIN chunks c
              ON c.id = candidate_vector.chunk_id
             AND c.tenant_id = candidate_vector.tenant_id
             AND c.user_id = candidate_vector.user_id
        """
        vector_filter_sql = f"""
              AND candidate_vector.generation_id = %s::uuid
              AND candidate_vector.status = 'embedded'
              AND candidate_vector.is_serving = true
              AND candidate_vector.embedding IS NOT NULL
              AND candidate_vector.embedding_profile = {embedding_profile_literal}
        """
        generation_params = (normalized_generation_id,)

    # pgvector applies approximate-index filtering after its index scan. Keep
    # authorization and relational eligibility inside the nearest-neighbor CTE,
    # but apply the distance threshold outside it so the HNSW scan remains an
    # ordered nearest-neighbor query. `+ 0` preserves ordering through the
    # materialized CTE on PostgreSQL 17 and newer.
    sql = f"""
        WITH nearest AS MATERIALIZED (
            SELECT c.id AS chunk_id, c.document_id, c.document_version_id,
                   {embedding_generation_select}
                   version.version_number, version.title AS doc_title,
                   version.content_hash, version.source_time, version.recorded_at,
                   (d.current_version_id = version.id) AS is_current_version,
                   c.derivation_id, derivation.input_parsed_artifact_id,
                   c.content, c.page_number, c.section_title,
                   c.start_char, c.end_char, c.text_locator_status,
                   c.text_locator_profile,
                   {vector_expression} <=> %s::vector AS vector_distance
            {vector_source_sql}
            JOIN documents d ON c.document_id = d.id
            JOIN document_versions AS version
              ON version.id = c.document_version_id
             AND version.document_id = c.document_id
            JOIN document_derivations AS derivation
              ON derivation.id = c.derivation_id
             AND derivation.document_version_id = c.document_version_id
             AND derivation.document_id = c.document_id
             AND derivation.tenant_id = c.tenant_id
             AND derivation.user_id = c.user_id
            WHERE c.tenant_id = %s AND c.user_id = %s
              AND d.tenant_id = %s AND d.user_id = %s
              AND version.tenant_id = %s AND version.user_id = %s
              {document_filter_sql}
              {title_filter_sql}
              {temporal_filter_sql}
              {version_filter_sql}
              AND d.deleted_at IS NULL
              AND version.status = 'ready'
              AND c.derivation_id = version.current_derivation_id
              {vector_filter_sql}
            ORDER BY {vector_expression} <=> %s::vector ASC
            LIMIT %s
        )
        SELECT chunk_id, document_id, document_version_id,
               embedding_generation_id, version_number,
               doc_title, content_hash, source_time, recorded_at,
               is_current_version, derivation_id, input_parsed_artifact_id,
               content, page_number, section_title, start_char, end_char,
               text_locator_status, text_locator_profile,
               (1.0 - vector_distance) AS vector_similarity
        FROM nearest
        WHERE vector_distance <= %s
        ORDER BY vector_distance + 0 ASC
    """

    params: tuple[Any, ...] = (
        vector_literal,
        tenant_id,
        user_id,
        tenant_id,
        user_id,
        tenant_id,
        user_id,
        *([list(normalized_document_ids)] if normalized_document_ids else []),
        *([list(normalized_titles)] if normalized_titles else []),
        *_temporal_params(
            normalized_years,
            normalized_year_start,
            normalized_year_end,
            normalized_time_start,
            normalized_time_end,
            version_scope,
        ),
        *generation_params,
        vector_literal,
        candidate_limit,
        1.0 - minimum_similarity,
    )
    return DocumentSemanticQuery(sql=sql, params=params)
