"""Verified resolution of immutable PDF layout artifacts into glyph selectors."""

from __future__ import annotations

import gzip
import hashlib
import json
import math
from typing import Any, Mapping

try:
    from app.pdf_layout import (
        LAYOUT_COORDINATE_SYSTEM,
        LAYOUT_SCHEMA_VERSION,
        MAX_LAYOUT_CANONICAL_BYTES,
        PAGE_JOIN_CONTRACT,
    )
except ModuleNotFoundError:
    from services.ingestion.app.pdf_layout import (
        LAYOUT_COORDINATE_SYSTEM,
        LAYOUT_SCHEMA_VERSION,
        MAX_LAYOUT_CANONICAL_BYTES,
        PAGE_JOIN_CONTRACT,
    )
from services.shared.object_storage import OriginalObjectStorage, StoredObject


MAX_EVIDENCE_GLYPHS = 10_000


class LayoutEvidenceIntegrityError(RuntimeError):
    """The cataloged layout could not prove the requested parsed-text range."""


def _finite_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _stored_layout(row: Mapping[str, Any]) -> StoredObject:
    return StoredObject(
        bucket=str(row["layout_bucket"]),
        object_key=str(row["layout_object_key"]),
        object_version_id=str(row["layout_object_version_id"]),
        byte_length=int(row["layout_byte_length"]),
        sha256_hex=str(row["layout_content_sha256"]),
        checksum_sha256_base64=str(row["layout_checksum_sha256_base64"]),
        etag=row["layout_etag"],
        storage_class=row["layout_storage_class"],
        server_side_encryption=row["layout_server_side_encryption"],
        kms_key_id=row["layout_kms_key_id"],
        bucket_key_enabled=row["layout_bucket_key_enabled"],
    )


def _read_canonical_layout(
    row: Mapping[str, Any],
    storage: OriginalObjectStorage,
) -> dict[str, Any]:
    expected_length = int(row["layout_uncompressed_byte_length"])
    if not 0 < expected_length <= MAX_LAYOUT_CANONICAL_BYTES:
        raise LayoutEvidenceIntegrityError("layout canonical length is outside its bound")
    spool = storage.download_verified_to_spool(_stored_layout(row))
    try:
        try:
            with gzip.GzipFile(fileobj=spool, mode="rb") as compressed:
                canonical = compressed.read(expected_length + 1)
        except (EOFError, OSError) as error:
            raise LayoutEvidenceIntegrityError("layout gzip stream is invalid") from error
    finally:
        spool.close()
    if len(canonical) != expected_length:
        raise LayoutEvidenceIntegrityError("layout canonical length does not match its catalog")
    if _sha256_bytes(canonical) != row["layout_canonical_content_sha256"]:
        raise LayoutEvidenceIntegrityError("layout canonical digest does not match its catalog")
    try:
        payload = json.loads(canonical)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise LayoutEvidenceIntegrityError("layout canonical JSON is invalid") from error
    if not isinstance(payload, dict):
        raise LayoutEvidenceIntegrityError("layout canonical root is not an object")
    return payload


