from __future__ import annotations

from typing import Any

from services.shared.embeddings import EmbeddingProfile


def embedding_profile_record(profile: EmbeddingProfile) -> dict[str, Any]:
    normalization_profile = {
        "local": "l2_normalized:v1",
        "openai": "producer_contract:v1",
        "legacy": "unknown:v0",
    }[profile.provider]
    return {
        "identifier": profile.identifier,
        "schema_version": profile.version,
        "provider": profile.provider,
        "model": profile.model,
        "dimensions": profile.dimensions,
        "distance_metric": "cosine",
        "normalization_profile": normalization_profile,
        "input_profile": "plain_text_unprefixed:v1",
        "storage_profile": "pgvector_float32_cosine:v1",
    }


def register_embedding_profile(cursor: Any, profile: EmbeddingProfile) -> None:
    """Idempotently register one canonical immutable producer definition."""
    record = embedding_profile_record(profile)
    cursor.execute(
        """
        INSERT INTO embedding_profiles (
            identifier, schema_version, provider, model, dimensions,
            distance_metric, normalization_profile, input_profile, storage_profile
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (identifier) DO NOTHING
        """,
        (
            record["identifier"],
            record["schema_version"],
            record["provider"],
            record["model"],
            record["dimensions"],
            record["distance_metric"],
            record["normalization_profile"],
            record["input_profile"],
            record["storage_profile"],
        ),
    )
