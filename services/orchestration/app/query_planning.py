from __future__ import annotations

import hashlib
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable, Literal, Mapping
from uuid import UUID

from pydantic import BaseModel, ConfigDict, ValidationError
from app.conversation import validate_conversation_context
from services.shared.document_retrieval import (
    extract_temporal_years,
    infer_temporal_authority,
)


QUERY_PLAN_PROFILE = "certus_deterministic_query_plan:v14"

_UUID_TEXT = (
    r"[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}"
)
_UUID_PATTERN = re.compile(rf"\b{_UUID_TEXT}\b", re.IGNORECASE)
_DOCUMENT_ID_PATTERN = re.compile(
    rf"(?<!\w)(?:(?:in|from|within|for)\s+)?(?:document|doc|file|pdf|report)"
    rf"(?:\s+(?:id|identifier))?\s*(?:is\s+|[=:#]\s*)?({_UUID_TEXT})(?!\w)",
    re.IGNORECASE,
)
_QUOTED_PATTERN = re.compile(r"[\"“]([^\"”]{1,200})[\"”]")
_TITLE_FILTER_PATTERN = re.compile(
    r"(?<!\w)(?:(?:document|doc|file|pdf|report)\s+(?:titled|named)|title)"
    r"\s*(?:(?:is|equals)\s+|[=:]\s*)?[\"“]([^\"”]{1,500})[\"”]",
    re.IGNORECASE,
)
_EXACT_VALUE_PATTERN = re.compile(
    r"(?<![\w.-])(?:[$€£₹]\s*)?[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)"
    r"(?:\.\d+)?(?:\s*(?:%|percent(?:age)?(?:\s+points?)?|"
    r"USD|EUR|GBP|INR))?(?![\w.-])",
    re.IGNORECASE,
)
_BOUND_YEAR = r"((?:1\d{3}|2\d{3}))(?![\d-])"
_YEAR_RANGE_PATTERN = re.compile(
    rf"(?<!\w)(?:between\s+{_BOUND_YEAR}\s+and|from\s+{_BOUND_YEAR}\s+(?:to|through))\s+{_BOUND_YEAR}",
    re.IGNORECASE,
)
_YEAR_BEFORE_PATTERN = re.compile(
    rf"(?<!\w)(?:before|prior\s+to)\s+{_BOUND_YEAR}",
    re.IGNORECASE,
)
_YEAR_AFTER_PATTERN = re.compile(
    rf"(?<!\w)after\s+{_BOUND_YEAR}",
    re.IGNORECASE,
)
_ISO_DATE = r"((?:1\d{3}|2\d{3})-\d{2}-\d{2})"
_ISO_DATE_PATTERN = re.compile(rf"(?<![\d-]){_ISO_DATE}(?![\d-])")
_DATE_RANGE_PATTERN = re.compile(
    rf"(?<!\w)(?:between\s+{_ISO_DATE}\s+and|from\s+{_ISO_DATE}\s+(?:to|through))\s+{_ISO_DATE}",
    re.IGNORECASE,
)
_DATE_BEFORE_PATTERN = re.compile(
    rf"(?<!\w)(?:before|prior\s+to)\s+{_ISO_DATE}",
    re.IGNORECASE,
)
_DATE_AFTER_PATTERN = re.compile(
    rf"(?<!\w)after\s+{_ISO_DATE}",
    re.IGNORECASE,
)
_DATE_AS_OF_PATTERN = re.compile(
    rf"(?<!\w)as\s+of\s+{_ISO_DATE}",
    re.IGNORECASE,
)
_ISO_DATETIME_PATTERN = re.compile(
    rf"(?<![\d-]){_ISO_DATE}(?:[Tt]|\s+)\d{{2}}:\d{{2}}",
)
_ZONED_INSTANT = (
    r"(?:1\d{3}|2\d{3})-\d{2}-\d{2}[Tt]\d{2}:\d{2}:\d{2}"
    r"(?:\.\d{1,6})?(?:[Zz]|[+-]\d{2}:\d{2})"
)
_ZONED_INSTANT_PATTERN = re.compile(
    rf"(?<![\d-])({_ZONED_INSTANT})(?![\d:])",
)
_INSTANT_RANGE_PATTERN = re.compile(
    rf"(?<!\w)(?:between\s+({_ZONED_INSTANT})\s+and|"
    rf"from\s+({_ZONED_INSTANT})\s+(?:to|through))\s+({_ZONED_INSTANT})",
    re.IGNORECASE,
)
_INSTANT_BEFORE_PATTERN = re.compile(
    rf"(?<!\w)(?:before|prior\s+to)\s+({_ZONED_INSTANT})",
    re.IGNORECASE,
)
_INSTANT_AFTER_PATTERN = re.compile(
    rf"(?<!\w)after\s+({_ZONED_INSTANT})",
    re.IGNORECASE,
)
_INSTANT_AS_OF_PATTERN = re.compile(
    rf"(?<!\w)as\s+of\s+({_ZONED_INSTANT})",
    re.IGNORECASE,
)
_STANDALONE_YEAR_PATTERN = re.compile(r"(?<![\w.,-])(?:1\d{3}|2\d{3})(?![\w.,-])")
_FOLLOW_UP_PREFIX_PATTERN = re.compile(
    r"^(?:and\b|also\b|then\b|what\s+about\b|how\s+about\b|"
    r"and\s+what\b|and\s+how\b)",
    re.IGNORECASE,
)
_FOLLOW_UP_REFERENCE_PATTERN = re.compile(
    r"^(?:"
    r"(?:what|why|when|where|who|which|how(?:\s+(?:much|many|long))?)\s+"
    r"(?:(?:(?:did|does|do|is|are|was|were|has|have|had|can|could|would|will)\s+|of\s+))?"
    r"(?:it|its|they|them|their|those|these|the\s+(?:same|former|latter|above|previous\s+one))\b|"
    r"(?:does|do|did|is|are|was|were|has|have|had|can|could|would|will)\s+"
    r"(?:it|they|this|that|those|these)\b|"
    r"(?:explain|summarize|verify|check|compare|expand\s+on|tell\s+me\s+about|show\s+me)\s+"
    r"(?:it|them|this|that|those|these|the\s+(?:same|former|latter|above|previous\s+one))\b"
    r")",
    re.IGNORECASE,
)
_LEADING_REFERENCE_PATTERN = re.compile(
    r"^(?:it|its|they|them|their|this|that|those|these|"
    r"the\s+(?:same|former|latter|above|previous\s+one))\b",
    re.IGNORECASE,
)