def resolve_pdf_visual_target(
    row: Mapping[str, Any],
    storage: OriginalObjectStorage,
) -> dict[str, Any]:
    """Resolve one exact chunk span to verified native PDF glyph quads."""

    if row["layout_status"] != "ready":
        return {
            "status": "unavailable",
            "reason": "The PDF layout artifact is not currently available.",
        }
    start = row["start_char"]
    end = row["end_char"]
    parsed_text = row["parsed_content_text"]
    if not isinstance(start, int) or not isinstance(end, int) or not isinstance(parsed_text, str):
        raise LayoutEvidenceIntegrityError("visual resolution requires an exact parsed range")
    if not 0 <= start < end <= len(parsed_text):
        raise LayoutEvidenceIntegrityError("visual resolution range is invalid")

    payload = _read_canonical_layout(row, storage)
    if payload.get("schema_version") != LAYOUT_SCHEMA_VERSION:
        raise LayoutEvidenceIntegrityError("layout schema version is unsupported")
    if payload.get("coordinate_system") != LAYOUT_COORDINATE_SYSTEM:
        raise LayoutEvidenceIntegrityError("layout coordinate system is unsupported")
    if payload.get("page_join_contract") != PAGE_JOIN_CONTRACT:
        raise LayoutEvidenceIntegrityError("layout page join contract is unsupported")
    source = payload.get("source")
    parsed = payload.get("parsed_text")
    if not isinstance(source, dict) or source.get("content_sha256") != row["original_content_sha256"]:
        raise LayoutEvidenceIntegrityError("layout source digest does not match the original")
    if not isinstance(parsed, dict) or parsed.get("content_sha256") != row["parsed_content_sha256"]:
        raise LayoutEvidenceIntegrityError("layout parsed digest does not match the parsed artifact")
    if parsed.get("length_code_points") != len(parsed_text):
        raise LayoutEvidenceIntegrityError("layout parsed length does not match the parsed artifact")
    if payload.get("producer_profile") != row["layout_producer_profile"]:
        raise LayoutEvidenceIntegrityError("layout producer profile does not match its catalog")

    pages = payload.get("pages")
    if not isinstance(pages, list) or len(pages) != int(row["layout_page_count"]):
        raise LayoutEvidenceIntegrityError("layout page count does not match its catalog")

    covered = [False] * (end - start)
    selected_pages: list[dict[str, Any]] = []
    selected_glyph_count = 0
    for page in pages:
        if not isinstance(page, dict):
            raise LayoutEvidenceIntegrityError("layout page entry is invalid")
        page_start = page.get("parsed_start")
        page_end = page.get("parsed_end")
        if not isinstance(page_start, int) or not isinstance(page_end, int):
            raise LayoutEvidenceIntegrityError("layout page range is invalid")
        page_index = page.get("page_index")
        page_label = page.get("page_label")
        width_points = page.get("width_points")
        height_points = page.get("height_points")
        rotation_degrees = page.get("rotation_degrees")
        crop_box = page.get("crop_box")
        if (
            not isinstance(page_index, int)
            or page_index < 0
            or not isinstance(page_label, str)
            or not page_label
            or not _finite_number(width_points)
            or not _finite_number(height_points)
            or width_points <= 0
            or height_points <= 0
            or rotation_degrees not in {0, 90, 180, 270}
            or not isinstance(crop_box, list)
            or len(crop_box) != 4
            or not all(_finite_number(value) for value in crop_box)
        ):
            raise LayoutEvidenceIntegrityError("layout page geometry is invalid")
        if page_end <= start or page_start >= end:
            continue
        selected_runs: list[dict[str, Any]] = []
        runs = page.get("text_runs")
        if not isinstance(runs, list):
            raise LayoutEvidenceIntegrityError("layout page text runs are invalid")
        for run in runs:
            if not isinstance(run, dict):
                raise LayoutEvidenceIntegrityError("layout text run is invalid")
            run_start = run.get("parsed_start")
            run_end = run.get("parsed_end")
            if not isinstance(run_start, int) or not isinstance(run_end, int):
                raise LayoutEvidenceIntegrityError("layout text run range is invalid")
            if run_end <= start or run_start >= end:
                continue
            glyphs = run.get("glyphs")
            if not isinstance(glyphs, list):
                raise LayoutEvidenceIntegrityError("layout glyph collection is invalid")
            selected_glyphs: list[dict[str, Any]] = []
            for glyph in glyphs:
                if not isinstance(glyph, dict):
                    raise LayoutEvidenceIntegrityError("layout glyph is invalid")
                glyph_start = glyph.get("parsed_start")
                glyph_end = glyph.get("parsed_end")
                glyph_text = glyph.get("text")
                quad = glyph.get("quad")
                if (
                    not isinstance(glyph_start, int)
                    or not isinstance(glyph_end, int)
                    or not isinstance(glyph_text, str)
                    or not isinstance(quad, list)
                    or len(quad) != 8
                    or not all(_finite_number(value) for value in quad)
                    or glyph_start < run_start
                    or glyph_end > run_end
                    or glyph_end <= glyph_start
                    or parsed_text[glyph_start:glyph_end] != glyph_text
                ):
                    raise LayoutEvidenceIntegrityError("layout glyph does not match parsed text")
                if glyph_end <= start or glyph_start >= end:
                    continue
                selected_glyph_count += 1
                if selected_glyph_count > MAX_EVIDENCE_GLYPHS:
                    raise LayoutEvidenceIntegrityError("visual selector exceeds its glyph bound")
                intersection_start = max(start, glyph_start)
                intersection_end = min(end, glyph_end)
                for index in range(intersection_start, intersection_end):
                    covered[index - start] = True
                selected_glyphs.append(
                    {
                        "parsed_start": glyph_start,
                        "parsed_end": glyph_end,
                        "quad": quad,
                    }
                )
            if selected_glyphs:
                selected_runs.append(
                    {
                        "parsed_start": max(start, run_start),
                        "parsed_end": min(end, run_end),
                        "reading_order": run.get("reading_order"),
                        "glyphs": selected_glyphs,
                    }
                )
        if selected_runs:
            selected_pages.append(
                {
                    "page_index": page_index,
                    "page_label": page_label,
                    "width_points": width_points,
                    "height_points": height_points,
                    "rotation_degrees": rotation_degrees,
                    "crop_box": crop_box,
                    "runs": selected_runs,
                }
            )

    for index, character in enumerate(parsed_text[start:end]):
        if character not in {"\n", "\f"} and not covered[index]:
            raise LayoutEvidenceIntegrityError(
                "visual selector does not cover every visible parsed character"
            )
    if not selected_pages:
        raise LayoutEvidenceIntegrityError("visual selector resolved no PDF glyphs")

    return {
        "status": "verified",
        "source": f"urn:certus:pdf-layout-artifact:{row['layout_artifact_id']}",
        "state": {
            "type": "certus:DigestState",
            "sha256": str(row["layout_canonical_content_sha256"]),
        },
        "selector": {
            "type": "certus:PdfGlyphSelector",
            "coordinate_system": LAYOUT_COORDINATE_SYSTEM,
            "offset_unit": "unicodeCodePoint",
            "range_semantics": "zeroBasedHalfOpen",
            "granularity": "nativeGlyphQuad",
            "pages": selected_pages,
        },
    }
