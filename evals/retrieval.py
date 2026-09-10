from __future__ import annotations

import hashlib
import json
import math
import platform
import re
import statistics
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from services.ingestion.app.chunking.chunker import TokenChunker
from services.shared.embeddings import (
    EMBEDDING_DIMENSIONS,
    LOCAL_EMBEDDING_PROFILE,
    local_lexical_embedding,
)
from services.shared.retrieval import reciprocal_rank_fusion


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
TOKEN_PATTERN = re.compile(r"[\w'-]+", re.UNICODE)
LEXICAL_STOP_WORDS = {
    "a", "an", "and", "as", "at", "be", "by", "did", "do", "does",
    "for", "from", "how", "in", "is", "it", "of", "on", "or", "rather",
    "than", "that", "the", "their", "this", "to", "was", "what", "when",
    "which", "who", "with",
}
MAX_DOCUMENT_BYTES = 10 * 1024 * 1024
IDENTIFIER_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,99}$")
ANSWER_STATUSES = frozenset({
    "answered",
    "insufficient_evidence",
    "conflicting_evidence",
})


class EvaluationDataError(ValueError):
    """The versioned evaluation corpus or baseline violates its contract."""


@dataclass(frozen=True)
class DocumentSpec:
    document_id: str
    title: str
    path: Path
    sha256: str
    source_time: str
    recorded_time: str
    content: str


@dataclass(frozen=True)
class EvidenceSpec:
    evidence_id: str
    document_id: str
    start_char: int
    end_char: int
    quote: str


@dataclass(frozen=True)
class QuerySpec:
    query_id: str
    query: str
    query_class: str
    tags: tuple[str, ...]
    expected_answer_status: str
    expected_facts: tuple[str, ...]
    evidence: tuple[EvidenceSpec, ...]

    @property
    def answerable(self) -> bool:
        return self.expected_answer_status != "insufficient_evidence"


@dataclass(frozen=True)
class DatasetSpec:
    dataset_id: str
    dataset_version: str
    description: str
    manifest_path: Path
    manifest_sha256: str
    chunking: dict[str, Any]
    retrieval: dict[str, Any]
    documents: tuple[DocumentSpec, ...]
    queries: tuple[QuerySpec, ...]


@dataclass(frozen=True)
class EvaluationChunk:
    chunk_id: str
    document_id: str
    document_title: str
    chunk_index: int
    content: str
    contextualized_content: str
    start_char: int
    end_char: int
    vector: tuple[float, ...]