_DOCUMENT_TERMS = (
    "document", "documents", "file", "files", "pdf", "report", "paper",
    "source", "sources", "citation", "evidence", "uploaded", "library",
    "archive", "record", "records",
)
_MEMORY_TERMS = (
    "remember", "memory", "memories", "preference", "preferences",
    "i told", "we discussed", "my usual", "about me",
)
_RELATIONSHIP_TERMS = (
    "relationship", "relationships", "related", "relate", "connection",
    "connections", "connected", "depend", "depends", "dependency",
    "affect", "affects", "impact", "impacts", "associated", "link", "linked",
    "knowledge graph", "graph query",
)
_COMPARISON_TERMS = (
    "compare", "comparison", "difference", "differences", "versus", " vs ",
    "contrast",
)
_TEMPORAL_TERMS = (
    "as of", "during", "before", "after", "between", "in year", "from year",
    "historical", "history", "previous", "old", "older", "latest", "current",
)
_ALL_HISTORY_VERSION_TERMS = (
    "all history", "all versions", "retained versions", "version history",
    "historical", "previous version", "previous versions", "older version",
    "older versions", "over time",
)
_EXACT_LOOKUP_TERMS = (
    "what is", "what was", "how much", "when did", "when was", "which value",
    "value of", "exact", "identifier", "id for", "find the",
)
_MULTI_HOP_TERMS = (
    "analyze", "synthesize", "multi-step", "across documents", "across sources",
    "explain in detail",
)
_SELF_CONTAINED_TOOLS = {"create_task", "summarize_document", "graph_query"}


class _StrictPlanModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class QueryConstraints(_StrictPlanModel):
    document_ids: list[str]
    query_document_ids: list[str]
    conversation_inherited_document_ids: list[str]
    product_selected_document_ids: list[str]
    document_scope_source: Literal[
        "all_documents",
        "query_label",
        "conversation_reference",
        "product_selection",
    ]
    titles: list[str]
    query_titles: list[str]
    conversation_inherited_titles: list[str]
    quoted_phrases: list[str]
    years: list[str]
    temporal_authority: Literal["effective", "source", "recorded"]
    year_start: str | None
    year_end: str | None
    time_start: str | None
    time_end_exclusive: str | None
    exact_values: list[str]
    query_requested_version_scope: Literal[
        "all_history",
        "current_requested",
        "as_of_requested",
    ]
    product_selected_version_scope: Literal["auto", "all_history", "current_only"]
    version_scope_source: Literal["default", "query", "product_selection"]
    requested_version_scope: Literal[
        "all_history",
        "current_requested",
        "as_of_requested",
    ]


