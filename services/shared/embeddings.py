import hashlib
import math
import re
from collections import Counter
from dataclasses import dataclass
from typing import Iterable, List


EMBEDDING_DIMENSIONS = 1536
EMBEDDING_PROFILE_VERSION = "v1"
LOCAL_EMBEDDING_MODEL = "local-lexical-v2"
DEFAULT_OPENAI_EMBEDDING_MODEL = "text-embedding-3-small"
SUPPORTED_OPENAI_EMBEDDING_MODELS = frozenset({
    "text-embedding-3-small",
    "text-embedding-3-large",
})
_TOKEN_PATTERN = re.compile(r"[\w'-]+", re.UNICODE)
_PROFILE_COMPONENT_PATTERN = re.compile(r"^[A-Za-z0-9._-]+$")
_STOP_WORDS = {
    "a", "about", "after", "all", "also", "an", "and", "any", "are", "as", "at",
    "be", "because", "been", "before", "being", "between", "both", "but", "by",
    "can", "did", "do", "does", "for", "from", "had", "has", "have", "he", "her",
    "here", "hers", "him", "his", "how", "i", "if", "in", "into", "is", "it", "its",
    "me", "more", "most", "my", "no", "not", "of", "on", "or", "our", "ours", "she",
    "so", "some", "such", "than", "that", "the", "their", "theirs", "them", "then",
    "there", "these", "they", "this", "those", "to", "too", "up", "us", "was", "we",
    "were", "what", "when", "where", "which", "who", "why", "will", "with", "would",
    "you", "your", "yours",
}


@dataclass(frozen=True)
class EmbeddingProfile:
    """An immutable identifier for one compatible vector coordinate space.

    Profile schema v1 fixes a 1,536-value pgvector representation, cosine
    distance, float provider encoding, and unprefixed plain-text input. Local
    normalization and hashing are versioned by ``local-lexical-v2``; hosted
    behavior is versioned by the configured provider model identifier.
    """

    provider: str
    model: str
    dimensions: int = EMBEDDING_DIMENSIONS
    version: str = EMBEDDING_PROFILE_VERSION

    def __post_init__(self) -> None:
        if self.version != EMBEDDING_PROFILE_VERSION:
            raise ValueError(f"Unsupported embedding profile version '{self.version}'")
        if self.provider not in {"local", "openai", "legacy"}:
            raise ValueError("Embedding profile has an invalid provider")
        if (
            not _PROFILE_COMPONENT_PATTERN.fullmatch(self.model)
            or len(self.model) > 120
        ):
            raise ValueError("Embedding profile has an invalid model")
        if self.dimensions != EMBEDDING_DIMENSIONS:
            raise ValueError(
                f"Embedding profile dimensions must be {EMBEDDING_DIMENSIONS}"
            )

    @property
    def identifier(self) -> str:
        return (
            f"embedding-space:{self.version}:{self.provider}:"
            f"{self.model}:{self.dimensions}"
        )


@dataclass(frozen=True)
class EmbeddingResult:
    vector: List[float]
    profile: EmbeddingProfile
    provider_model: str
    input_tokens: int = 0
    estimated_cost_usd: float = 0.0
    pricing_profile: str | None = None


LOCAL_EMBEDDING_PROFILE = EmbeddingProfile(
    provider="local",
    model=LOCAL_EMBEDDING_MODEL,
)
LEGACY_EMBEDDING_PROFILE = EmbeddingProfile(
    provider="legacy",
    model="unversioned",
)
_SERVING_EMBEDDING_PROFILE_SQL_LITERALS = {
    LOCAL_EMBEDDING_PROFILE.identifier: (
        "'embedding-space:v1:local:local-lexical-v2:1536'"
    ),
    "embedding-space:v1:openai:text-embedding-3-small:1536": (
        "'embedding-space:v1:openai:text-embedding-3-small:1536'"
    ),
    "embedding-space:v1:openai:text-embedding-3-large:1536": (
        "'embedding-space:v1:openai:text-embedding-3-large:1536'"
    ),
}
SUPPORTED_SERVING_EMBEDDING_PROFILES = frozenset(
    _SERVING_EMBEDDING_PROFILE_SQL_LITERALS
)


