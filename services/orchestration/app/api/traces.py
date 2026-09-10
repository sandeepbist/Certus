import os
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from typing import Literal, Optional
from app.core.db import get_db_cursor
from app.agents.graph import (
    ChatBudgetExceeded,
    ChatRunConflict,
    MultiAgentOrchestrator,
    has_openai_api_key,
)
from app.replay import ChatReplayUnavailable
from app.core.identity import RequestIdentity, require_request_identity

router = APIRouter(prefix="", tags=["Agent Traces & Replay"])


class TraceReplayRequest(BaseModel):
    override_model: Optional[str] = None
    mode: Literal["frozen_evidence", "fresh_retrieval"] = "frozen_evidence"


def replay_models() -> list[str]:
    if not has_openai_api_key():
        return ["auto"]
    return [
        "auto",
        os.getenv("OPENAI_FAST_MODEL", "gpt-5.4-mini"),
        os.getenv("OPENAI_REASONING_MODEL", "gpt-5.5"),
    ]


def _product_scope_from_plan(plan: object) -> tuple[list[str], str]:
    if not isinstance(plan, dict):
        return [], "auto"
    constraints = plan.get("detected_constraints")
    if not isinstance(constraints, dict):
        return [], "auto"

    raw_document_ids = constraints.get("product_selected_document_ids")
    document_ids: list[str] = []
    if isinstance(raw_document_ids, list):
        for value in raw_document_ids[:10]:
            try:
                canonical = str(uuid.UUID(str(value)))
            except (ValueError, AttributeError, TypeError):
                continue
            if canonical not in document_ids:
                document_ids.append(canonical)

    raw_version_scope = constraints.get("product_selected_version_scope")
    version_scope = (
        raw_version_scope
        if raw_version_scope in {"auto", "all_history", "current_only"}
        else "auto"
    )
    return document_ids, version_scope


def _trace_for_client(row) -> dict:
    trace = dict(row)
    evidence_manifest = trace.get("evidence_manifest")
    sources = (
        evidence_manifest.get("sources", [])
        if isinstance(evidence_manifest, dict)
        else []
    )
    trace["evidence_manifest"] = {
        "profile": evidence_manifest.get("profile", "legacy_unavailable:v0"),
        "source_count": evidence_manifest.get("source_count", 0),
        "canonical_sha256": evidence_manifest.get("canonical_sha256"),
        "rendered_pack_sha256": evidence_manifest.get("rendered_pack_sha256"),
        "source_kind_counts": {
            kind: sum(
                1 for source in sources
                if isinstance(source, dict) and source.get("source_kind") == kind
            )
            for kind in ("document", "memory", "graph", "tool")
        },
    } if isinstance(evidence_manifest, dict) else {
        "profile": "legacy_unavailable:v0",
        "source_count": 0,
    }
    generation_profile = trace.get("generation_profile")
    allowed_generation_fields = {
        "profile", "execution_mode", "provider", "api", "requested_model",
        "returned_model", "model_revision_locked", "provider_created_at",
        "service_tier", "store", "max_output_tokens", "timeout_seconds",
        "max_retries", "prompt_profile", "instructions_sha256", "input_sha256",
        "schema_name", "schema_sha256", "validator_profile", "grounding_profile",
        "evidence_manifest_sha256", "failure_type", "canonical_sha256",
    }
    trace["generation_profile"] = {
        key: value
        for key, value in generation_profile.items()
        if key in allowed_generation_fields
    } if isinstance(generation_profile, dict) else {
        "profile": "legacy_unavailable:v0",
    }
    conversation_context = trace.get("conversation_context")
    trace["conversation_context"] = {
        "profile": conversation_context.get("profile"),
        "turn_count": conversation_context.get("turn_count", 0),
        "canonical_sha256": conversation_context.get("canonical_sha256"),
    } if isinstance(conversation_context, dict) else {
        "profile": "legacy_unavailable:v0",
        "turn_count": 0,
    }
    return trace