class QueryPlanEnforcement(_StrictPlanModel):
    authorization_scope: Literal["tenant_and_user_storage_predicates"]
    retrieval_version_scope: Literal[
        "all_retained_versions",
        "current_version_only",
        "as_of_version",
    ]
    temporal_filter_mode: Literal[
        "none",
        "exact_years",
        "year_bounds",
        "time_window",
        "as_of",
    ]
    requested_temporal_filter_applied: bool
    requested_version_filter_applied: bool
    requested_document_id_filter_applied: bool
    product_selection_overrode_query_document_ids: bool
    product_version_scope_overrode_query_scope: bool
    requested_title_filter_applied: bool
    note: str


class QueryBranches(_StrictPlanModel):
    documents: bool
    memories: bool
    graph: bool
    tools: bool
    query_embedding: bool


class QueryRoutingSignals(_StrictPlanModel):
    document_focused: bool
    memory_focused: bool
    relationship_focused: bool
    comparison: bool
    temporal: bool
    exact_lookup: bool
    multi_hop: bool
    self_contained_tool_only: bool


class QueryPlanLimits(_StrictPlanModel):
    target_context_tokens: int
    max_document_results: int
    max_memory_results: int
    max_graph_depth: int


class ConversationResolution(_StrictPlanModel):
    profile: Literal["certus_deterministic_conversation_resolution:v1"]
    applied: bool
    strategy: Literal["none", "contiguous_user_question_chain"]
    source_run_ids: list[str]
    source_question_sha256s: list[str]
    reference_signals: list[str]
    standalone_query: str
    inherited_constraints: list[str]
    permission_scope_broadened: Literal[False]


class QueryPlan(_StrictPlanModel):
    profile: Literal["certus_deterministic_query_plan:v14"]
    intent: Literal[
        "action",
        "comparison",
        "relationship",
        "temporal_lookup",
        "exact_lookup",
        "multi_hop",
        "semantic_lookup",
    ]
    complexity: Literal["simple", "complex"]
    selected_model: str
    original_query_preserved: Literal[True]
    retrieval_query: str
    deterministic_transformations: list[str]
    synthetic_rewrites: list[str]
    conversation_resolution: ConversationResolution
    detected_constraints: QueryConstraints
    enforcement: QueryPlanEnforcement
    branches: QueryBranches
    routing_signals: QueryRoutingSignals
    tool_names: list[str]
    limits: QueryPlanLimits
    steps: list[str]


def _contains_any(normalized_query: str, terms: Iterable[str]) -> bool:
    return any(
        re.search(
            rf"(?<!\w){re.escape(' '.join(term.casefold().split()))}(?!\w)",
            normalized_query,
        )
        for term in terms
    )


def _normalize_query_text(value: str) -> str:
    collapsed = " ".join(value.split()).strip()
    return re.sub(r"\s+([,.;:!?])", r"\1", collapsed)


def _strip_follow_up_scaffold(value: str) -> str:
    fragment = _FOLLOW_UP_PREFIX_PATTERN.sub(" ", value)
    fragment = _normalize_query_text(fragment)
    fragment = _FOLLOW_UP_REFERENCE_PATTERN.sub(" ", fragment)
    fragment = _normalize_query_text(fragment)
    fragment = _LEADING_REFERENCE_PATTERN.sub(" ", fragment)
    return _normalize_query_text(fragment).strip(" ,.;:!?-")


def _strip_prior_constraints(question: str) -> str:
    topic = _mask_matches(
        question,
        (
            _DOCUMENT_ID_PATTERN,
            _TITLE_FILTER_PATTERN,
            _ZONED_INSTANT_PATTERN,
            _ISO_DATE_PATTERN,
        ),
    )
    topic = _STANDALONE_YEAR_PATTERN.sub(" ", topic)
    topic = _EXACT_VALUE_PATTERN.sub(" ", topic)
    topic = _strip_follow_up_scaffold(topic)
    topic = re.sub(
        r"(?<!\w)(?:as\s+of|in|from|during|before|after|between|through|to)\s*$",
        " ",
        topic,
        flags=re.IGNORECASE,
    )
    return _normalize_query_text(topic).strip(" ,.;:!?-")


