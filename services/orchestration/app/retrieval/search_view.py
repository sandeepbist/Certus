import re
from typing import Any


WHITESPACE_PATTERN = re.compile(r"\s+")
TOKEN_PATTERN = re.compile(r"[\w-]+", re.UNICODE)


def escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def compact_text(value: Any) -> str:
    return WHITESPACE_PATTERN.sub(" ", value if isinstance(value, str) else "").strip()


def search_snippet(value: Any, query: str, max_length: int = 240) -> str:
    text = compact_text(value)
    if len(text) <= max_length:
        return text

    normalized = text.casefold()
    candidates = [query.strip(), *TOKEN_PATTERN.findall(query)]
    match_index = next(
        (
            normalized.find(candidate.casefold())
            for candidate in candidates
            if len(candidate.strip()) >= 2 and normalized.find(candidate.casefold()) >= 0
        ),
        0,
    )
    start = max(0, match_index - max_length // 3)
    end = min(len(text), start + max_length)
    if end - start < max_length:
        start = max(0, end - max_length)
    snippet = text[start:end].strip()
    return f"{'…' if start > 0 else ''}{snippet}{'…' if end < len(text) else ''}"


def result_count(rows: list[dict[str, Any]]) -> int:
    return int(rows[0].get("total_matches", 0)) if rows else 0

