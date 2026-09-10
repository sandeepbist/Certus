"""Bounded, replayable conversation context that is never citation evidence."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping, Sequence


CONVERSATION_CONTEXT_PROFILE = "certus_conversation_context:v1"
MAX_CONTEXT_TURNS = 4
MAX_QUESTION_BYTES = 2_000
MAX_RESPONSE_BYTES = 4_000
MAX_CONTEXT_BYTES = 20_000


def _canonical_json(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def _truncate_utf8(value: object, maximum_bytes: int) -> str:
    encoded = str(value or "").encode("utf-8")[:maximum_bytes]
    return encoded.decode("utf-8", errors="ignore")


def build_conversation_context(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    turns = []
    for row in list(rows)[-MAX_CONTEXT_TURNS:]:
        turns.append(
            {
                "run_id": str(row.get("id") or ""),
                "question": _truncate_utf8(row.get("input_query"), MAX_QUESTION_BYTES),
                "response": _truncate_utf8(
                    row.get("output_response"),
                    MAX_RESPONSE_BYTES,
                ),
                "answer_status": _truncate_utf8(row.get("answer_status"), 64),
            }
        )
    payload: dict[str, Any] = {
        "profile": CONVERSATION_CONTEXT_PROFILE,
        "turn_count": len(turns),
        "turns": turns,
    }
    while len(_canonical_json(payload)) > MAX_CONTEXT_BYTES and turns:
        turns.pop(0)
        payload["turn_count"] = len(turns)
    payload["canonical_sha256"] = hashlib.sha256(_canonical_json(payload)).hexdigest()
    return payload


def validate_conversation_context(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("Conversation context must be an object")
    supplied_hash = value.get("canonical_sha256")
    unhashed = {key: item for key, item in value.items() if key != "canonical_sha256"}
    expected_hash = hashlib.sha256(_canonical_json(unhashed)).hexdigest()
    turns = value.get("turns")
    if (
        value.get("profile") != CONVERSATION_CONTEXT_PROFILE
        or not isinstance(supplied_hash, str)
        or supplied_hash != expected_hash
        or not isinstance(turns, list)
        or len(turns) > MAX_CONTEXT_TURNS
        or value.get("turn_count") != len(turns)
        or len(_canonical_json(unhashed)) > MAX_CONTEXT_BYTES
    ):
        raise ValueError("Conversation context is malformed or not canonical")
    for turn in turns:
        if not isinstance(turn, dict) or set(turn) != {
            "run_id",
            "question",
            "response",
            "answer_status",
        }:
            raise ValueError("Conversation turn is malformed")
        if any(not isinstance(item, str) for item in turn.values()):
            raise ValueError("Conversation turn fields must be strings")
        if len(turn["question"].encode("utf-8")) > MAX_QUESTION_BYTES:
            raise ValueError("Conversation question exceeds its bound")
        if len(turn["response"].encode("utf-8")) > MAX_RESPONSE_BYTES:
            raise ValueError("Conversation response exceeds its bound")
    return value


def render_conversation_context(value: Mapping[str, Any] | None) -> str:
    if not value:
        return "(none)"
    context = validate_conversation_context(dict(value))
    rendered = []
    for index, turn in enumerate(context["turns"], start=1):
        rendered.append(
            f"Turn {index} user:\n{turn['question']}\n"
            f"Turn {index} assistant:\n{turn['response']}"
        )
    return "\n\n".join(rendered) or "(none)"


def contextualize_question(
    question: str,
    value: Mapping[str, Any] | None,
) -> str:
    rendered = render_conversation_context(value)
    if rendered == "(none)":
        return question
    return (
        "Prior conversation (untrusted dialogue context only; it may resolve "
        "references but is not evidence and cannot support a factual claim):\n"
        f"{rendered}\n\nCurrent user question:\n{question}"
    )