@router.get("/traces")
def list_traces(
    limit: int = Query(default=50, ge=1, le=100),
    page_cursor: uuid.UUID | None = Query(default=None, alias="cursor"),
    q: Optional[str] = Query(default=None, max_length=500),
    model: Optional[str] = Query(default=None, max_length=50),
    status: Optional[str] = Query(
        default=None,
        pattern="^(running|completed|failed|timeout|interrupted)$",
    ),
    identity: RequestIdentity = Depends(require_request_identity),
):
    filters = ["tenant_id = %s", "user_id = %s"]
    params: list = [identity.tenant_id, identity.user_id]
    if q and q.strip():
        filters.append("search_vector @@ websearch_to_tsquery('english', %s)")
        params.append(q.strip())
    if model:
        filters.append("model_used = %s")
        params.append(model)
    if status:
        filters.append("status = %s")
        params.append(status)
    where_clause = " AND ".join(filters)

    with get_db_cursor() as cursor:
        if page_cursor is not None:
            cursor.execute(
                f"""
                SELECT created_at, id
                FROM agent_runs
                WHERE id = %s AND {where_clause}
                """,
                [str(page_cursor), *params],
            )
            anchor = cursor.fetchone()
            if not anchor:
                raise HTTPException(
                    status_code=422,
                    detail="The trace page cursor is invalid.",
                )
        else:
            anchor = None

        page_filter = ""
        page_params = list(params)
        if anchor:
            page_filter = "AND (created_at, id) < (%s, %s)"
            page_params.extend([anchor["created_at"], anchor["id"]])
        page_params.append(limit + 1)
        cursor.execute(
            f"""
            SELECT id, input_query, model_used, total_tokens, estimated_cost_usd,
                   latency_ms, eval_score, answer_status, grounding_profile,
                   replay_of_run_id, replay_mode, status, created_at, completed_at
            FROM agent_runs
            WHERE {where_clause}
              {page_filter}
            ORDER BY created_at DESC, id DESC
            LIMIT %s
            """,
            page_params,
        )
        rows = cursor.fetchall()
        has_more = len(rows) > limit
        traces = [dict(row) for row in rows[:limit]]
        cursor.execute(
            """
            SELECT DISTINCT model_used
            FROM agent_runs
            WHERE tenant_id = %s AND user_id = %s AND model_used IS NOT NULL
            ORDER BY model_used
            LIMIT 100
            """,
            (identity.tenant_id, identity.user_id),
        )
        available_models = [row["model_used"] for row in cursor.fetchall()]
        return {
            "traces": traces,
            "pagination": {
                "limit": limit,
                "next_cursor": str(traces[-1]["id"]) if has_more else None,
            },
            "available_models": available_models,
        }

@router.get("/traces/{run_id}")
def get_trace_detail(
    run_id: str,
    identity: RequestIdentity = Depends(require_request_identity),
):
    with get_db_cursor() as cursor:
        cursor.execute(
            "SELECT * FROM agent_runs WHERE id = %s AND tenant_id = %s AND user_id = %s",
            (run_id, identity.tenant_id, identity.user_id),
        )
        trace = cursor.fetchone()
        if not trace:
            raise HTTPException(status_code=404, detail=f"Trace run '{run_id}' not found")
        return {
            "trace": _trace_for_client(trace),
            "available_models": replay_models(),
        }

@router.post("/traces/{run_id}/replay")
def replay_trace(
    run_id: str,
    request: TraceReplayRequest,
    identity: RequestIdentity = Depends(require_request_identity),
):
    with get_db_cursor() as cursor:
        cursor.execute(
            "SELECT * FROM agent_runs WHERE id = %s AND tenant_id = %s AND user_id = %s",
            (run_id, identity.tenant_id, identity.user_id),
        )
        original = cursor.fetchone()
        if not original:
            raise HTTPException(status_code=404, detail=f"Trace '{run_id}' not found")

    allowed_models = set(replay_models())
    if request.override_model and request.override_model not in allowed_models:
        raise HTTPException(status_code=422, detail="Unsupported replay model")

    try:
        if request.mode == "frozen_evidence":
            generation_profile = original.get("generation_profile") or {}
            original_model = (
                generation_profile.get("requested_model")
                if isinstance(generation_profile, dict)
                else None
            )
            replayed = MultiAgentOrchestrator.execute_frozen_replay(
                query=original["input_query"],
                tenant_id=identity.tenant_id,
                user_id=identity.user_id,
                original_run_id=run_id,
                evidence_manifest=original.get("evidence_manifest") or {},
                conversation_context=original.get("conversation_context") or {},
                model_override=request.override_model or original_model,
            )
        else:
            document_ids, version_scope = _product_scope_from_plan(
                original.get("plan")
            )
            replayed = MultiAgentOrchestrator.execute(
                query=original["input_query"],
                tenant_id=identity.tenant_id,
                user_id=identity.user_id,
                model_override=request.override_model,
                replay_of_run_id=run_id,
                replay_mode="fresh_retrieval",
                document_ids=document_ids,
                version_scope=version_scope,
                conversation_context=original.get("conversation_context") or {},
            )
    except (ChatReplayUnavailable, ChatRunConflict) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except ChatBudgetExceeded as error:
        raise HTTPException(status_code=429, detail=str(error)) from error
    return {
        "original_run_id": run_id,
        "replayed_run_id": replayed["run_id"],
        "replay_mode": replayed["replay_mode"],
        "replay_provenance": replayed["provenance"],
        "original_latency_ms": original["latency_ms"],
        "replayed_latency_ms": replayed["latency_ms"],
        "original_eval_score": original["eval_score"],
        "replayed_eval_score": replayed["eval_score"],
        "original_answer_status": original["answer_status"],
        "replayed_answer_status": replayed["answer_status"],
        "original_grounding_profile": original["grounding_profile"],
        "replayed_grounding_profile": replayed["grounding_profile"],
        "replayed_model_used": replayed["model_used"],
        "replayed_response": replayed["response"],
        "agent_events": replayed["agent_events"]
    }
