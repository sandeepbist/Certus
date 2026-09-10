import hashlib
from typing import Any, Mapping


EXACT_TEXT_LOCATOR_PROFILE = "unicode_code_point:zero_based_half_open:v1"
LEGACY_TEXT_LOCATOR_PROFILE = "legacy_unavailable:v0"


class EvidenceIntegrityError(RuntimeError):
    """Raised when stored evidence lineage cannot be verified exactly."""


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def build_evidence_envelope(
    row: Mapping[str, Any],
    visual_target: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a fail-closed selector envelope from one tenant-scoped DB row."""

    version_hash = str(row["version_content_hash"])
    original_hash = str(row["original_content_sha256"])
    if version_hash != original_hash:
        raise EvidenceIntegrityError("source version and original object digests disagree")

    locator_status = str(row["text_locator_status"])
    locator_profile = str(row["text_locator_profile"])
    text_target: dict[str, Any] | None = None
    unavailable_reason: str | None = None

    if locator_status == "exact":
        if locator_profile != EXACT_TEXT_LOCATOR_PROFILE:
            raise EvidenceIntegrityError("exact text locator has an unknown profile")
        if row["parsed_status"] != "ready":
            raise EvidenceIntegrityError("exact text locator has no ready parsed artifact")
        start = row["start_char"]
        end = row["end_char"]
        if not isinstance(start, int) or not isinstance(end, int) or not 0 <= start < end:
            raise EvidenceIntegrityError("exact text locator bounds are invalid")
        exact = row["resolved_quote"]
        if not isinstance(exact, str) or exact != row["chunk_content"]:
            raise EvidenceIntegrityError("chunk content does not resolve at its text locator")
        prefix = row["resolved_prefix"]
        suffix = row["resolved_suffix"]
        if not isinstance(prefix, str) or not isinstance(suffix, str):
            raise EvidenceIntegrityError("text quote context could not be resolved")
        text_target = {
            "source": f"urn:certus:parsed-artifact:{row['parsed_artifact_id']}",
            "state": {
                "type": "certus:DigestState",
                "sha256": str(row["parsed_content_sha256"]),
            },
            "selector": [
                {
                    "type": "TextPositionSelector",
                    "start": start,
                    "end": end,
                    "unit": "unicodeCodePoint",
                },
                {
                    "type": "TextQuoteSelector",
                    "exact": exact,
                    "prefix": prefix,
                    "suffix": suffix,
                },
            ],
            "quote_sha256": _sha256_text(exact),
        }
        resolution_status = "verified"
    elif locator_status == "unavailable":
        if locator_profile != LEGACY_TEXT_LOCATOR_PROFILE:
            raise EvidenceIntegrityError("unavailable text locator has an unknown profile")
        unavailable_reason = str(row["text_locator_unavailable_reason"] or "")
        if not unavailable_reason:
            raise EvidenceIntegrityError("unavailable text locator has no reason")
        resolution_status = "unavailable"
    else:
        raise EvidenceIntegrityError("text locator has an unknown status")

    source_mime_type = str(row["source_mime_type"]).split(";", 1)[0].strip().lower()
    if visual_target is None:
        visual_target = {
            "status": "unavailable" if source_mime_type == "application/pdf" else "not_applicable",
            "reason": (
                "Exact PDF region geometry has not been produced for this parsed artifact."
                if source_mime_type == "application/pdf"
                else "Visual region selectors do not apply to this source format."
            ),
        }

    return {
        "schema_version": 1,
        "resolution_status": resolution_status,
        "unavailable_reason": unavailable_reason,
        "evidence_handle": str(row["chunk_id"]),
        "support_scope": "retrieved_context_not_claim_aligned",
        "document": {
            "id": str(row["document_id"]),
            "title": str(row["document_title"]),
            "version_id": str(row["document_version_id"]),
            "version_number": int(row["version_number"]),
            "is_current_version": bool(row["is_current_version"]),
            "source_time": row["source_time"],
            "recorded_at": row["recorded_at"],
        },
        "lineage": {
            "derivation_id": str(row["derivation_id"]),
            "parsed_artifact_id": str(row["parsed_artifact_id"]),
            "source_object_id": str(row["source_object_id"]),
            "layout_artifact_id": (
                str(row["layout_artifact_id"])
                if row.get("layout_artifact_id")
                else None
            ),
            "text_locator_status": locator_status,
            "text_locator_profile": locator_profile,
            "parser_profile": row["parser_profile"],
            "chunker_profile": row["chunker_profile"],
        },
        "text_target": text_target,
        "visual_target": visual_target,
        "source_object": {
            "id": str(row["source_object_id"]),
            "status": str(row["original_status"]),
            "filename": str(row["original_filename"]),
            "mime_type": source_mime_type,
            "byte_length": int(row["original_byte_length"]),
            "sha256": original_hash,
            "download_available": row["original_status"] == "available",
        },
        "display": {
            "page_number_hint": row["page_number"],
            "section_title": row["section_title"] or "",
        },
    }