def _require_mapping(value: Any, context: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise EvaluationDataError(f"{context} must be an object")
    return value


def _require_list(value: Any, context: str) -> list[Any]:
    if not isinstance(value, list):
        raise EvaluationDataError(f"{context} must be an array")
    return value


def _require_string(mapping: dict[str, Any], key: str, context: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value.strip():
        raise EvaluationDataError(f"{context}.{key} must be a non-empty string")
    return value.strip()


def _require_integer(mapping: dict[str, Any], key: str, context: str) -> int:
    value = mapping.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise EvaluationDataError(f"{context}.{key} must be an integer")
    return value


def _require_number(mapping: dict[str, Any], key: str, context: str) -> float:
    value = mapping.get(key)
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise EvaluationDataError(f"{context}.{key} must be a number")
    if not math.isfinite(float(value)):
        raise EvaluationDataError(f"{context}.{key} must be finite")
    return float(value)


def _require_exact_keys(
    mapping: dict[str, Any],
    required: set[str],
    context: str,
) -> None:
    missing = required - set(mapping)
    extra = set(mapping) - required
    if missing or extra:
        details = []
        if missing:
            details.append(f"missing={sorted(missing)}")
        if extra:
            details.append(f"unknown={sorted(extra)}")
        raise EvaluationDataError(f"{context} has invalid fields ({', '.join(details)})")


def _validate_timestamp(value: str, context: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise EvaluationDataError(f"{context} must be ISO-8601") from error
    if parsed.tzinfo is None:
        raise EvaluationDataError(f"{context} must include a timezone")
    return parsed


def _validate_identifier(value: str, context: str) -> None:
    if not IDENTIFIER_PATTERN.fullmatch(value):
        raise EvaluationDataError(
            f"{context} must use lowercase letters, digits, and hyphens"
        )


def _safe_dataset_path(manifest_path: Path, relative_path: str) -> Path:
    dataset_root = manifest_path.parent.resolve()
    candidate = (dataset_root / relative_path).resolve()
    if not candidate.is_relative_to(dataset_root):
        raise EvaluationDataError("Document paths must remain inside the dataset directory")
    if not candidate.is_file():
        raise EvaluationDataError(f"Evaluation document does not exist: {relative_path}")
    return candidate


def _load_document(
    raw: Any,
    index: int,
    manifest_path: Path,
) -> DocumentSpec:
    context = f"documents[{index}]"
    item = _require_mapping(raw, context)
    _require_exact_keys(
        item,
        {
            "document_id", "title", "path", "sha256", "source_time",
            "recorded_time",
        },
        context,
    )
    document_id = _require_string(item, "document_id", context)
    _validate_identifier(document_id, f"{context}.document_id")
    title = _require_string(item, "title", context)
    relative_path = _require_string(item, "path", context)
    expected_sha256 = _require_string(item, "sha256", context).lower()
    if not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
        raise EvaluationDataError(f"{context}.sha256 must be lowercase SHA-256")
    source_time = _require_string(item, "source_time", context)
    recorded_time = _require_string(item, "recorded_time", context)
    parsed_source_time = _validate_timestamp(source_time, f"{context}.source_time")
    parsed_recorded_time = _validate_timestamp(
        recorded_time, f"{context}.recorded_time"
    )
    if parsed_recorded_time < parsed_source_time:
        raise EvaluationDataError(
            f"{context}.recorded_time cannot precede source_time"
        )

    path = _safe_dataset_path(manifest_path, relative_path)
    raw_bytes = path.read_bytes()
    if len(raw_bytes) > MAX_DOCUMENT_BYTES:
        raise EvaluationDataError(f"{context}.path exceeds {MAX_DOCUMENT_BYTES} bytes")
    actual_sha256 = hashlib.sha256(raw_bytes).hexdigest()
    if actual_sha256 != expected_sha256:
        raise EvaluationDataError(
            f"Checksum mismatch for {relative_path}: "
            f"expected {expected_sha256}, got {actual_sha256}"
        )
    try:
        content = raw_bytes.decode("utf-8")
    except UnicodeDecodeError as error:
        raise EvaluationDataError(f"{relative_path} must be UTF-8") from error
    if not content.strip():
        raise EvaluationDataError(f"{relative_path} cannot be blank")

    return DocumentSpec(
        document_id=document_id,
        title=title,
        path=path,
        sha256=expected_sha256,
        source_time=source_time,
        recorded_time=recorded_time,
        content=content,
    )


def _load_evidence(
    raw: Any,
    query_context: str,
    index: int,
    documents: dict[str, DocumentSpec],
) -> EvidenceSpec:
    context = f"{query_context}.evidence[{index}]"
    item = _require_mapping(raw, context)
    _require_exact_keys(
        item,
        {"evidence_id", "document_id", "start_char", "end_char", "quote"},
        context,
    )
    evidence_id = _require_string(item, "evidence_id", context)
    _validate_identifier(evidence_id, f"{context}.evidence_id")
    document_id = _require_string(item, "document_id", context)
    start_char = _require_integer(item, "start_char", context)
    end_char = _require_integer(item, "end_char", context)
    quote = _require_string(item, "quote", context)
    document = documents.get(document_id)
    if document is None:
        raise EvaluationDataError(f"{context} references unknown document {document_id}")
    if start_char < 0 or end_char <= start_char or end_char > len(document.content):
        raise EvaluationDataError(f"{context} has invalid character boundaries")
    actual_quote = document.content[start_char:end_char]
    if actual_quote != quote:
        raise EvaluationDataError(
            f"{context} quote does not exactly match its source character span"
        )
    return EvidenceSpec(
        evidence_id=evidence_id,
        document_id=document_id,
        start_char=start_char,
        end_char=end_char,
        quote=quote,
    )


def _load_query(
    raw: Any,
    index: int,
    documents: dict[str, DocumentSpec],
    schema_version: int,
) -> QuerySpec:
    context = f"queries[{index}]"
    item = _require_mapping(raw, context)
    common_fields = {
        "query_id", "query", "query_class", "tags", "expected_facts",
        "evidence",
    }
    status_field = "answerable" if schema_version == 1 else "expected_answer_status"
    _require_exact_keys(item, common_fields | {status_field}, context)
    query_id = _require_string(item, "query_id", context)
    _validate_identifier(query_id, f"{context}.query_id")
    query = _require_string(item, "query", context)
    query_class = _require_string(item, "query_class", context)
    raw_tags = _require_list(item.get("tags"), f"{context}.tags")
    tags = tuple(
        _require_string({"value": value}, "value", f"{context}.tags[{tag_index}]")
        for tag_index, value in enumerate(raw_tags)
    )
    if len(set(tags)) != len(tags):
        raise EvaluationDataError(f"{context}.tags cannot contain duplicates")
    if schema_version == 1:
        answerable = item.get("answerable")
        if not isinstance(answerable, bool):
            raise EvaluationDataError(f"{context}.answerable must be boolean")
        expected_answer_status = (
            "answered" if answerable else "insufficient_evidence"
        )
    else:
        expected_answer_status = _require_string(
            item,
            "expected_answer_status",
            context,
        )
        if expected_answer_status not in ANSWER_STATUSES:
            raise EvaluationDataError(
                f"{context}.expected_answer_status must be one of "
                f"{sorted(ANSWER_STATUSES)}"
            )
    raw_facts = _require_list(item.get("expected_facts"), f"{context}.expected_facts")
    expected_facts = tuple(
        _require_string(
            {"value": value},
            "value",
            f"{context}.expected_facts[{fact_index}]",
        )
        for fact_index, value in enumerate(raw_facts)
    )
    raw_evidence = _require_list(item.get("evidence"), f"{context}.evidence")
    evidence = tuple(
        _load_evidence(value, context, evidence_index, documents)
        for evidence_index, value in enumerate(raw_evidence)
    )
    evidence_ids = [item.evidence_id for item in evidence]
    if len(set(evidence_ids)) != len(evidence_ids):
        raise EvaluationDataError(f"{context}.evidence_id values must be unique")
    if expected_answer_status != "insufficient_evidence" and (
        not expected_facts or not evidence
    ):
        raise EvaluationDataError(
            f"{context} answerable queries require expected facts and evidence"
        )
    if expected_answer_status == "insufficient_evidence" and (
        expected_facts or evidence
    ):
        raise EvaluationDataError(
            f"{context} no-answer queries cannot declare facts or evidence"
        )
    if expected_answer_status == "conflicting_evidence" and len(evidence) < 2:
        raise EvaluationDataError(
            f"{context} conflicting queries require at least two evidence spans"
        )

    return QuerySpec(
        query_id=query_id,
        query=query,
        query_class=query_class,
        tags=tags,
        expected_answer_status=expected_answer_status,
        expected_facts=expected_facts,
        evidence=evidence,
    )


def load_dataset(manifest_path: Path | str) -> DatasetSpec:
    path = Path(manifest_path).resolve()
    if not path.is_file():
        raise EvaluationDataError(f"Evaluation manifest does not exist: {path}")
    raw_bytes = path.read_bytes()
    try:
        raw = json.loads(raw_bytes)
    except json.JSONDecodeError as error:
        raise EvaluationDataError(f"Invalid evaluation manifest JSON: {error}") from error
    manifest = _require_mapping(raw, "manifest")
    _require_exact_keys(
        manifest,
        {
            "schema_version", "dataset_id", "dataset_version", "description",
            "chunking", "retrieval", "documents", "queries",
        },
        "manifest",
    )
    schema_version = manifest.get("schema_version")
    if schema_version not in {1, 2}:
        raise EvaluationDataError(
            "Only evaluation manifest schema_version 1 or 2 is supported"
        )

    chunking = _require_mapping(manifest.get("chunking"), "manifest.chunking")
    _require_exact_keys(
        chunking,
        {"strategy", "target_chunk_tokens", "overlap_tokens"},
        "manifest.chunking",
    )
    if _require_string(chunking, "strategy", "manifest.chunking") != "token":
        raise EvaluationDataError("Seed evaluator currently supports only token chunking")
    target_tokens = _require_integer(
        chunking, "target_chunk_tokens", "manifest.chunking"
    )
    overlap_tokens = _require_integer(chunking, "overlap_tokens", "manifest.chunking")
    if target_tokens <= 0 or overlap_tokens < 0 or overlap_tokens >= target_tokens:
        raise EvaluationDataError("manifest.chunking has invalid token bounds")

    retrieval = _require_mapping(manifest.get("retrieval"), "manifest.retrieval")
    _require_exact_keys(
        retrieval,
        {
            "dense_min_similarity", "lexical_min_score", "candidate_limit",
            "rrf_k", "evaluation_cutoffs",
        },
        "manifest.retrieval",
    )
    dense_threshold = _require_number(
        retrieval, "dense_min_similarity", "manifest.retrieval"
    )
    lexical_threshold = _require_number(
        retrieval, "lexical_min_score", "manifest.retrieval"
    )
    candidate_limit = _require_integer(
        retrieval, "candidate_limit", "manifest.retrieval"
    )
    rrf_k = _require_number(retrieval, "rrf_k", "manifest.retrieval")
    cutoffs = _require_list(
        retrieval.get("evaluation_cutoffs"),
        "manifest.retrieval.evaluation_cutoffs",
    )
    if not -1 <= dense_threshold <= 1:
        raise EvaluationDataError("dense_min_similarity must be between -1 and 1")
    if not 0 <= lexical_threshold <= 1:
        raise EvaluationDataError("lexical_min_score must be between 0 and 1")
    if candidate_limit <= 0 or rrf_k <= 0:
        raise EvaluationDataError("candidate_limit and rrf_k must be positive")
    if cutoffs != [1, 5, 20] or candidate_limit < max(cutoffs):
        raise EvaluationDataError(
            "schema v1 requires evaluation_cutoffs [1, 5, 20] within candidate_limit"
        )

    raw_documents = _require_list(manifest.get("documents"), "manifest.documents")
    documents = tuple(
        _load_document(value, index, path)
        for index, value in enumerate(raw_documents)
    )
    if not documents:
        raise EvaluationDataError("Evaluation dataset must contain documents")
    document_ids = [document.document_id for document in documents]
    if len(set(document_ids)) != len(document_ids):
        raise EvaluationDataError("Document identifiers must be unique")
    document_map = {document.document_id: document for document in documents}

    raw_queries = _require_list(manifest.get("queries"), "manifest.queries")
    queries = tuple(
        _load_query(value, index, document_map, schema_version)
        for index, value in enumerate(raw_queries)
    )
    if not queries:
        raise EvaluationDataError("Evaluation dataset must contain queries")
    query_ids = [query.query_id for query in queries]
    if len(set(query_ids)) != len(query_ids):
        raise EvaluationDataError("Query identifiers must be unique")
    if not any(query.answerable for query in queries) or not any(
        not query.answerable for query in queries
    ):
        raise EvaluationDataError(
            "Evaluation dataset requires answerable and no-answer queries"
        )

    return DatasetSpec(
        dataset_id=_require_string(manifest, "dataset_id", "manifest"),
        dataset_version=_require_string(manifest, "dataset_version", "manifest"),
        description=_require_string(manifest, "description", "manifest"),
        manifest_path=path,
        manifest_sha256=hashlib.sha256(raw_bytes).hexdigest(),
        chunking=dict(chunking),
        retrieval=dict(retrieval),
        documents=documents,
        queries=queries,
    )


def _build_chunks(dataset: DatasetSpec) -> tuple[EvaluationChunk, ...]:
    chunker = TokenChunker(
        target_chunk_tokens=int(dataset.chunking["target_chunk_tokens"]),
        overlap_tokens=int(dataset.chunking["overlap_tokens"]),
    )
    chunks: list[EvaluationChunk] = []
    for document in dataset.documents:
        document_chunks = chunker.chunk(document.content, document.title)
        if not document_chunks:
            raise EvaluationDataError(
                f"Chunking produced no content for {document.document_id}"
            )
        for chunk in document_chunks:
            vector = local_lexical_embedding(chunk.contextualized_content)
            chunks.append(
                EvaluationChunk(
                    chunk_id=f"{document.document_id}:{chunk.chunk_index:06d}",
                    document_id=document.document_id,
                    document_title=document.title,
                    chunk_index=chunk.chunk_index,
                    content=chunk.content,
                    contextualized_content=chunk.contextualized_content,
                    start_char=chunk.start_char,
                    end_char=chunk.end_char,
                    vector=tuple(vector),
                )
            )
    return tuple(chunks)


def _lexical_terms(text: str) -> set[str]:
    return {
        token.lower()
        for token in TOKEN_PATTERN.findall(text)
        if len(token) > 1 and token.lower() not in LEXICAL_STOP_WORDS
    }


def _lexical_score(query: str, content: str) -> float:
    query_terms = _lexical_terms(query)
    content_terms = _lexical_terms(content)
    if not query_terms or not content_terms:
        return 0.0
    overlap = len(query_terms & content_terms)
    return overlap / math.sqrt(len(query_terms) * len(content_terms))


def _cosine_similarity(left: list[float], right: tuple[float, ...]) -> float:
    return sum(a * b for a, b in zip(left, right))


def _rank_query(
    query: str,
    chunks: tuple[EvaluationChunk, ...],
    dataset: DatasetSpec,
) -> dict[str, list[str]]:
    query_vector = local_lexical_embedding(query)
    candidate_limit = int(dataset.retrieval["candidate_limit"])
    dense_threshold = float(dataset.retrieval["dense_min_similarity"])
    lexical_threshold = float(dataset.retrieval["lexical_min_score"])

    dense_scored = [
        (chunk.chunk_id, _cosine_similarity(query_vector, chunk.vector))
        for chunk in chunks
    ]
    dense = [
        chunk_id
        for chunk_id, score in sorted(
            dense_scored,
            key=lambda item: (-item[1], item[0]),
        )
        if score >= dense_threshold
    ][:candidate_limit]

    lexical_scored = [
        (chunk.chunk_id, _lexical_score(query, chunk.content))
        for chunk in chunks
    ]
    lexical = [
        chunk_id
        for chunk_id, score in sorted(
            lexical_scored,
            key=lambda item: (-item[1], item[0]),
        )
        if score >= lexical_threshold
    ][:candidate_limit]

    hybrid = [
        chunk_id
        for chunk_id, _ in reciprocal_rank_fusion(
            [dense, lexical],
            rank_constant=float(dataset.retrieval["rrf_k"]),
        )
    ][:candidate_limit]
    return {"dense_exact": dense, "lexical": lexical, "hybrid_rrf": hybrid}


def _evidence_chunk_map(
    query: QuerySpec,
    chunks: tuple[EvaluationChunk, ...],
) -> dict[str, set[str]]:
    mapping: dict[str, set[str]] = {}
    for evidence in query.evidence:
        matching_chunks = {
            chunk.chunk_id
            for chunk in chunks
            if chunk.document_id == evidence.document_id
            and evidence.quote in chunk.content
        }
        if not matching_chunks:
            raise EvaluationDataError(
                f"Evidence {evidence.evidence_id} is not contained in any generated chunk"
            )
        mapping[evidence.evidence_id] = matching_chunks
    return mapping


def _ndcg_at_k(ranking: list[str], relevant_chunks: set[str], cutoff: int) -> float:
    if not relevant_chunks:
        return 0.0
    dcg = sum(
        1.0 / math.log2(rank + 1)
        for rank, chunk_id in enumerate(ranking[:cutoff], start=1)
        if chunk_id in relevant_chunks
    )
    ideal_count = min(len(relevant_chunks), cutoff)
    ideal = sum(1.0 / math.log2(rank + 1) for rank in range(1, ideal_count + 1))
    return dcg / ideal if ideal else 0.0


def _query_metrics(
    query: QuerySpec,
    ranking: list[str],
    evidence_chunks: dict[str, set[str]],
) -> dict[str, Any]:
    relevant_chunks = set().union(*evidence_chunks.values()) if evidence_chunks else set()
    metrics: dict[str, Any] = {
        "returned_count": len(ranking),
        "returned_any": bool(ranking),
    }
    if not query.answerable:
        metrics["empty_result"] = not ranking
        return metrics

    for cutoff in (1, 5, 20):
        retrieved = set(ranking[:cutoff])
        matched_evidence = sum(
            1 for chunk_ids in evidence_chunks.values() if retrieved & chunk_ids
        )
        metrics[f"evidence_recall_at_{cutoff}"] = matched_evidence / len(
            evidence_chunks
        )
        metrics[f"chunk_precision_at_{cutoff}"] = len(
            retrieved & relevant_chunks
        ) / cutoff

    first_relevant_rank = next(
        (
            rank
            for rank, chunk_id in enumerate(ranking[:20], start=1)
            if chunk_id in relevant_chunks
        ),
        None,
    )
    metrics["reciprocal_rank_at_20"] = (
        1.0 / first_relevant_rank if first_relevant_rank else 0.0
    )
    metrics["ndcg_at_5"] = _ndcg_at_k(ranking, relevant_chunks, 5)
    metrics["complete_evidence_at_5"] = all(
        set(ranking[:5]) & chunk_ids for chunk_ids in evidence_chunks.values()
    )
    return metrics


def _mean(values: list[float]) -> float:
    return statistics.fmean(values) if values else 0.0


def _aggregate_method(
    query_reports: list[dict[str, Any]],
    method: str,
) -> dict[str, Any]:
    answerable = [report for report in query_reports if report["answerable"]]
    no_answer = [report for report in query_reports if not report["answerable"]]
    multi_evidence = [
        report for report in answerable if report["evidence_count"] > 1
    ]
    conflicting = [
        report
        for report in answerable
        if report["expected_answer_status"] == "conflicting_evidence"
    ]
    method_metrics = [report["methods"][method]["metrics"] for report in answerable]
    no_answer_metrics = [
        report["methods"][method]["metrics"] for report in no_answer
    ]

    aggregate: dict[str, Any] = {
        "query_count": len(query_reports),
        "answerable_query_count": len(answerable),
        "no_answer_query_count": len(no_answer),
        "multi_evidence_query_count": len(multi_evidence),
        "conflicting_query_count": len(conflicting),
    }
    for cutoff in (1, 5, 20):
        aggregate[f"answerable_evidence_recall_at_{cutoff}"] = _mean([
            float(metrics[f"evidence_recall_at_{cutoff}"])
            for metrics in method_metrics
        ])
        aggregate[f"answerable_chunk_precision_at_{cutoff}"] = _mean([
            float(metrics[f"chunk_precision_at_{cutoff}"])
            for metrics in method_metrics
        ])
    aggregate["answerable_mrr_at_20"] = _mean([
        float(metrics["reciprocal_rank_at_20"]) for metrics in method_metrics
    ])
    aggregate["answerable_ndcg_at_5"] = _mean([
        float(metrics["ndcg_at_5"]) for metrics in method_metrics
    ])
    aggregate["multi_evidence_complete_at_5"] = _mean([
        1.0 if report["methods"][method]["metrics"]["complete_evidence_at_5"] else 0.0
        for report in multi_evidence
    ])
    aggregate["conflicting_evidence_complete_at_5"] = _mean([
        1.0 if report["methods"][method]["metrics"]["complete_evidence_at_5"] else 0.0
        for report in conflicting
    ])
    aggregate["no_answer_empty_rate"] = _mean([
        1.0 if metrics["empty_result"] else 0.0 for metrics in no_answer_metrics
    ])
    return {key: round(value, 6) if isinstance(value, float) else value for key, value in aggregate.items()}


def _cohort_report(
    query_reports: list[dict[str, Any]],
    method: str,
) -> dict[str, dict[str, Any]]:
    cohorts: dict[str, list[dict[str, Any]]] = {}
    for report in query_reports:
        labels = [f"class:{report['query_class']}"] + [
            f"tag:{tag}" for tag in report["tags"]
        ]
        labels.append(f"status:{report['expected_answer_status']}")
        for label in labels:
            cohorts.setdefault(label, []).append(report)

    output: dict[str, dict[str, Any]] = {}
    for label, reports in sorted(cohorts.items()):
        answerable = [report for report in reports if report["answerable"]]
        no_answer = [report for report in reports if not report["answerable"]]
        output[label] = {
            "query_count": len(reports),
            "answerable_evidence_recall_at_5": round(_mean([
                float(report["methods"][method]["metrics"]["evidence_recall_at_5"])
                for report in answerable
            ]), 6),
            "answerable_mrr_at_20": round(_mean([
                float(report["methods"][method]["metrics"]["reciprocal_rank_at_20"])
                for report in answerable
            ]), 6),
            "no_answer_empty_rate": round(_mean([
                1.0
                if report["methods"][method]["metrics"]["empty_result"]
                else 0.0
                for report in no_answer
            ]), 6),
        }
    return output


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    position = (len(ordered) - 1) * percentile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def _source_fingerprint(dataset: DatasetSpec) -> str:
    source_paths = [
        REPOSITORY_ROOT / "services/shared/embeddings.py",
        REPOSITORY_ROOT / "services/shared/retrieval.py",
        REPOSITORY_ROOT / "services/ingestion/app/chunking/chunker.py",
        Path(__file__).resolve(),
    ]
    digest = hashlib.sha256()
    digest.update(dataset.manifest_sha256.encode("ascii"))
    for path in source_paths:
        digest.update(path.relative_to(REPOSITORY_ROOT).as_posix().encode("utf-8"))
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def evaluate_dataset(dataset: DatasetSpec) -> dict[str, Any]:
    chunks = _build_chunks(dataset)
    query_reports: list[dict[str, Any]] = []
    latencies_ms: list[float] = []

    for query in dataset.queries:
        started = time.perf_counter()
        rankings = _rank_query(query.query, chunks, dataset)
        latency_ms = (time.perf_counter() - started) * 1000
        latencies_ms.append(latency_ms)
        evidence_chunks = _evidence_chunk_map(query, chunks)
        methods = {
            method: {
                "ranking": ranking,
                "metrics": _query_metrics(query, ranking, evidence_chunks),
            }
            for method, ranking in rankings.items()
        }
        query_reports.append({
            "query_id": query.query_id,
            "query": query.query,
            "query_class": query.query_class,
            "tags": list(query.tags),
            "answerable": query.answerable,
            "expected_answer_status": query.expected_answer_status,
            "expected_facts": list(query.expected_facts),
            "evidence_count": len(query.evidence),
            "evidence": [
                {
                    "evidence_id": evidence.evidence_id,
                    "document_id": evidence.document_id,
                    "start_char": evidence.start_char,
                    "end_char": evidence.end_char,
                    "matching_chunk_ids": sorted(evidence_chunks[evidence.evidence_id]),
                }
                for evidence in query.evidence
            ],
            "methods": methods,
            "latency_ms": round(latency_ms, 6),
        })

    method_names = ("dense_exact", "lexical", "hybrid_rrf")
    methods = {
        method: {
            "aggregate": _aggregate_method(query_reports, method),
            "cohorts": _cohort_report(query_reports, method),
        }
        for method in method_names
    }
    corpus_bytes = sum(len(document.content.encode("utf-8")) for document in dataset.documents)
    chunk_text_bytes = sum(len(chunk.content.encode("utf-8")) for chunk in chunks)
    vector_bytes = len(chunks) * EMBEDDING_DIMENSIONS * 4

    return {
        "schema_version": 2,
        "dataset": {
            "dataset_id": dataset.dataset_id,
            "dataset_version": dataset.dataset_version,
            "manifest_sha256": dataset.manifest_sha256,
            "description": dataset.description,
            "document_count": len(dataset.documents),
            "query_count": len(dataset.queries),
            "documents": [
                {
                    "document_id": document.document_id,
                    "sha256": document.sha256,
                    "source_time": document.source_time,
                    "recorded_time": document.recorded_time,
                }
                for document in dataset.documents
            ],
        },
        "run": {
            "runner_id": "certus-offline-retrieval-v2",
            "run_fingerprint": _source_fingerprint(dataset),
            "python": platform.python_version(),
            "embedding_profile": LOCAL_EMBEDDING_PROFILE.identifier,
            "chunking": dataset.chunking,
            "retrieval": dataset.retrieval,
            "provider_calls": 0,
        },
        "corpus": {
            "chunk_count": len(chunks),
            "source_bytes": corpus_bytes,
            "chunk_text_bytes": chunk_text_bytes,
            "estimated_vector_bytes_float32": vector_bytes,
            "estimated_total_index_payload_bytes": chunk_text_bytes + vector_bytes,
        },
        "methods": methods,
        "queries": query_reports,
        "latency_ms": {
            "mean": round(_mean(latencies_ms), 6),
            "p50": round(_percentile(latencies_ms, 0.50), 6),
            "p95": round(_percentile(latencies_ms, 0.95), 6),
            "max": round(max(latencies_ms, default=0.0), 6),
        },
    }


def _resolve_report_value(report: dict[str, Any], path: str) -> Any:
    value: Any = report
    for part in path.split("."):
        if not isinstance(value, dict) or part not in value:
            raise EvaluationDataError(f"Baseline metric path does not exist: {path}")
        value = value[part]
    return value


def check_baseline(report: dict[str, Any], baseline_path: Path | str) -> list[str]:
    path = Path(baseline_path).resolve()
    if not path.is_file():
        raise EvaluationDataError(f"Evaluation baseline does not exist: {path}")
    try:
        baseline = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise EvaluationDataError(f"Invalid evaluation baseline JSON: {error}") from error
    baseline = _require_mapping(baseline, "baseline")
    _require_exact_keys(
        baseline,
        {
            "schema_version", "dataset_id", "dataset_version",
            "manifest_sha256", "run_fingerprint", "known_gaps", "gates",
        },
        "baseline",
    )
    if baseline.get("schema_version") != 1:
        raise EvaluationDataError("Only baseline schema_version 1 is supported")
    expected_identity = (
        _require_string(baseline, "dataset_id", "baseline"),
        _require_string(baseline, "dataset_version", "baseline"),
        _require_string(baseline, "manifest_sha256", "baseline"),
        _require_string(baseline, "run_fingerprint", "baseline"),
    )
    actual_identity = (
        report["dataset"]["dataset_id"],
        report["dataset"]["dataset_version"],
        report["dataset"]["manifest_sha256"],
        report["run"]["run_fingerprint"],
    )
    if actual_identity != expected_identity:
        raise EvaluationDataError(
            "Evaluation corpus or runner changed; review the report and update the "
            "baseline identity explicitly"
        )

    known_gaps = _require_list(baseline.get("known_gaps"), "baseline.known_gaps")
    for index, value in enumerate(known_gaps):
        _require_string(
            {"value": value},
            "value",
            f"baseline.known_gaps[{index}]",
        )

    failures: list[str] = []
    gates = _require_list(baseline.get("gates"), "baseline.gates")
    for index, raw_gate in enumerate(gates):
        context = f"baseline.gates[{index}]"
        gate = _require_mapping(raw_gate, context)
        _require_exact_keys(gate, {"metric", "operator", "value"}, context)
        metric = _require_string(gate, "metric", context)
        operator = _require_string(gate, "operator", context)
        expected = _require_number(gate, "value", context)
        actual = _resolve_report_value(report, metric)
        if not isinstance(actual, (int, float)) or isinstance(actual, bool):
            raise EvaluationDataError(f"Baseline metric is not numeric: {metric}")
        passed = {
            ">=": float(actual) >= expected,
            "<=": float(actual) <= expected,
            "==": float(actual) == expected,
        }.get(operator)
        if passed is None:
            raise EvaluationDataError(f"Unsupported baseline operator: {operator}")
        if not passed:
            failures.append(
                f"{metric} expected {operator} {expected}, observed {actual}"
            )
    return failures
