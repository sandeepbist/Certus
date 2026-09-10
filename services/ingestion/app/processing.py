import hashlib
import os
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version
from typing import Any, Iterator


PROVENANCE_SCHEMA_VERSION = 2
PARSER_PIPELINE_VERSION = 2
PDF_PARSER_PIPELINE_VERSION = 3
CHUNKER_PIPELINE_VERSION = 2
PARSER_DISTRIBUTIONS = {
    "PDFParser": ("PyMuPDF",),
    "DocxParser": ("python-docx",),
}


def embedding_job_ranges(total_chunks: int, batch_size: int = 20) -> Iterator[tuple[int, int]]:
    if total_chunks < 1:
        raise ValueError("total_chunks must be positive")
    if batch_size < 1:
        raise ValueError("batch_size must be positive")
    for batch_start in range(0, total_chunks, batch_size):
        yield batch_start, min(batch_start + batch_size, total_chunks)


def parse_source_time(value: str | None) -> tuple[datetime | None, str]:
    clean_value = (value or "").strip()
    if not clean_value:
        return None, "unspecified"
    if len(clean_value) > 64:
        raise ValueError("Source time must be a timezone-aware ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(clean_value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(
            "Source time must be a timezone-aware ISO-8601 timestamp"
        ) from error
    if parsed.tzinfo is None:
        raise ValueError("Source time must include a timezone")
    return parsed.astimezone(timezone.utc), "user_provided"


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _distribution_versions(distributions: tuple[str, ...]) -> dict[str, str]:
    installed = {}
    for distribution in distributions:
        try:
            installed[distribution] = version(distribution)
        except PackageNotFoundError:
            installed[distribution] = "unavailable"
    return installed


def _build_revision() -> str:
    return os.getenv("CERTUS_BUILD_REVISION", "unversioned-development").strip() or (
        "unversioned-development"
    )


def parser_profile(parser: Any, source_format: str) -> dict[str, Any]:
    parser_type = type(parser)
    return {
        "schema_version": PROVENANCE_SCHEMA_VERSION,
        "pipeline_version": (
            PDF_PARSER_PIPELINE_VERSION
            if source_format == "pdf"
            else PARSER_PIPELINE_VERSION
        ),
        "implementation": f"certus.ingestion.parsers.{parser_type.__qualname__}",
        "source_format": source_format,
        "text_offset_unit": "unicode_code_point",
        "text_range_semantics": "zero_based_half_open",
        **(
            {
                "page_join_contract": "physical_pages_form_feed:v1",
                "text_extraction_contract": "rawdict_sorted_spans_linefeed:v1",
                "spatial_artifact": "pdf_native_text_layout:v1",
                "coordinate_system": "pymupdf_unrotated_cropbox_top_left_points:v1",
            }
            if source_format == "pdf"
            else {}
        ),
        "library_versions": _distribution_versions(
            PARSER_DISTRIBUTIONS.get(parser_type.__name__, ())
        ),
        "build_revision": _build_revision(),
    }


def chunker_profile(strategy: str, chunker: Any) -> dict[str, Any]:
    chunker_type = type(chunker)
    parameters = {
        name: value
        for name in (
            "target_chunk_tokens",
            "overlap_tokens",
            "max_tokens",
        )
        if isinstance((value := getattr(chunker, name, None)), int)
    }
    return {
        "schema_version": PROVENANCE_SCHEMA_VERSION,
        "pipeline_version": CHUNKER_PIPELINE_VERSION,
        "strategy": strategy,
        "implementation": f"certus.ingestion.chunkers.{chunker_type.__qualname__}",
        "token_estimator": "whitespace_words_x_1.3_floor:v1",
        "sentence_splitter": "source_preserving_punctuation_spans:v2",
        "text_offset_unit": "unicode_code_point",
        "text_range_semantics": "zero_based_half_open",
        "exact_source_spans": True,
        "parameters": parameters,
        "build_revision": _build_revision(),
    }