def _resolve_conversation_reference(
    query: str,
    conversation_context: Mapping[str, Any] | None,
) -> tuple[ConversationResolution, str | None]:
    normalized_query = _normalize_query_text(query)
    empty = ConversationResolution(
        profile="certus_deterministic_conversation_resolution:v1",
        applied=False,
        strategy="none",
        source_run_ids=[],
        source_question_sha256s=[],
        reference_signals=[],
        standalone_query=normalized_query,
        inherited_constraints=[],
        permission_scope_broadened=False,
    )
    if not conversation_context or len(normalized_query.split()) > 32:
        return empty, None
    context = validate_conversation_context(dict(conversation_context))
    if not context["turns"]:
        return empty, None
    signals = _conversation_reference_signals(normalized_query)
    if not signals:
        return empty, None
    turns = context["turns"]
    source_index = len(turns) - 1
    while source_index > 0 and _conversation_reference_signals(
        _normalize_query_text(turns[source_index]["question"])
    ):
        source_index -= 1
    source_turns = turns[source_index:]
    topic_parts = [
        topic
        for turn in source_turns
        if (topic := _strip_prior_constraints(turn["question"]))
    ]
    if not topic_parts:
        return empty, None
    current_fragment = _strip_follow_up_scaffold(normalized_query) or normalized_query
    standalone_query = _normalize_query_text(". ".join([*topic_parts, current_fragment]))
    scope_question = next(
        (
            turn["question"]
            for turn in reversed(source_turns)
            if _DOCUMENT_ID_PATTERN.search(turn["question"])
            or _TITLE_FILTER_PATTERN.search(turn["question"])
        ),
        None,
    )
    return ConversationResolution(
        profile="certus_deterministic_conversation_resolution:v1",
        applied=True,
        strategy="contiguous_user_question_chain",
        source_run_ids=[turn["run_id"] for turn in source_turns],
        source_question_sha256s=[
            hashlib.sha256(turn["question"].encode("utf-8")).hexdigest()
            for turn in source_turns
        ],
        reference_signals=signals,
        standalone_query=standalone_query,
        inherited_constraints=[],
        permission_scope_broadened=False,
    ), scope_question


def _conversation_reference_signals(normalized_query: str) -> list[str]:
    signals: list[str] = []
    if _FOLLOW_UP_PREFIX_PATTERN.search(normalized_query):
        signals.append("continuation_prefix")
    if _FOLLOW_UP_REFERENCE_PATTERN.search(normalized_query):
        signals.append("anaphoric_reference")
    return signals


def _unique_matches(pattern: re.Pattern[str], query: str, limit: int = 10) -> list[str]:
    values: list[str] = []
    seen: set[str] = set()
    for match in pattern.findall(query):
        value = (match if isinstance(match, str) else match[0]).strip()
        canonical = value.casefold()
        if value and canonical not in seen:
            values.append(value)
            seen.add(canonical)
        if len(values) >= limit:
            break
    return values


def _mask_matches(text: str, patterns: Iterable[re.Pattern[str]]) -> str:
    masked = text
    for pattern in patterns:
        masked = pattern.sub(lambda match: " " * len(match.group(0)), masked)
    return masked


def _extract_exact_values(text: str) -> list[str]:
    # Temporal and identifier constraints have their own typed fields. Mask
    # them before numeric extraction so a persisted value anchor never becomes
    # a date component or the numeric suffix of an opaque identifier.
    masked = _mask_matches(
        text,
        (
            _UUID_PATTERN,
            _ZONED_INSTANT_PATTERN,
            _ISO_DATE_PATTERN,
        ),
    )
    masked = re.sub(
        r"(?<![\w.,-])(?:1\d{3}|2\d{3})(?![\w.,-])",
        lambda match: " " * len(match.group(0)),
        masked,
    )
    return _unique_matches(_EXACT_VALUE_PATTERN, masked)


def _extract_year_bounds(text: str) -> tuple[int | None, int | None, bool]:
    range_match = _YEAR_RANGE_PATTERN.search(text)
    if range_match:
        values = [int(value) for value in range_match.groups() if value]
        start, end = values[0], values[-1]
        return (
            (start, end, True)
            if start <= end else (None, None, True)
        )

    before_match = _YEAR_BEFORE_PATTERN.search(text)
    after_match = _YEAR_AFTER_PATTERN.search(text)
    if not before_match and not after_match:
        return None, None, False
    start = int(after_match.group(1)) + 1 if after_match else None
    end = int(before_match.group(1)) - 1 if before_match else None
    if (
        (start is not None and start > 2999)
        or (end is not None and end < 1000)
        or (start is not None and end is not None and start > end)
    ):
        return None, None, True
    return start, end, True


