import os
import sys
import logging
import math
from datetime import datetime, timezone
from pathlib import Path
from psycopg2.extras import RealDictCursor
from typing import Any, Dict, Iterable, List, Optional
from uuid import UUID

from app.core.db import get_db_connection
from app.core.runtime import (
    OPENAI_EMBEDDING_MAX_RETRIES,
    OPENAI_EMBEDDING_TIMEOUT_SECONDS,
    RETRIEVAL_STATEMENT_TIMEOUT_MS,
    get_openai_client,
)
from app.pricing import estimate_openai_embedding_cost

MODULE_PATH = Path(__file__).resolve()
REPO_ROOT = next(
    (parent for parent in MODULE_PATH.parents if (parent / "services" / "shared").exists()),
    MODULE_PATH.parents[2],
)
sys.path.insert(0, str(REPO_ROOT))

from services.shared.embeddings import (
    EMBEDDING_DIMENSIONS,
    EmbeddingResult,
    configured_embedding_profile,
    has_usable_openai_api_key,
    local_lexical_embedding,
)
from services.shared.document_retrieval import (
    DEFAULT_SEMANTIC_CANDIDATE_LIMIT,
    MAX_SEMANTIC_CANDIDATE_LIMIT,
    build_document_lexical_queries,
    build_document_semantic_query,
)
from services.shared.retrieval import reciprocal_rank_fusion
from services.shared.pgvector_policy import configure_filtered_hnsw

logger = logging.getLogger("orchestration_retrieval")

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
EMBEDDING_MODEL = os.getenv("OPENAI_EMBEDDING_MODEL", "text-embedding-3-small")
ACTIVE_EMBEDDING_PROFILE = configured_embedding_profile(
    OPENAI_API_KEY,
    EMBEDDING_MODEL,
)
def normalize_min_vector_similarity(value: object) -> float:
    try:
        normalized = float(value)
    except (TypeError, ValueError) as error:
        raise ValueError(
            "RETRIEVAL_MIN_VECTOR_SIMILARITY must be a finite number between -1 and 1"
        ) from error
    if not math.isfinite(normalized) or not -1.0 <= normalized <= 1.0:
        raise ValueError(
            "RETRIEVAL_MIN_VECTOR_SIMILARITY must be a finite number between -1 and 1"
        )
    return normalized


