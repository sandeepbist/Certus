from __future__ import annotations

import json
from typing import Any, Dict, List

import psycopg2
from psycopg2.extras import RealDictCursor

from app.core.config import settings
from app.core.db import acquire_db_connection, release_db_connection
from app.grounding import (
    build_evidence_manifest,
    build_evidence_pack,
    has_valid_canonical_sha256,
)


class ChatReplayUnavailable(Exception):
    """Raised when retained provenance cannot reconstruct authorized evidence."""


def reconstruct_frozen_evidence(
    manifest: Dict[str, Any],
    tenant_id: str,
    user_id: str,
    database_url: str,
) -> Dict[str, Any]:
    """Resolve an ordered manifest without new retrieval or external tool calls."""
    if (
        manifest.get("profile") != "certus_typed_evidence_manifest:v1"
        or not has_valid_canonical_sha256(manifest)
        or not isinstance(manifest.get("sources"), list)
    ):
        raise ChatReplayUnavailable("This run has no valid frozen evidence manifest")

    chunks: List[Dict[str, Any]] = []
    memories: List[Dict[str, Any]] = []
    graph_triples: List[str] = []
    tool_results: List[Dict[str, Any]] = []
    pooled_connection = database_url == settings.DATABASE_URL
    connection = (
        acquire_db_connection()
        if pooled_connection
        else psycopg2.connect(database_url)
    )
    try:
        with connection.cursor(cursor_factory=RealDictCursor) as cursor:
            for source in manifest["sources"]:
                if not isinstance(source, dict):
                    raise ChatReplayUnavailable("The frozen evidence manifest is malformed")
                source_kind = source.get("source_kind")
                locator = source.get("locator") if isinstance(source.get("locator"), dict) else {}
                if source_kind == "document":
                    cursor.execute(
                        """
                        SELECT chunk.content
                        FROM chunks AS chunk
                        JOIN document_derivations AS derivation
                          ON derivation.id = chunk.derivation_id
                         AND derivation.document_id = chunk.document_id
                         AND derivation.document_version_id = chunk.document_version_id
                        WHERE chunk.id = %s
                          AND chunk.document_id = %s
                          AND chunk.document_version_id = %s
                          AND chunk.derivation_id = %s
                          AND derivation.input_parsed_artifact_id = %s
                          AND chunk.tenant_id = %s
                          AND chunk.user_id = %s
                        """,
                        (
                            locator.get("chunk_id"),
                            locator.get("document_id"),
                            locator.get("document_version_id"),
                            locator.get("derivation_id"),
                            locator.get("parsed_artifact_id"),
                            tenant_id,
                            user_id,
                        ),
                    )
                    row = cursor.fetchone()
                    if not row:
                        raise ChatReplayUnavailable(
                            "A frozen document source is no longer retained or authorized"
                        )
                    chunks.append({
                        **locator,
                        "content": row["content"],
                        "score": locator.get("retrieval_score"),
                    })
                elif source_kind == "memory":
                    memories.append({
                        "id": locator.get("memory_id"),
                        "category": locator.get("category"),
                        "fact": source.get("content_snapshot"),
                    })
                elif source_kind == "graph":
                    graph_triples.append(str(source.get("content_snapshot") or ""))
                elif source_kind == "tool":
                    try:
                        tool_result = json.loads(str(source.get("content_snapshot") or ""))
                    except json.JSONDecodeError as error:
                        raise ChatReplayUnavailable(
                            "A frozen tool result is not valid canonical JSON"
                        ) from error
                    if not isinstance(tool_result, dict):
                        raise ChatReplayUnavailable("A frozen tool result is malformed")
                    tool_results.append(tool_result)
                else:
                    raise ChatReplayUnavailable("The frozen evidence source kind is unsupported")
    finally:
        if pooled_connection:
            release_db_connection(connection)
        else:
            connection.close()

    rebuilt_pack = build_evidence_pack(chunks, memories, graph_triples, tool_results)
    if build_evidence_manifest(rebuilt_pack) != manifest:
        raise ChatReplayUnavailable(
            "The retained sources no longer reproduce the frozen generation evidence pack"
        )
    return {
        "retrieved_chunks": chunks,
        "retrieved_memories": memories,
        "graph_context": {
            "graph_triples": graph_triples,
            "relationships_count": len(graph_triples),
        },
        "tool_results": tool_results,
    }