def _date_at_utc(value: str) -> datetime:
    parsed = date.fromisoformat(value)
    if not 1000 <= parsed.year <= 2999:
        raise ValueError("date year must be between 1000 and 2999")
    return datetime(parsed.year, parsed.month, parsed.day, tzinfo=timezone.utc)


def _instant_at_utc(value: str) -> datetime:
    # RFC 3339 reserves -00:00 for an unknown local offset. Treating it as UTC
    # would turn an explicitly uncertain civil time into false precision.
    if value.endswith("-00:00"):
        raise ValueError("timestamp offset must be known")
    normalized = value[:-1] + "+00:00" if value[-1].casefold() == "z" else value
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None or not 1000 <= parsed.year <= 2999:
        raise ValueError("timestamp must have an explicit timezone and supported year")
    normalized_utc = parsed.astimezone(timezone.utc)
    if not 1000 <= normalized_utc.year <= 2999:
        raise ValueError("timestamp UTC instant must have a supported year")
    return normalized_utc


def _checked_add(value: datetime, delta: timedelta) -> datetime | None:
    try:
        return value + delta
    except OverflowError:
        return None


def _extract_instant_window(
    text: str,
) -> tuple[datetime | None, datetime | None, bool]:
    raw_instants = _ZONED_INSTANT_PATTERN.findall(text)
    if not raw_instants:
        return None, None, False
    try:
        parsed_instants = {
            value: _instant_at_utc(value)
            for value in raw_instants
        }
    except ValueError:
        return None, None, True

    range_match = _INSTANT_RANGE_PATTERN.search(text)
    if range_match:
        values = [value for value in range_match.groups() if value]
        start = parsed_instants[values[0]]
        end = _checked_add(parsed_instants[values[-1]], timedelta(microseconds=1))
        if end is None:
            return None, None, True
        return (start, end, True) if start < end else (None, None, True)

    before_match = _INSTANT_BEFORE_PATTERN.search(text)
    after_match = _INSTANT_AFTER_PATTERN.search(text)
    if before_match or after_match:
        start = (
            _checked_add(
                parsed_instants[after_match.group(1)],
                timedelta(microseconds=1),
            )
            if after_match else None
        )
        if after_match and start is None:
            return None, None, True
        end = parsed_instants[before_match.group(1)] if before_match else None
        if start is not None and end is not None and start >= end:
            return None, None, True
        return start, end, True

    as_of_match = _INSTANT_AS_OF_PATTERN.search(text)
    if as_of_match:
        end = _checked_add(
            parsed_instants[as_of_match.group(1)],
            timedelta(microseconds=1),
        )
        if end is None:
            return None, None, True
        return (
            None,
            end,
            True,
        )
    if len(raw_instants) == 1:
        instant = parsed_instants[raw_instants[0]]
        end = _checked_add(instant, timedelta(microseconds=1))
        return (instant, end, True) if end is not None else (None, None, True)
    return None, None, True


def _extract_date_window(
    text: str,
) -> tuple[datetime | None, datetime | None, bool]:
    if _ISO_DATETIME_PATTERN.search(text):
        return None, None, True
    raw_dates = _ISO_DATE_PATTERN.findall(text)
    if not raw_dates:
        return None, None, False
    try:
        parsed_dates = {value: _date_at_utc(value) for value in raw_dates}
    except ValueError:
        return None, None, True

    range_match = _DATE_RANGE_PATTERN.search(text)
    if range_match:
        values = [value for value in range_match.groups() if value]
        start = parsed_dates[values[0]]
        end = _checked_add(parsed_dates[values[-1]], timedelta(days=1))
        if end is None:
            return None, None, True
        return (start, end, True) if start < end else (None, None, True)

    before_match = _DATE_BEFORE_PATTERN.search(text)
    after_match = _DATE_AFTER_PATTERN.search(text)
    if before_match or after_match:
        start = (
            _checked_add(parsed_dates[after_match.group(1)], timedelta(days=1))
            if after_match else None
        )
        if after_match and start is None:
            return None, None, True
        end = parsed_dates[before_match.group(1)] if before_match else None
        if start is not None and end is not None and start >= end:
            return None, None, True
        return start, end, True

    as_of_match = _DATE_AS_OF_PATTERN.search(text)
    if as_of_match:
        end = _checked_add(parsed_dates[as_of_match.group(1)], timedelta(days=1))
        return (None, end, True) if end is not None else (None, None, True)
    if len(raw_dates) == 1:
        start = parsed_dates[raw_dates[0]]
        end = _checked_add(start, timedelta(days=1))
        return (start, end, True) if end is not None else (None, None, True)
    return None, None, True


