"""Deterministic native-PDF text and spatial artifact extraction.

The parsed text and geometry in this module come from one explicitly configured
PyMuPDF TextPage per physical page. This avoids the unprovable alignment created
by extracting plain text and coordinates through independent parser passes.
"""

from __future__ import annotations

import base64
import gzip
import hashlib
import json
import os
from dataclasses import dataclass
from typing import Any, Iterable


LAYOUT_SCHEMA_VERSION = 1
LAYOUT_MIME_TYPE = "application/vnd.certus.pdf-layout+json"
LAYOUT_CONTENT_ENCODING = "gzip"
LAYOUT_COORDINATE_SYSTEM = "pymupdf_unrotated_cropbox_top_left_points:v1"
PAGE_JOIN_CONTRACT = "physical_pages_form_feed:v1"
TEXT_EXTRACTION_CONTRACT = "rawdict_sorted_spans_linefeed:v1"
MAX_LAYOUT_PAGES = 5_000
MAX_LAYOUT_GLYPHS = 2_000_000
MAX_LAYOUT_CANONICAL_BYTES = 256 * 1024 * 1024
MAX_LAYOUT_COMPRESSED_BYTES = 64 * 1024 * 1024


def _sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _sha256_text(content: str) -> str:
    return _sha256_bytes(content.encode("utf-8"))


def _float_list(values: Iterable[Any]) -> list[float]:
    result = [float(value) for value in values]
    if any(value != value or value in {float("inf"), float("-inf")} for value in result):
        raise ValueError("PDF geometry must contain only finite coordinates")
    return result


def _quad_list(quad: Any) -> list[float]:
    return _float_list(
        (
            quad.ul.x,
            quad.ul.y,
            quad.ur.x,
            quad.ur.y,
            quad.ll.x,
            quad.ll.y,
            quad.lr.x,
            quad.lr.y,
        )
    )


@dataclass(frozen=True)
class PdfLayoutPage:
    page_index: int
    page_label: str
    width_points: float
    height_points: float
    rotation_degrees: int
    media_box: tuple[float, float, float, float]
    crop_box: tuple[float, float, float, float]
    parsed_start: int
    parsed_end: int
    extraction_status: str


@dataclass(frozen=True)
class PdfTextRun:
    page_index: int
    parsed_start: int
    parsed_end: int
    reading_order: int
    source_block_index: int
    line_index: int
    span_index: int
    text_sha256: str
    bbox: tuple[float, float, float, float]
    quad: tuple[float, float, float, float, float, float, float, float]
    direction: tuple[float, float]
    writing_mode: int


@dataclass(frozen=True)
class PdfLayoutArtifact:
    raw_text: str
    pages: tuple[PdfLayoutPage, ...]
    text_runs: tuple[PdfTextRun, ...]
    producer_profile: dict[str, Any]
    canonical_bytes: bytes
    compressed_bytes: bytes
    canonical_content_sha256: str
    content_sha256: str
    checksum_sha256_base64: str

    @property
    def byte_length(self) -> int:
        return len(self.compressed_bytes)

    @property
    def uncompressed_byte_length(self) -> int:
        return len(self.canonical_bytes)


def _producer_profile(pymupdf: Any, textpage_flags: int) -> dict[str, Any]:
    build_revision = os.getenv(
        "CERTUS_BUILD_REVISION",
        "unversioned-development",
    ).strip() or "unversioned-development"
    return {
        "schema_version": LAYOUT_SCHEMA_VERSION,
        "pipeline_version": 1,
        "implementation": "certus.ingestion.pdf_layout.extract_native_pdf_layout",
        "library_versions": {
            "PyMuPDF": str(pymupdf.__version__),
            "MuPDF": ".".join(str(part) for part in pymupdf.mupdf_version_tuple),
        },
        "textpage_flags": textpage_flags,
        "preserve_ligatures": True,
        "preserve_whitespace": True,
        "dehyphenate": False,
        "include_images": False,
        "reading_order": "pymupdf_rawdict_sort_true:v1",
        "text_extraction_contract": TEXT_EXTRACTION_CONTRACT,
        "text_offset_unit": "unicode_code_point",
        "text_range_semantics": "zero_based_half_open",
        "coordinate_system": LAYOUT_COORDINATE_SYSTEM,
        "coordinate_origin": "visible_cropbox_top_left",
        "coordinate_unit": "pdf_point_1_72_inch",
        "page_join_contract": PAGE_JOIN_CONTRACT,
        "build_revision": build_revision,
    }