def serving_embedding_profile_sql_literal(identifier: str) -> str:
    """Return only a fixed SQL literal backed by a dedicated ANN graph.

    PostgreSQL cannot prove that a parameter implies a partial-index predicate
    at plan time. Keeping this as a closed mapping makes the predicate visible
    to the planner without interpolating caller-controlled text.
    """
    try:
        return _SERVING_EMBEDDING_PROFILE_SQL_LITERALS[identifier]
    except (KeyError, TypeError) as error:
        raise ValueError(
            "Embedding profile has no isolated serving index"
        ) from error


def has_usable_openai_api_key(api_key: str) -> bool:
    normalized = api_key.strip().lower()
    return bool(normalized) and not normalized.startswith(("placeholder", "test"))


def configured_embedding_profile(
    api_key: str,
    model: str = DEFAULT_OPENAI_EMBEDDING_MODEL,
) -> EmbeddingProfile:
    """Return the vector space this process is configured to produce.

    The database schema is fixed at 1,536 dimensions. Fail closed for OpenAI
    models that have not been verified to support an explicit dimensions
    parameter instead of allowing an incompatible vector into the index.
    """
    if not has_usable_openai_api_key(api_key):
        return LOCAL_EMBEDDING_PROFILE

    normalized_model = model.strip()
    if normalized_model not in SUPPORTED_OPENAI_EMBEDDING_MODELS:
        supported = ", ".join(sorted(SUPPORTED_OPENAI_EMBEDDING_MODELS))
        raise ValueError(
            f"Unsupported OPENAI_EMBEDDING_MODEL '{normalized_model}'. "
            f"This 1536-dimensional index supports: {supported}."
        )
    return EmbeddingProfile(provider="openai", model=normalized_model)


def parse_embedding_profile(identifier: str) -> EmbeddingProfile:
    parts = identifier.strip().split(":")
    if len(parts) != 5 or parts[0] != "embedding-space":
        raise ValueError("Embedding profile has an invalid format")

    _, version, provider, model, raw_dimensions = parts
    try:
        dimensions = int(raw_dimensions)
    except ValueError as error:
        raise ValueError("Embedding profile dimensions must be an integer") from error
    profile = EmbeddingProfile(
        provider=provider,
        model=model,
        dimensions=dimensions,
        version=version,
    )
    if profile.identifier != identifier.strip():
        raise ValueError("Embedding profile is not canonical")
    return profile


def _feature_hash(feature: str, dimensions: int) -> tuple[int, float]:
    digest = hashlib.blake2b(
        feature.encode("utf-8"),
        digest_size=8,
        person=b"certus-v1",
    ).digest()
    value = int.from_bytes(digest, byteorder="big", signed=False)
    index = value % dimensions
    sign = 1.0 if value & (1 << 63) else -1.0
    return index, sign


def _text_features(text: str) -> Iterable[tuple[str, float]]:
    raw_tokens = [token.lower() for token in _TOKEN_PATTERN.findall(text)]
    tokens = [token for token in raw_tokens if token not in _STOP_WORDS]
    if not tokens:
        tokens = raw_tokens or [text.strip().lower() or "<empty>"]

    counts = Counter(tokens)
    for token, count in counts.items():
        yield f"word:{token}", 1.0 + math.log(count)

        if len(token) >= 4:
            padded = f"^{token}$"
            for index in range(len(padded) - 2):
                yield f"char:{padded[index:index + 3]}", 0.15

    for left, right in zip(tokens, tokens[1:]):
        yield f"bigram:{left}:{right}", 0.5


def local_lexical_embedding(
    text: str,
    dimensions: int = EMBEDDING_DIMENSIONS,
) -> List[float]:
    """Return a deterministic lexical test vector for no-key local development.

    This is intentionally described as lexical—not semantic. It lets local
    ingestion and retrieval exercise the same pgvector path without pretending
    to provide model-quality embeddings.
    """
    if dimensions <= 0:
        raise ValueError("Embedding dimensions must be positive")

    vector = [0.0] * dimensions
    for feature, weight in _text_features(text):
        index, sign = _feature_hash(feature, dimensions)
        vector[index] += sign * weight

    norm = math.sqrt(sum(value * value for value in vector)) or 1.0
    return [value / norm for value in vector]