def validate_query_plan(value: Any) -> QueryPlan | None:
    """Return only the current complete plan; older/invalid plans fail conservatively."""
    if not isinstance(value, Mapping) or value.get("profile") != QUERY_PLAN_PROFILE:
        return None
    try:
        return QueryPlan.model_validate(value)
    except ValidationError:
        return None


def build_query_plan(
    query: str,
    tool_calls: Iterable[Mapping[str, Any]],
    *,
    selected_model: str,
    is_complex: bool,
    selected_document_ids: Iterable[str] = (),
    selected_version_scope: Literal["auto", "all_history", "current_only"] = "auto",
    conversation_context: Mapping[str, Any] | None = None,
) -> QueryPlan:
    """Build a conservative, auditable plan without model-generated rewrites."""
    normalized_original = _normalize_query_text(query)
    resolution, prior_question = _resolve_conversation_reference(
        normalized_original,
        conversation_context,
    )
    planning_query = resolution.standalone_query
    normalized_current = normalized_original.casefold()
    normalized_routing = planning_query.casefold()
    tool_names = [
        str(call.get("tool_name"))
        for call in tool_calls
        if isinstance(call, Mapping) and call.get("tool_name")
    ]
    detected_titles = _unique_matches(_TITLE_FILTER_PATTERN, query, limit=6)
    query_titles = detected_titles if len(detected_titles) <= 5 else []
    current_has_title_clause = bool(_TITLE_FILTER_PATTERN.search(query))
    current_without_title_filters = (
        _TITLE_FILTER_PATTERN.sub(" ", query) if query_titles else query
    )
    quoted_phrases = _unique_matches(_QUOTED_PATTERN, current_without_title_filters)
    identifiers = _unique_matches(_UUID_PATTERN, current_without_title_filters)
    query_document_ids = [
        str(UUID(value))
        for value in _unique_matches(_DOCUMENT_ID_PATTERN, current_without_title_filters)
    ]
    product_selected_document_ids: list[str] = []
    selected_seen: set[str] = set()
    for raw_document_id in selected_document_ids:
        document_id = str(UUID(str(raw_document_id)))
        if document_id not in selected_seen:
            product_selected_document_ids.append(document_id)
            selected_seen.add(document_id)
        if len(product_selected_document_ids) > 10:
            raise ValueError("At most 10 documents can be selected for one query")
    inherited_document_ids: list[str] = []
    inherited_titles: list[str] = []
    if (
        resolution.applied
        and prior_question
        and not product_selected_document_ids
        and not query_document_ids
        and not current_has_title_clause
    ):
        inherited_document_ids = [
            str(UUID(value))
            for value in _unique_matches(_DOCUMENT_ID_PATTERN, prior_question)
        ]
        prior_titles = _unique_matches(_TITLE_FILTER_PATTERN, prior_question, limit=6)
        inherited_titles = prior_titles if len(prior_titles) <= 5 else []
    titles = query_titles or inherited_titles
    document_ids = (
        product_selected_document_ids
        or query_document_ids
        or inherited_document_ids
    )
    document_scope_source = (
        "product_selection"
        if product_selected_document_ids
        else "query_label"
        if query_document_ids
        else "conversation_reference"
        if inherited_document_ids or inherited_titles
        else "all_documents"
    )
    inherited_constraints = []
    if inherited_document_ids:
        inherited_constraints.append("document_ids")
    if inherited_titles:
        inherited_constraints.append("titles")
    resolution = resolution.model_copy(
        update={"inherited_constraints": inherited_constraints}
    )
    retrieval_without_title_filters = (
        _TITLE_FILTER_PATTERN.sub(" ", planning_query)
        if query_titles else planning_query
    )
    query_without_explicit_document_ids = _DOCUMENT_ID_PATTERN.sub(
        " ", retrieval_without_title_filters
    )
    retrieval_query = _normalize_query_text(query_without_explicit_document_ids)
    deterministic_transformations = []
    if query_titles:
        deterministic_transformations.append("remove_explicit_title_clause:v1")
    if _DOCUMENT_ID_PATTERN.search(retrieval_without_title_filters):
        deterministic_transformations.append("remove_explicit_document_id_clause:v1")
    if resolution.applied:
        deterministic_transformations.append("resolve_latest_user_reference:v1")
    if not retrieval_query:
        retrieval_query = " ".join(titles) if titles else query
    query_without_document_ids = _UUID_PATTERN.sub(
        " ", current_without_title_filters
    )
    years = [str(year) for year in extract_temporal_years(query_without_document_ids)]
    temporal_authority = infer_temporal_authority(query_without_document_ids)
    year_start, year_end, year_bounds_requested = _extract_year_bounds(
        query_without_document_ids
    )
    instant_start, instant_end, instant_window_requested = _extract_instant_window(
        query_without_document_ids
    )
    date_start, date_end, date_window_requested = _extract_date_window(
        query_without_document_ids
    )
    time_start, time_end = (
        (instant_start, instant_end)
        if instant_window_requested else (date_start, date_end)
    )
    time_window_requested = instant_window_requested or date_window_requested
    exact_values = _extract_exact_values(query_without_document_ids)

    document_focused = _contains_any(normalized_routing, _DOCUMENT_TERMS)
    memory_focused = _contains_any(normalized_routing, _MEMORY_TERMS)
    relationship_focused = _contains_any(normalized_routing, _RELATIONSHIP_TERMS)
    comparison = _contains_any(normalized_routing, _COMPARISON_TERMS)
    temporal = (
        bool(years)
        or time_window_requested
        or _contains_any(normalized_current, _TEMPORAL_TERMS)
    )
    exact_lookup = bool(identifiers or quoted_phrases or exact_values) or _contains_any(
        normalized_routing,
        _EXACT_LOOKUP_TERMS,
    )
    multi_hop = _contains_any(normalized_routing, _MULTI_HOP_TERMS)
    tool_only = bool(tool_names) and set(tool_names).issubset(_SELF_CONTAINED_TOOLS)

    if tool_names:
        intent = "action"
    elif comparison:
        intent = "comparison"
    elif relationship_focused:
        intent = "relationship"
    elif temporal:
        intent = "temporal_lookup"
    elif exact_lookup:
        intent = "exact_lookup"
    elif multi_hop:
        intent = "multi_hop"
    else:
        intent = "semantic_lookup"

    use_documents = not tool_only
    use_graph = use_documents and relationship_focused
    use_memories = use_documents and (memory_focused or not document_focused)
    needs_embedding = use_documents or use_memories
    document_id_filter_applied = use_documents and bool(document_ids)
    title_filter_applied = use_documents and bool(titles)
    if selected_version_scope not in {"auto", "all_history", "current_only"}:
        raise ValueError("Unsupported product-selected version scope")
    as_of_requested = _contains_any(normalized_current, ("as of",))
    current_requested = _contains_any(normalized_current, ("latest", "current"))
    all_history_requested = _contains_any(normalized_current, _ALL_HISTORY_VERSION_TERMS)
    if as_of_requested:
        query_requested_version_scope = "as_of_requested"
    elif all_history_requested:
        query_requested_version_scope = "all_history"
    elif current_requested:
        query_requested_version_scope = "current_requested"
    else:
        query_requested_version_scope = "all_history"

    # An explicit temporal cutoff remains the most specific user instruction.
    # Otherwise a product control is authoritative over current/latest wording.
    if query_requested_version_scope == "as_of_requested":
        requested_version_scope = "as_of_requested"
        version_scope_source = "query"
    elif selected_version_scope == "current_only":
        requested_version_scope = "current_requested"
        version_scope_source = "product_selection"
    elif selected_version_scope == "all_history":
        requested_version_scope = "all_history"
        version_scope_source = "product_selection"
    else:
        requested_version_scope = query_requested_version_scope
        version_scope_source = (
            "query"
            if query_requested_version_scope == "current_requested"
            or all_history_requested
            else "default"
        )
    current_version_applied = use_documents and requested_version_scope == "current_requested"
    as_of_version_applied = (
        use_documents
        and requested_version_scope == "as_of_requested"
        and not year_bounds_requested
        and (
            (len(years) == 1 and not time_window_requested)
            or (
                not years
                and time_window_requested
                and time_start is None
                and time_end is not None
            )
        )
    )
    if as_of_version_applied:
        temporal_filter_mode = "as_of"
    elif use_documents and time_window_requested and (
        time_start is not None or time_end is not None
    ):
        temporal_filter_mode = "time_window"
    elif use_documents and year_bounds_requested and (
        year_start is not None or year_end is not None
    ) and not time_window_requested:
        temporal_filter_mode = "year_bounds"
    elif (
        use_documents
        and years
        and not as_of_requested
        and not year_bounds_requested
        and not time_window_requested
    ):
        temporal_filter_mode = "exact_years"
    else:
        temporal_filter_mode = "none"
    temporal_filter_applied = temporal_filter_mode != "none"
    if current_version_applied:
        retrieval_version_scope = "current_version_only"
    elif as_of_version_applied:
        retrieval_version_scope = "as_of_version"
    else:
        retrieval_version_scope = "all_retained_versions"

    steps: list[str] = []
    if needs_embedding:
        steps.append("Compute one query embedding for compatible retrieval branches")
    if use_documents:
        steps.append("Run tenant-scoped vector and full-text document retrieval")
    if use_memories:
        steps.append("Run tenant-scoped long-term memory retrieval")
    if use_graph:
        steps.append("Run tenant-scoped Neo4j relationship traversal")
    if tool_names:
        steps.append(f"Dispatch {len(tool_names)} validated workspace tool call(s)")
    steps.append("Validate and render atomic claims from authorized evidence")

    return QueryPlan(
        profile=QUERY_PLAN_PROFILE,
        intent=intent,
        complexity="complex" if is_complex else "simple",
        selected_model=selected_model,
        original_query_preserved=True,
        retrieval_query=retrieval_query,
        deterministic_transformations=deterministic_transformations,
        synthetic_rewrites=[],
        conversation_resolution=resolution,
        detected_constraints=QueryConstraints(
            document_ids=document_ids,
            query_document_ids=query_document_ids,
            conversation_inherited_document_ids=inherited_document_ids,
            product_selected_document_ids=product_selected_document_ids,
            document_scope_source=document_scope_source,
            titles=titles,
            query_titles=query_titles,
            conversation_inherited_titles=inherited_titles,
            quoted_phrases=quoted_phrases,
            years=years,
            temporal_authority=temporal_authority,
            year_start=str(year_start) if year_start is not None else None,
            year_end=str(year_end) if year_end is not None else None,
            time_start=time_start.isoformat() if time_start is not None else None,
            time_end_exclusive=(
                time_end.isoformat() if time_end is not None else None
            ),
            exact_values=exact_values,
            query_requested_version_scope=query_requested_version_scope,
            product_selected_version_scope=selected_version_scope,
            version_scope_source=version_scope_source,
            requested_version_scope=requested_version_scope,
        ),
        enforcement=QueryPlanEnforcement(
            authorization_scope="tenant_and_user_storage_predicates",
            retrieval_version_scope=retrieval_version_scope,
            temporal_filter_mode=temporal_filter_mode,
            requested_temporal_filter_applied=temporal_filter_applied,
            requested_version_filter_applied=(
                current_version_applied or as_of_version_applied
            ),
            requested_document_id_filter_applied=document_id_filter_applied,
            product_selection_overrode_query_document_ids=(
                bool(product_selected_document_ids)
                and query_document_ids != product_selected_document_ids
            ),
            product_version_scope_overrode_query_scope=(
                version_scope_source == "product_selection"
                and requested_version_scope != query_requested_version_scope
            ),
            requested_title_filter_applied=title_filter_applied,
            note=(
                "Product-selected document identifiers are the authoritative allow-list; "
                "query-labeled identifiers cannot broaden it. Otherwise, explicit document "
                "identifiers and labeled exact titles are enforced by document retrieval when "
                "marked applied. A short referential follow-up may inherit the latest user "
                "turn's explicit document identifiers or title only when the current turn "
                "supplies no document scope; assistant output is never reused. Detected "
                "four-digit years are "
                "enforced against source "
                "time with recorded-time fallback; explicit before/after/range operators "
                "become bounded year, strict ISO-date, or timezone-aware instant windows. "
                "A single-cutoff as-of "
                "request selects the latest eligible version by the requested time "
                "authority. Explicit "
                "current/latest requests are limited to each document's logical current "
                "version; the original query is unchanged."
            ),
        ),
        branches=QueryBranches(
            documents=use_documents,
            memories=use_memories,
            graph=use_graph,
            tools=bool(tool_names),
            query_embedding=needs_embedding,
        ),
        routing_signals=QueryRoutingSignals(
            document_focused=document_focused,
            memory_focused=memory_focused,
            relationship_focused=relationship_focused,
            comparison=comparison,
            temporal=temporal,
            exact_lookup=exact_lookup,
            multi_hop=multi_hop,
            self_contained_tool_only=tool_only,
        ),
        tool_names=tool_names,
        limits=QueryPlanLimits(
            target_context_tokens=1400,
            max_document_results=5,
            max_memory_results=5,
            max_graph_depth=2,
        ),
        steps=[f"{index}. {step}" for index, step in enumerate(steps, start=1)],
    )