def extract_native_pdf_layout(content_bytes: bytes, filename: str) -> PdfLayoutArtifact:
    """Extract canonical text plus exact native glyph geometry from one graph/page."""

    import pymupdf

    textpage_flags = pymupdf.TEXT_PRESERVE_LIGATURES | pymupdf.TEXT_PRESERVE_WHITESPACE
    profile = _producer_profile(pymupdf, textpage_flags)
    full_text_parts: list[str] = []
    page_rows: list[PdfLayoutPage] = []
    run_rows: list[PdfTextRun] = []
    artifact_pages: list[dict[str, Any]] = []
    artifact_length = 0
    source_sha256 = _sha256_bytes(content_bytes)
    glyph_count = 0

    with pymupdf.open(stream=content_bytes, filetype="pdf") as document:
        if document.is_encrypted:
            raise ValueError("encrypted_pdf")

        if len(document) > MAX_LAYOUT_PAGES:
            raise ValueError("layout_limit")

        for page_index in range(len(document)):
            page = document[page_index]
            if page_index:
                full_text_parts.append("\f")
                artifact_length += 1
            page_start = artifact_length
            textpage = page.get_textpage(flags=textpage_flags)
            raw = textpage.extractRAWDICT(sort=True)
            page_run_payloads: list[dict[str, Any]] = []
            reading_order = 0

            for sorted_block_index, block in enumerate(raw.get("blocks", [])):
                if int(block.get("type", -1)) != 0:
                    continue
                source_block_index = int(block.get("number", sorted_block_index))
                for line_index, line in enumerate(block.get("lines", [])):
                    line_has_text = False
                    direction_values = _float_list(line.get("dir", (1.0, 0.0)))
                    direction = (direction_values[0], direction_values[1])
                    writing_mode = int(line.get("wmode", 0))
                    for span_index, span in enumerate(line.get("spans", [])):
                        characters = list(span.get("chars", []))
                        span_text = "".join(str(character.get("c", "")) for character in characters)
                        if not span_text:
                            continue
                        line_has_text = True
                        parsed_start = artifact_length
                        full_text_parts.append(span_text)
                        artifact_length += len(span_text)
                        parsed_end = artifact_length
                        span_quad = pymupdf.recover_quad(direction, span)
                        bbox_values = _float_list(span["bbox"])
                        quad_values = _quad_list(span_quad)
                        glyphs: list[dict[str, Any]] = []
                        glyph_offset = parsed_start
                        for character in characters:
                            character_text = str(character.get("c", ""))
                            if not character_text:
                                continue
                            glyph_count += 1
                            if glyph_count > MAX_LAYOUT_GLYPHS:
                                raise ValueError("layout_limit")
                            character_quad = pymupdf.recover_char_quad(direction, span, character)
                            glyphs.append(
                                {
                                    "parsed_start": glyph_offset,
                                    "parsed_end": glyph_offset + len(character_text),
                                    "text": character_text,
                                    "bbox": _float_list(character["bbox"]),
                                    "quad": _quad_list(character_quad),
                                    "synthetic": bool(character.get("synthetic", False)),
                                }
                            )
                            glyph_offset += len(character_text)
                        if glyph_offset != parsed_end:
                            raise ValueError("PDF glyph offsets do not cover their text span")

                        run = PdfTextRun(
                            page_index=page_index,
                            parsed_start=parsed_start,
                            parsed_end=parsed_end,
                            reading_order=reading_order,
                            source_block_index=source_block_index,
                            line_index=line_index,
                            span_index=span_index,
                            text_sha256=_sha256_text(span_text),
                            bbox=tuple(bbox_values),
                            quad=tuple(quad_values),
                            direction=direction,
                            writing_mode=writing_mode,
                        )
                        run_rows.append(run)
                        page_run_payloads.append(
                            {
                                "parsed_start": parsed_start,
                                "parsed_end": parsed_end,
                                "reading_order": reading_order,
                                "source_block_index": source_block_index,
                                "line_index": line_index,
                                "span_index": span_index,
                                "text_sha256": run.text_sha256,
                                "bbox": bbox_values,
                                "quad": quad_values,
                                "direction": direction_values,
                                "writing_mode": writing_mode,
                                "glyphs": glyphs,
                            }
                        )
                        reading_order += 1
                    if line_has_text:
                        full_text_parts.append("\n")
                        artifact_length += 1

            page_end = artifact_length
            crop_values = _float_list(page.cropbox)
            media_values = _float_list(page.mediabox)
            page_label = str(page.get_label() or page_index + 1)
            page_row = PdfLayoutPage(
                page_index=page_index,
                page_label=page_label,
                width_points=float(page.cropbox.width),
                height_points=float(page.cropbox.height),
                rotation_degrees=int(page.rotation),
                media_box=tuple(media_values),
                crop_box=tuple(crop_values),
                parsed_start=page_start,
                parsed_end=page_end,
                extraction_status="native_text" if page_end > page_start else "empty",
            )
            page_rows.append(page_row)
            artifact_pages.append(
                {
                    "page_index": page_index,
                    "page_label": page_label,
                    "width_points": page_row.width_points,
                    "height_points": page_row.height_points,
                    "rotation_degrees": page_row.rotation_degrees,
                    "media_box": media_values,
                    "crop_box": crop_values,
                    "parsed_start": page_start,
                    "parsed_end": page_end,
                    "extraction_status": page_row.extraction_status,
                    "text_runs": page_run_payloads,
                }
            )

    raw_text = "".join(full_text_parts)
    parsed_sha256 = _sha256_text(raw_text)
    canonical_payload = {
        "schema_version": LAYOUT_SCHEMA_VERSION,
        "artifact_type": "pdf_native_text_layout",
        "source": {
            "filename": filename,
            "content_sha256": source_sha256,
        },
        "parsed_text": {
            "content_sha256": parsed_sha256,
            "length_code_points": len(raw_text),
            "offset_unit": "unicode_code_point",
            "range_semantics": "zero_based_half_open",
        },
        "coordinate_system": LAYOUT_COORDINATE_SYSTEM,
        "page_join_contract": PAGE_JOIN_CONTRACT,
        "producer_profile": profile,
        "pages": artifact_pages,
    }
    canonical_bytes = json.dumps(
        canonical_payload,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(canonical_bytes) > MAX_LAYOUT_CANONICAL_BYTES:
        raise ValueError("layout_limit")
    compressed_bytes = gzip.compress(canonical_bytes, compresslevel=9, mtime=0)
    if len(compressed_bytes) > MAX_LAYOUT_COMPRESSED_BYTES:
        raise ValueError("layout_limit")
    compressed_digest = hashlib.sha256(compressed_bytes).digest()
    return PdfLayoutArtifact(
        raw_text=raw_text,
        pages=tuple(page_rows),
        text_runs=tuple(run_rows),
        producer_profile=profile,
        canonical_bytes=canonical_bytes,
        compressed_bytes=compressed_bytes,
        canonical_content_sha256=_sha256_bytes(canonical_bytes),
        content_sha256=compressed_digest.hex(),
        checksum_sha256_base64=base64.b64encode(compressed_digest).decode("ascii"),
    )