def normalize_result_limit(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("top_k must be an integer between 1 and 100")
    if not 1 <= value <= MAX_SEMANTIC_CANDIDATE_LIMIT:
        raise ValueError("top_k must be an integer between 1 and 100")
    return value


MIN_VECTOR_SIMILARITY = normalize_min_vector_similarity(
    os.getenv("RETRIEVAL_MIN_VECTOR_SIMILARITY", "0.3")
)
MAX_TITLE_FILTERS = 5
MAX_TITLE_LENGTH = 500


def normalize_document_ids(document_ids: Optional[Iterable[str]]) -> tuple[str, ...]:
    """Normalize a bounded explicit UUID filter before it reaches PostgreSQL."""
    if document_ids is None:
        return ()
    if isinstance(document_ids, (str, bytes)):
        raise ValueError("Document IDs must be a collection of UUID strings")

    normalized: list[str] = []
    seen: set[str] = set()
    for value in document_ids:
        try:
            document_id = str(UUID(str(value)))
        except (TypeError, ValueError, AttributeError) as error:
            raise ValueError("Document filters must contain valid UUIDs") from error
        if document_id not in seen:
            normalized.append(document_id)
            seen.add(document_id)
        if len(normalized) > 10:
            raise ValueError("A query can filter at most 10 documents")
    return tuple(normalized)


def normalize_titles(titles: Optional[Iterable[str]]) -> tuple[str, ...]:
    if titles is None:
        return ()
    if isinstance(titles, (str, bytes)):
        raise ValueError("Title filters must be a collection of strings")
    normalized: list[str] = []
    for value in titles:
        if not isinstance(value, str):
            raise ValueError("Title filters must be strings")
        title = value.strip().casefold()
        if not title or len(title) > MAX_TITLE_LENGTH:
            raise ValueError(
                f"Title filters must contain 1 to {MAX_TITLE_LENGTH} characters"
            )
        if title not in normalized:
            normalized.append(title)
        if len(normalized) > MAX_TITLE_FILTERS:
            raise ValueError(f"A query can filter at most {MAX_TITLE_FILTERS} titles")
    return tuple(normalized)


def normalize_temporal_years(years: Optional[Iterable[int | str]]) -> tuple[int, ...]:
    if years is None:
        return ()
    if isinstance(years, (str, bytes)):
        raise ValueError("Temporal years must be a collection")
    normalized: list[int] = []
    for value in years:
        try:
            year = int(value)
        except (TypeError, ValueError) as error:
            raise ValueError("Temporal filters must contain four-digit years") from error
        if year < 1000 or year > 2999:
            raise ValueError("Temporal filters must contain four-digit years")
        if year not in normalized:
            normalized.append(year)
        if len(normalized) > 10:
            raise ValueError("A query can filter at most 10 years")
    return tuple(normalized)


def normalize_temporal_authority(value: str) -> str:
    normalized = str(value).strip().casefold()
    if normalized not in {"effective", "source", "recorded"}:
        raise ValueError("Temporal authority must be effective, source, or recorded")
    return normalized


def normalize_temporal_year_bound(value: int | str | None, name: str) -> int | None:
    if value is None:
        return None
    try:
        normalized = int(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{name} must be a four-digit year") from error
    if normalized < 1000 or normalized > 2999:
        raise ValueError(f"{name} must be a four-digit year")
    return normalized


def normalize_temporal_time_bound(
    value: datetime | str | None,
    name: str,
) -> datetime | None:
    if value is None:
        return None
    try:
        normalized = (
            value
            if isinstance(value, datetime)
            else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        )
    except ValueError as error:
        raise ValueError(f"{name} must be an ISO 8601 timestamp") from error
    if normalized.tzinfo is None:
        raise ValueError(f"{name} must include a timezone")
    return normalized.astimezone(timezone.utc)


def normalize_version_scope(value: str) -> str:
    normalized = str(value).strip().casefold()
    if normalized not in {"all", "current", "as_of"}:
        raise ValueError("Version scope must be all, current, or as_of")
    return normalized


def embed_query(query: str) -> EmbeddingResult:
    if not query.strip():
        raise ValueError("Embedding input cannot be empty")

    if not has_usable_openai_api_key(OPENAI_API_KEY):
        return EmbeddingResult(
            vector=local_lexical_embedding(query),
            profile=ACTIVE_EMBEDDING_PROFILE,
            provider_model=ACTIVE_EMBEDDING_PROFILE.model,
        )

    client = get_openai_client(
        "embedding",
        OPENAI_API_KEY,
        OPENAI_EMBEDDING_TIMEOUT_SECONDS,
        OPENAI_EMBEDDING_MAX_RETRIES,
    )
    response = client.embeddings.create(
        input=[query],
        model=EMBEDDING_MODEL,
        dimensions=EMBEDDING_DIMENSIONS,
        encoding_format="float",
    )
    embedding = response.data[0].embedding
    if len(embedding) != EMBEDDING_DIMENSIONS:
        raise RuntimeError("Embedding provider returned an incompatible vector dimension")
    if response.model != ACTIVE_EMBEDDING_PROFILE.model:
        raise RuntimeError(
            "Embedding provider returned a different model than requested: "
            f"requested={ACTIVE_EMBEDDING_PROFILE.model}, returned={response.model}"
        )
    usage = getattr(response, "usage", None)
    input_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
    cost_estimate = estimate_openai_embedding_cost(
        model=response.model,
        input_tokens=input_tokens,
    )
    return EmbeddingResult(
        vector=embedding,
        profile=ACTIVE_EMBEDDING_PROFILE,
        provider_model=response.model,
        input_tokens=input_tokens,
        estimated_cost_usd=(cost_estimate.amount_usd if cost_estimate else 0.0),
        pricing_profile=(cost_estimate.pricing_profile if cost_estimate else None),
    )


class RetrievedChunk:
    def __init__(
        self,
        chunk_id: str,
        document_id: str,
        document_version_id: str,
        derivation_id: str,
        parsed_artifact_id: str,
        version_number: int,
        document_title: str,
        content: str,
        score: float,
        content_hash: str,
        source_time: Optional[Any] = None,
        recorded_at: Optional[Any] = None,
        is_current_version: bool = False,
        page_number: Optional[int] = None,
        section_title: str = "",
        start_char: Optional[int] = None,
        end_char: Optional[int] = None,
        text_locator_status: str = "unavailable",
        text_locator_profile: str = "legacy_unavailable:v0",
        retrieval_method: str = "hybrid"
    ):
        self.chunk_id = chunk_id
        self.document_id = document_id
        self.document_version_id = document_version_id
        self.derivation_id = derivation_id
        self.parsed_artifact_id = parsed_artifact_id
        self.version_number = version_number
        self.document_title = document_title
        self.content = content
        self.score = score
        self.content_hash = content_hash
        self.source_time = source_time
        self.recorded_at = recorded_at
        self.is_current_version = is_current_version
        self.page_number = page_number
        self.section_title = section_title
        self.start_char = start_char
        self.end_char = end_char
        self.text_locator_status = text_locator_status
        self.text_locator_profile = text_locator_profile
        self.retrieval_method = retrieval_method

    def to_dict(self) -> Dict[str, Any]:
        source_time = (
            self.source_time.isoformat()
            if hasattr(self.source_time, "isoformat")
            else self.source_time
        )
        recorded_at = (
            self.recorded_at.isoformat()
            if hasattr(self.recorded_at, "isoformat")
            else self.recorded_at
        )
        return {
            "chunk_id": self.chunk_id,
            "document_id": self.document_id,
            "document_version_id": self.document_version_id,
            "derivation_id": self.derivation_id,
            "parsed_artifact_id": self.parsed_artifact_id,
            "version_number": self.version_number,
            "document_title": self.document_title,
            "content": self.content,
            "score": round(self.score, 4),
            "content_hash": self.content_hash,
            "source_time": source_time,
            "recorded_at": recorded_at,
            "is_current_version": self.is_current_version,
            "page_number": self.page_number,
            "section_title": self.section_title,
            "start_char": self.start_char,
            "end_char": self.end_char,
            "text_locator_status": self.text_locator_status,
            "text_locator_profile": self.text_locator_profile,
            "retrieval_method": self.retrieval_method,
        }


class HybridSearchEngine:
    @staticmethod
    def search(
        query: str,
        tenant_id: str,
        user_id: str,
        top_k: int = 5,
        query_embedding: Optional[EmbeddingResult] = None,
        allow_embedding_generation: bool = True,
        document_ids: Optional[Iterable[str]] = None,
        years: Optional[Iterable[int | str]] = None,
        temporal_authority: str = "effective",
        version_scope: str = "all",
        year_start: int | str | None = None,
        year_end: int | str | None = None,
        time_start: datetime | str | None = None,
        time_end: datetime | str | None = None,
        titles: Optional[Iterable[str]] = None,
    ) -> List[RetrievedChunk]:
        normalized_top_k = normalize_result_limit(top_k)
        candidate_limit = max(DEFAULT_SEMANTIC_CANDIDATE_LIMIT, normalized_top_k)
        normalized_document_ids = normalize_document_ids(document_ids)
        normalized_titles = normalize_titles(titles)
        normalized_years = normalize_temporal_years(years)
        normalized_temporal_authority = normalize_temporal_authority(
            temporal_authority
        )
        normalized_version_scope = normalize_version_scope(version_scope)
        normalized_year_start = normalize_temporal_year_bound(
            year_start, "year_start"
        )
        normalized_year_end = normalize_temporal_year_bound(year_end, "year_end")
        normalized_time_start = normalize_temporal_time_bound(
            time_start, "time_start"
        )
        normalized_time_end = normalize_temporal_time_bound(time_end, "time_end")
        if (
            normalized_time_start is not None
            and normalized_time_end is not None
            and normalized_time_start >= normalized_time_end
        ):
            raise ValueError("time_start must be earlier than time_end")
        if (
            normalized_year_start is not None
            and normalized_year_end is not None
            and normalized_year_start > normalized_year_end
        ):
            raise ValueError("year_start must not be later than year_end")
        if normalized_years and (
            normalized_year_start is not None or normalized_year_end is not None
        ):
            raise ValueError("Exact years and year bounds are mutually exclusive")
        temporal_modes = sum((
            bool(normalized_years),
            normalized_year_start is not None or normalized_year_end is not None,
            normalized_time_start is not None or normalized_time_end is not None,
        ))
        if temporal_modes > 1:
            raise ValueError(
                "Exact years, year bounds, and time bounds are mutually exclusive"
            )
        if normalized_version_scope == "as_of":
            valid_year_cutoff = len(normalized_years) == 1
            valid_date_cutoff = (
                not normalized_years
                and normalized_time_start is None
                and normalized_time_end is not None
            )
            if (
                not (valid_year_cutoff or valid_date_cutoff)
                or normalized_year_start is not None
                or normalized_year_end is not None
            ):
                raise ValueError("As-of retrieval requires exactly one cutoff")
        with get_db_connection() as connection:
            with connection.cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute(
                    "SELECT set_config('statement_timeout', %s, true)",
                    (f"{RETRIEVAL_STATEMENT_TIMEOUT_MS}ms",),
                )

                # 1. Semantic vector search. If a configured provider is temporarily
                # unavailable, keyword retrieval still returns an honest partial result.
                semantic_results = []
                try:
                    resolved_embedding = query_embedding
                    if resolved_embedding is None and allow_embedding_generation:
                        resolved_embedding = embed_query(query)
                    if resolved_embedding is not None:
                        vec_str = (
                            f"[{','.join(str(v) for v in resolved_embedding.vector)}]"
                        )
                        # Retained versions and derivations make this a heavily filtered ANN
                        # query. pgvector 0.8+ iterative scans continue until enough eligible
                        # candidates are found while preserving exact distance ordering.
                        configure_filtered_hnsw(cursor)
                        semantic_query = build_document_semantic_query(
                            vector_literal=vec_str,
                            tenant_id=tenant_id,
                            user_id=user_id,
                            embedding_profile=resolved_embedding.profile.identifier,
                            minimum_similarity=MIN_VECTOR_SIMILARITY,
                            document_ids=normalized_document_ids,
                            titles=normalized_titles,
                            years=normalized_years,
                            year_start=normalized_year_start,
                            year_end=normalized_year_end,
                            time_start=normalized_time_start,
                            time_end=normalized_time_end,
                            temporal_authority=normalized_temporal_authority,
                            version_scope=normalized_version_scope,
                            candidate_limit=candidate_limit,
                        )
                        cursor.execute(semantic_query.sql, semantic_query.params)
                        semantic_results = cursor.fetchall()
                except Exception as error:
                    connection.rollback()
                    cursor.execute(
                        "SELECT set_config('statement_timeout', %s, true)",
                        (f"{RETRIEVAL_STATEMENT_TIMEOUT_MS}ms",),
                    )
                    logger.warning(
                        "Semantic retrieval unavailable; using keyword search: %s",
                        error,
                    )

                # 2. Keep indexed FTS separate from the bounded literal phrase
                # fallback. An OR with an unindexed ILIKE arm prevents a reliable
                # GIN plan; raw `%` and `_` must not become user-controlled wildcards.
                lexical_queries = build_document_lexical_queries(
                    query=query,
                    tenant_id=tenant_id,
                    user_id=user_id,
                    document_ids=normalized_document_ids,
                    titles=normalized_titles,
                    years=normalized_years,
                    year_start=normalized_year_start,
                    year_end=normalized_year_end,
                    time_start=normalized_time_start,
                    time_end=normalized_time_end,
                    temporal_authority=normalized_temporal_authority,
                    version_scope=normalized_version_scope,
                    candidate_limit=candidate_limit,
                )
                cursor.execute(
                    lexical_queries.full_text.sql,
                    lexical_queries.full_text.params,
                )
                keyword_results = list(cursor.fetchall())
                cursor.execute(
                    lexical_queries.literal_phrase.sql,
                    lexical_queries.literal_phrase.params,
                )
                seen_keyword_ids = {str(row["chunk_id"]) for row in keyword_results}
                keyword_results.extend(
                    row
                    for row in cursor.fetchall()
                    if str(row["chunk_id"]) not in seen_keyword_ids
                )

        # 3. Reciprocal Rank Fusion (RRF with k=60)
        chunk_meta: Dict[str, Dict[str, Any]] = {}

        semantic_ids = []
        for row in semantic_results:
            cid = str(row["chunk_id"])
            semantic_ids.append(cid)
            chunk_meta[cid] = row

        keyword_ids = []
        for row in keyword_results:
            cid = str(row["chunk_id"])
            keyword_ids.append(cid)
            if cid not in chunk_meta:
                chunk_meta[cid] = row

        fused_chunks = reciprocal_rank_fusion([semantic_ids, keyword_ids])

        final_results: List[RetrievedChunk] = []
        for cid, score in fused_chunks[:normalized_top_k]:
            data = chunk_meta[cid]
            final_results.append(
                RetrievedChunk(
                    chunk_id=cid,
                    document_id=str(data["document_id"]),
                    document_version_id=str(data["document_version_id"]),
                    derivation_id=str(data["derivation_id"]),
                    parsed_artifact_id=str(data["input_parsed_artifact_id"]),
                    version_number=int(data["version_number"]),
                    document_title=data.get("doc_title", "Document"),
                    content=data["content"],
                    score=score,
                    content_hash=data["content_hash"],
                    source_time=data.get("source_time"),
                    recorded_at=data.get("recorded_at"),
                    is_current_version=bool(data.get("is_current_version")),
                    page_number=data.get("page_number"),
                    section_title=data.get("section_title", ""),
                    start_char=data.get("start_char"),
                    end_char=data.get("end_char"),
                    text_locator_status=data.get("text_locator_status", "unavailable"),
                    text_locator_profile=data.get(
                        "text_locator_profile", "legacy_unavailable:v0"
                    ),
                )
            )

        return final_results
