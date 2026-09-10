import os
import re
import json
import time
import uuid
import logging
import hashlib
import asyncio
from concurrent.futures import wait
from threading import Event
from typing import TypedDict, List, Dict, Any, Optional, Callable
from psycopg2.extras import RealDictCursor

from langgraph.graph import StateGraph, START, END

from app.core.db import acquire_db_connection, release_db_connection
from app.core.runtime import (
    RETRIEVAL_STAGE_TIMEOUT_SECONDS,
    get_openai_client,
    get_retrieval_executor,
)
from app.conversation import (
    build_conversation_context,
    contextualize_question,
    validate_conversation_context,
)
from app.retrieval.hybrid import HybridSearchEngine, embed_query
from app.retrieval.graphrag import GraphRAGEngine
from app.retrieval.memory import retrieve_relevant_memories
from app.replay import ChatReplayUnavailable, reconstruct_frozen_evidence
from app.query_planning import build_query_plan, validate_query_plan
from app.pricing import estimate_openai_text_generation_cost
from app.grounding import (
    ANSWER_PROPOSAL_SCHEMA,
    GENERATION_MAX_OUTPUT_TOKENS,
    GENERATION_SCHEMA_NAME,
    GROUNDING_PROFILE,
    GroundingValidationError,
    build_evidence_pack,
    build_evidence_manifest,
    build_extractive_answer,
    build_generation_profile,
    build_generation_request,
    has_valid_canonical_sha256,
    validate_answer_proposal,
)

logger = logging.getLogger("orchestration_langgraph")

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://nexus:nexus_dev_password@localhost:5432/nexus")
MCP_TOOLS_URL = os.getenv("MCP_TOOLS_URL", "http://localhost:8003")
MCP_SERVER_URL = os.getenv("MCP_SERVER_URL", f"{MCP_TOOLS_URL.rstrip('/')}/mcp")
INTERNAL_SERVICE_TOKEN = os.getenv("INTERNAL_SERVICE_TOKEN", "")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_FAST_MODEL = os.getenv("OPENAI_FAST_MODEL", "gpt-5.4-mini")
OPENAI_REASONING_MODEL = os.getenv("OPENAI_REASONING_MODEL", "gpt-5.5")
OPENAI_GENERATION_TIMEOUT_SECONDS = float(
    os.getenv("OPENAI_GENERATION_TIMEOUT_SECONDS", "45")
)
OPENAI_GENERATION_MAX_RETRIES = int(
    os.getenv("OPENAI_GENERATION_MAX_RETRIES", "1")
)
AGENT_RUN_TOKEN_RESERVATION = int(os.getenv("AGENT_RUN_TOKEN_RESERVATION", "10000"))
AGENT_RUN_RESERVATION_TTL_MINUTES = int(
    os.getenv("AGENT_RUN_RESERVATION_TTL_MINUTES", "15")
)

if not 1_000 <= AGENT_RUN_TOKEN_RESERVATION <= 100_000:
    raise ValueError("AGENT_RUN_TOKEN_RESERVATION must be between 1000 and 100000")
if not 5 <= AGENT_RUN_RESERVATION_TTL_MINUTES <= 240:
    raise ValueError("AGENT_RUN_RESERVATION_TTL_MINUTES must be between 5 and 240")
if not 5 <= OPENAI_GENERATION_TIMEOUT_SECONDS <= 120:
    raise ValueError("OPENAI_GENERATION_TIMEOUT_SECONDS must be between 5 and 120")
if not 0 <= OPENAI_GENERATION_MAX_RETRIES <= 3:
    raise ValueError("OPENAI_GENERATION_MAX_RETRIES must be between 0 and 3")


def has_openai_api_key() -> bool:
    normalized = OPENAI_API_KEY.strip().lower()
    return bool(normalized) and not normalized.startswith(("placeholder", "test"))

# Optional LangSmith & Langfuse environment variables
LANGCHAIN_TRACING_V2 = os.getenv("LANGCHAIN_TRACING_V2", "false").lower() == "true"
LANGCHAIN_API_KEY = os.getenv("LANGCHAIN_API_KEY", "")
LANGFUSE_PUBLIC_KEY = os.getenv("LANGFUSE_PUBLIC_KEY", "")
LANGFUSE_SECRET_KEY = os.getenv("LANGFUSE_SECRET_KEY", "")

class AgentEvent(TypedDict):
    agent: str
    action: str
    status: str
    details: Dict[str, Any]
    timestamp: float

class AgentState(TypedDict):
    query: str
    selected_document_ids: List[str]
    selected_version_scope: str
    request_fingerprint: str
    user_id: str
    tenant_id: str
    session_id: str
    conversation_context: Dict[str, Any]
    model: str
    plan: Optional[Dict[str, Any]]
    retrieved_chunks: List[Dict[str, Any]]
    retrieved_memories: List[Dict[str, Any]]
    graph_context: Dict[str, Any]
    tool_calls: List[Dict[str, Any]]
    tool_results: List[Dict[str, Any]]
    response: str
    citations: List[Dict[str, Any]]
    claim_evidence: List[Dict[str, Any]]
    answer_status: str
    grounding_profile: str
    evidence_manifest: Dict[str, Any]
    generation_profile: Dict[str, Any]
    replay_of_run_id: Optional[str]
    replay_mode: str
    eval_score: float
    eval_details: Dict[str, Any]
    iteration_count: int
    agent_events: List[AgentEvent]
    trace_id: str
    prompt_tokens: int
    completion_tokens: int
    embedding_tokens: int
    embedding_cost_usd: float
    embedding_pricing_profile: Optional[str]
    token_reservation: int
    estimated_cost_usd: Optional[float]
    retrieval_latency_ms: int
    llm_latency_ms: int
    cancel_event: Optional[Event]


class ChatRunCancelled(Exception):
    """Raised cooperatively when the downstream client abandons a chat run."""


class ChatRunConflict(Exception):
    """Raised when a request ID cannot safely start another agent execution."""


class ChatBudgetExceeded(Exception):
    """Raised before a new run when daily token capacity cannot be reserved."""


def token_reservation_for_budget(allocated_tokens: int) -> int:
    if allocated_tokens <= 0:
        raise ValueError("allocated_tokens must be positive")
    return min(
        allocated_tokens,
        AGENT_RUN_TOKEN_RESERVATION,
        max(1_000, allocated_tokens // 10),
    )


def _raise_if_cancelled(state: AgentState) -> None:
    cancel_event = state.get("cancel_event")
    if cancel_event and cancel_event.is_set():
        raise ChatRunCancelled("The client disconnected before the run completed")


async def _execute_mcp_tool_call(
    tool_name: str,
    arguments: Dict[str, Any],
    user_id: str,
    tenant_id: str,
) -> Dict[str, Any]:
    from mcp import Client
    from mcp.client.streamable_http import streamable_http_client
    import httpx2

    headers = {
        "X-Internal-Service-Token": INTERNAL_SERVICE_TOKEN,
        "X-Certus-User-Id": user_id,
        "X-Certus-Tenant-Id": tenant_id,
    }
    async with httpx2.AsyncClient(
        headers=headers,
        follow_redirects=True,
        timeout=httpx2.Timeout(15.0, read=60.0),
    ) as http_client:
        transport = streamable_http_client(MCP_SERVER_URL, http_client=http_client)
        async with Client(transport) as client:
            result = await client.call_tool(tool_name, arguments)

    if result.is_error:
        message = " ".join(
            getattr(block, "text", "")
            for block in result.content
            if getattr(block, "type", "") == "text"
        ).strip()
        return {
            "tool": tool_name,
            "status": "error",
            "error": message or "The MCP tool could not complete the request",
        }

    structured = result.structured_content
    if isinstance(structured, dict):
        if set(structured) == {"result"} and isinstance(structured["result"], dict):
            return structured["result"]
        return structured
    return {"tool": tool_name, "status": "completed", "content": str(structured or "")}


def execute_mcp_tool_call(tool_name: str, arguments: Dict[str, Any], user_id: str, tenant_id: str) -> Dict[str, Any]:
    try:
        return asyncio.run(
            _execute_mcp_tool_call(tool_name, arguments, user_id, tenant_id)
        )
    except Exception as e:
        logger.warning("Failed to call MCP tool '%s': %s", tool_name, type(e).__name__)
        return {
            "tool": tool_name,
            "status": "unavailable",
            "error": "The workspace tool service is temporarily unavailable",
        }

# ------------------------------------------------------------
# LangGraph Agent Nodes
# ------------------------------------------------------------

def router_node(state: AgentState) -> Dict[str, Any]:
    _raise_if_cancelled(state)
    query = state["query"].lower()
    events = list(state.get("agent_events", []))
    
    # 1. Identify Tool Needs
    tool_calls = []
    if any(k in query for k in ["create task", "create a task", "add task", "new task", "remind me to"]):
        title_match = re.search(r'(?:create (?:a )?task (?:for |to )?|add task )(.+)', state["query"], re.IGNORECASE)
        task_title = title_match.group(1).strip() if title_match else state["query"]
        tool_calls.append({
            "tool_name": "create_task",
            "arguments": {"title": task_title, "priority": "high", "tags": ["agent_created"]}
        })
    elif any(k in query for k in ["summarize document", "summary of document", "summarize doc"]):
        document_id = re.search(
            r"\b[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\b",
            state["query"],
            re.IGNORECASE,
        )
        if document_id:
            tool_calls.append({
                "tool_name": "summarize_document",
                "arguments": {"document_id": document_id.group(0), "style": "bullet_points"}
            })
    elif any(k in query for k in ["graph query", "find graph connections", "knowledge graph"]):
        entity_candidates = GraphRAGEngine.extract_query_entities(state["query"])
        if entity_candidates:
            tool_calls.append({
                "tool_name": "graph_query",
                "arguments": {"entity_name": entity_candidates[0], "depth": 2}
            })

    # 2. Complexity Classification
    complex_keywords = ["analyze", "compare", "synthesize", "evaluate", "architecture", "multi-step", "explain in detail", "relate"]
    is_complex = any(k in query for k in complex_keywords) or len(query.split()) > 35 or len(tool_calls) > 0
    requested_model = state.get("model")
    allowed_models = {OPENAI_FAST_MODEL, OPENAI_REASONING_MODEL}
    selected_model = (
        (requested_model if requested_model in allowed_models else (
            OPENAI_REASONING_MODEL if is_complex else OPENAI_FAST_MODEL
        ))
        if has_openai_api_key()
        else "local-extractive"
    )
    
    events.append({
        "agent": "router",
        "action": f"Classified complexity: {'COMPLEX' if is_complex else 'SIMPLE'} → Assigned {selected_model}" + (f" ({len(tool_calls)} tool calls identified)" if tool_calls else ""),
        "status": "completed",
        "details": {"model": selected_model, "is_complex": is_complex, "tool_calls_planned": len(tool_calls)},
        "timestamp": time.time()
    })
    
    return {"model": selected_model, "tool_calls": tool_calls, "agent_events": events}

def planner_node(state: AgentState) -> Dict[str, Any]:
    _raise_if_cancelled(state)
    events = list(state.get("agent_events", []))
    tool_calls = state.get("tool_calls", [])
    router_details = next(
        (
            event.get("details", {})
            for event in reversed(events)
            if event.get("agent") == "router"
        ),
        {},
    )
    plan = build_query_plan(
        state["query"],
        tool_calls,
        selected_model=state["model"],
        is_complex=bool(router_details.get("is_complex")),
        selected_document_ids=state.get("selected_document_ids", []),
        selected_version_scope=state.get("selected_version_scope", "auto"),
        conversation_context=state.get("conversation_context"),
    ).model_dump(mode="json")
    
    events.append({
        "agent": "planner",
        "action": (
            f"Planned {plan['intent']} query with "
            f"{sum(bool(value) for value in plan['branches'].values())} active operation(s)"
        ),
        "status": "completed",
        "details": plan,
        "timestamp": time.time()
    })
    
    return {"plan": plan, "agent_events": events}

def researcher_node(state: AgentState) -> Dict[str, Any]:
    _raise_if_cancelled(state)
    events = list(state.get("agent_events", []))
    query = state["query"]
    tenant_id = state["tenant_id"]
    user_id = state["user_id"]
    retrieval_started_at = time.monotonic()
    raw_plan = state.get("plan")
    plan = validate_query_plan(raw_plan)
    retrieval_query = plan.retrieval_query if plan else query
    use_documents = plan.branches.documents if plan else True
    use_memories = plan.branches.memories if plan else True
    use_graph = plan.branches.graph if plan else True
    needs_embedding = plan.branches.query_embedding if plan else True
    document_ids = (
        plan.detected_constraints.document_ids
        if plan and plan.enforcement.requested_document_id_filter_applied
        else []
    )
    titles = (
        plan.detected_constraints.titles
        if plan and plan.enforcement.requested_title_filter_applied
        else []
    )
    temporal_years = (
        plan.detected_constraints.years
        if plan and plan.enforcement.temporal_filter_mode in {"exact_years", "as_of"}
        else []
    )
    temporal_year_start = (
        plan.detected_constraints.year_start
        if plan and plan.enforcement.temporal_filter_mode == "year_bounds"
        else None
    )
    temporal_year_end = (
        plan.detected_constraints.year_end
        if plan and plan.enforcement.temporal_filter_mode == "year_bounds"
        else None
    )
    temporal_time_start = (
        plan.detected_constraints.time_start
        if plan and plan.enforcement.temporal_filter_mode == "time_window"
        else None
    )
    temporal_time_end = (
        plan.detected_constraints.time_end_exclusive
        if plan and plan.enforcement.temporal_filter_mode in {"time_window", "as_of"}
        else None
    )
    version_scope = (
        {
            "all_retained_versions": "all",
            "current_version_only": "current",
            "as_of_version": "as_of",
        }[plan.enforcement.retrieval_version_scope]
        if plan else "all"
    )

    embedding_started_at = time.monotonic()
    query_embedding = None
    embedding_error = None
    if needs_embedding:
        try:
            query_embedding = embed_query(retrieval_query)
        except Exception as error:
            embedding_error = type(error).__name__
            logger.warning("Shared query embedding unavailable: %s", error)
    embedding_latency_ms = int((time.monotonic() - embedding_started_at) * 1000)

    query_entities = (
        GraphRAGEngine.extract_query_entities(retrieval_query)
        if use_graph
        else []
    )
    executor = get_retrieval_executor() if any((use_documents, use_graph, use_memories)) else None
    futures = {}
    if use_documents:
        assert executor is not None
        futures["documents"] = executor.submit(
            HybridSearchEngine.search,
            retrieval_query,
            tenant_id,
            user_id,
            5,
            query_embedding,
            False,
            document_ids,
            temporal_years,
            plan.detected_constraints.temporal_authority if plan else "effective",
            version_scope,
            temporal_year_start,
            temporal_year_end,
            temporal_time_start,
            temporal_time_end,
            titles,
        )
    if use_graph:
        assert executor is not None
        futures["graph"] = executor.submit(
            GraphRAGEngine.traverse_graph,
            query_entities,
            user_id,
            tenant_id,
            2,
        )
    if use_memories:
        assert executor is not None
        futures["memories"] = executor.submit(
            retrieve_relevant_memories,
            retrieval_query,
            tenant_id,
            user_id,
            5,
            query_embedding,
            False,
        )
    done, pending = (
        wait(futures.values(), timeout=RETRIEVAL_STAGE_TIMEOUT_SECONDS)
        if futures
        else (set(), set())
    )
    timed_out = [name for name, future in futures.items() if future in pending]
    for future in pending:
        future.cancel()

    branch_errors: Dict[str, str] = {}

    def result_or_default(name: str, default):
        future = futures[name]
        if future not in done:
            return default
        try:
            return future.result()
        except Exception as error:
            branch_errors[name] = type(error).__name__
            logger.warning("%s retrieval branch degraded: %s", name, error)
            return default

    chunks = result_or_default("documents", []) if use_documents else []
    empty_graph = {
        "graph_triples": [],
        "connected_entities": [],
        "relationships_count": 0,
    }
    graph_data = (
        result_or_default("graph", empty_graph)
        if use_graph
        else empty_graph
    )
    memories = result_or_default("memories", []) if use_memories else []
    _raise_if_cancelled(state)
    chunk_dicts = [chunk.to_dict() for chunk in chunks]
    retrieval_latency_ms = int((time.monotonic() - retrieval_started_at) * 1000)

    events.append({
        "agent": "researcher",
        "action": (
            f"Hybrid search retrieved {len(chunk_dicts)} chunks and {len(memories)} memories; "
            f"GraphRAG discovered {graph_data.get('relationships_count', 0)} triples"
        ),
        "status": "completed",
        "details": {
            "chunk_count": len(chunk_dicts),
            "memory_count": len(memories),
            "graph_triples": graph_data.get("graph_triples", [])[:3],
            "shared_embedding_profile": (
                query_embedding.profile.identifier if query_embedding else None
            ),
            "embedding_latency_ms": embedding_latency_ms,
            "retrieval_latency_ms": retrieval_latency_ms,
            "embedding_error": embedding_error,
            "embedding_tokens": query_embedding.input_tokens if query_embedding else 0,
            "embedding_cost_usd": (
                query_embedding.estimated_cost_usd if query_embedding else 0.0
            ),
            "degraded_branches": sorted(branch_errors),
            "timed_out_branches": timed_out,
            "query_plan_profile": plan.profile if plan else "legacy_all_branches:v0",
            "planned_branches": {
                "documents": use_documents,
                "memories": use_memories,
                "graph": use_graph,
            },
            "applied_document_filter_count": len(document_ids),
            "applied_title_filter_count": len(titles),
            "applied_temporal_years": list(temporal_years),
            "applied_temporal_year_start": temporal_year_start,
            "applied_temporal_year_end": temporal_year_end,
            "applied_temporal_time_start": temporal_time_start,
            "applied_temporal_time_end_exclusive": temporal_time_end,
            "temporal_authority": (
                plan.detected_constraints.temporal_authority
                if plan else "effective"
            ),
            "temporal_filter_mode": (
                plan.enforcement.temporal_filter_mode if plan else "none"
            ),
            "retrieval_version_scope": (
                plan.enforcement.retrieval_version_scope
                if plan else "all_retained_versions"
            ),
            "retrieval_query_transformed": bool(
                plan and plan.deterministic_transformations
            ),
        },
        "timestamp": time.time()
    })
    
    return {
        "retrieved_chunks": chunk_dicts,
        "retrieved_memories": memories,
        "graph_context": graph_data,
        "retrieval_latency_ms": retrieval_latency_ms,
        "embedding_tokens": query_embedding.input_tokens if query_embedding else 0,
        "embedding_cost_usd": (
            query_embedding.estimated_cost_usd if query_embedding else 0.0
        ),
        "embedding_pricing_profile": (
            query_embedding.pricing_profile if query_embedding else None
        ),
        "agent_events": events,
    }

def tool_dispatcher_node(state: AgentState) -> Dict[str, Any]:
    _raise_if_cancelled(state)
    events = list(state.get("agent_events", []))
    tool_calls = state.get("tool_calls", [])
    user_id = state["user_id"]
    tenant_id = state["tenant_id"]
    
    tool_results = []
    for tc in tool_calls:
        _raise_if_cancelled(state)
        events.append({
            "agent": "tool_dispatcher",
            "action": f"Invoking MCP tool: {tc['tool_name']}",
            "status": "started",
            "details": tc,
            "timestamp": time.time()
        })
        t_res = execute_mcp_tool_call(tc["tool_name"], tc["arguments"], user_id, tenant_id)
        _raise_if_cancelled(state)
        tool_results.append(t_res)
        tool_succeeded = t_res.get("status") not in {"error", "unavailable"} and "error" not in t_res
        events.append({
            "agent": "tool_dispatcher",
            "action": (
                f"MCP tool '{tc['tool_name']}' completed"
                if tool_succeeded
                else f"MCP tool '{tc['tool_name']}' failed"
            ),
            "status": "completed" if tool_succeeded else "failed",
            "details": t_res,
            "timestamp": time.time()
        })
        
    return {"tool_results": tool_results, "agent_events": events}

def render_tool_results(tool_results: List[Dict[str, Any]]) -> List[str]:
    rendered = []
    for result in tool_results:
        if result.get("status") in {"error", "unavailable"} or result.get("error"):
            rendered.append(
                f"Tool `{result.get('tool', 'unknown')}` could not complete: "
                f"{result.get('error', 'service unavailable')}"
            )
        elif result.get("tool") == "create_task":
            rendered.append(
                "Task created successfully.\n"
                f"- Task ID: `{result.get('task_id')}`\n"
                f"- Title: **{result.get('title')}**\n"
                f"- Priority: `{result.get('priority')}`"
            )
        elif result.get("tool") == "summarize_document":
            rendered.append(f"Document summary:\n{result.get('summary')}")
        elif result.get("tool") == "graph_query":
            triples = "\n".join(f"- {triple}" for triple in result.get("graph_triples", []))
            rendered.append(f"Knowledge graph results:\n{triples or 'No connections found.'}")
    return rendered


def generate_openai_grounded_proposal(
    state: AgentState,
    evidence_pack: List[Dict[str, Any]],
    generation_query: str,
) -> tuple[Dict[str, Any], int, int, Dict[str, Any]]:
    client = get_openai_client(
        "generation",
        OPENAI_API_KEY,
        OPENAI_GENERATION_TIMEOUT_SECONDS,
        OPENAI_GENERATION_MAX_RETRIES,
    )
    generation_request = build_generation_request(generation_query, evidence_pack)
    response = client.responses.create(
        model=state["model"],
        instructions=generation_request["instructions"],
        input=generation_request["input"],
        max_output_tokens=GENERATION_MAX_OUTPUT_TOKENS,
        store=False,
        text={
            "format": {
                "type": "json_schema",
                "name": GENERATION_SCHEMA_NAME,
                "strict": True,
                "schema": ANSWER_PROPOSAL_SCHEMA,
            }
        },
        safety_identifier=hashlib.sha256(state["user_id"].encode("utf-8")).hexdigest(),
    )
    _raise_if_cancelled(state)
    response_status = getattr(response, "status", None)
    if response_status not in {None, "completed"}:
        raise RuntimeError(f"Generation response did not complete: {response_status}")
    output_text = getattr(response, "output_text", "")
    if not isinstance(output_text, str) or not output_text.strip():
        raise RuntimeError("Generation response contained no structured answer")
    try:
        proposal = json.loads(output_text)
    except json.JSONDecodeError as error:
        raise RuntimeError("Generation response was not valid structured JSON") from error
    if not isinstance(proposal, dict):
        raise RuntimeError("Generation response schema root was not an object")
    usage = getattr(response, "usage", None)
    prompt_tokens = int(getattr(usage, "input_tokens", 0) or 0)
    completion_tokens = int(getattr(usage, "output_tokens", 0) or 0)
    provider_metadata = {
        "provider": "openai",
        "response_id": getattr(response, "id", None),
        "response_created_at": getattr(response, "created_at", None),
        "returned_model": getattr(response, "model", None),
        "service_tier": getattr(response, "service_tier", None),
        "cached_input_tokens": int(
            getattr(getattr(usage, "input_tokens_details", None), "cached_tokens", 0)
            or 0
        ),
    }
    return proposal, prompt_tokens, completion_tokens, provider_metadata


def executor_node(state: AgentState) -> Dict[str, Any]:
    _raise_if_cancelled(state)
    generation_started_at = time.monotonic()
    events = list(state.get("agent_events", []))
    chunks = state.get("retrieved_chunks", [])
    memories = state.get("retrieved_memories", [])
    graph_context = state.get("graph_context", {})
    tool_results = state.get("tool_results", [])
    model = state["model"]
    requested_model = model
    generation_query = contextualize_question(
        state["query"],
        state.get("conversation_context"),
    )

    graph_triples = graph_context.get("graph_triples", [])
    evidence_pack = build_evidence_pack(chunks, memories, graph_triples, tool_results)
    evidence_manifest = build_evidence_manifest(evidence_pack)
    non_tool_evidence = [source for source in evidence_pack if source["source_kind"] != "tool"]
    tool_response_parts = render_tool_results(tool_results)
    prompt_tokens = 0
    completion_tokens = 0
    grounded_result: Dict[str, Any]
    generation_profile: Dict[str, Any]
    provider_metadata: Dict[str, Any] = {}
    provider_attempted = False
    embedding_cost_usd = float(state.get("embedding_cost_usd", 0.0) or 0.0)
    estimated_cost_usd: Optional[float] = embedding_cost_usd

    if evidence_pack and has_openai_api_key() and model != "local-extractive":
        provider_attempted = True
        try:
            (
                proposal,
                prompt_tokens,
                completion_tokens,
                provider_metadata,
            ) = generate_openai_grounded_proposal(
                state,
                evidence_pack,
                generation_query,
            )
            grounded_result = validate_answer_proposal(proposal, evidence_pack)
            returned_model = str(provider_metadata.get("returned_model") or requested_model)
            try:
                generation_cost = estimate_openai_text_generation_cost(
                    model=returned_model,
                    input_tokens=prompt_tokens,
                    cached_input_tokens=int(
                        provider_metadata.get("cached_input_tokens") or 0
                    ),
                    output_tokens=completion_tokens,
                    service_tier=provider_metadata.get("service_tier"),
                )
            except ValueError as error:
                logger.warning("Provider usage could not be priced: %s", type(error).__name__)
                generation_cost = None
            estimated_cost_usd = (
                round(embedding_cost_usd + generation_cost.amount_usd, 12)
                if generation_cost is not None
                else None
            )
            generation_profile = build_generation_profile(
                execution_mode="openai_structured",
                provider="openai",
                requested_model=requested_model,
                returned_model=returned_model,
                query=generation_query,
                evidence_pack=evidence_pack,
                evidence_manifest=evidence_manifest,
                response_id=provider_metadata.get("response_id"),
                response_created_at=provider_metadata.get("response_created_at"),
                service_tier=provider_metadata.get("service_tier"),
                timeout_seconds=OPENAI_GENERATION_TIMEOUT_SECONDS,
                max_retries=OPENAI_GENERATION_MAX_RETRIES,
                cost_estimate=(
                    {
                        **generation_cost.as_profile(),
                        "generation_amount_usd": generation_cost.amount_usd,
                        "embedding_amount_usd": embedding_cost_usd,
                        "embedding_pricing_profile": state.get(
                            "embedding_pricing_profile"
                        ),
                        "total_amount_usd": estimated_cost_usd,
                    }
                    if generation_cost is not None
                    else None
                ),
            )
            model = returned_model
        except ChatRunCancelled:
            raise
        except Exception as error:
            logger.warning(
                "Structured grounded generation failed validation; using conservative fallback: %s",
                type(error).__name__,
            )
            model = "local-extractive-fallback"
            estimated_cost_usd = None
            events.append({
                "agent": "executor",
                "action": "Model proposal failed closed; returned exact extractive evidence",
                "status": "degraded",
                "details": {"error_type": type(error).__name__},
                "timestamp": time.time(),
            })
            grounded_result = build_extractive_answer(non_tool_evidence)
            generation_profile = build_generation_profile(
                execution_mode="local_extractive_fallback",
                provider="certus_local",
                requested_model=requested_model,
                returned_model=model,
                query=generation_query,
                evidence_pack=evidence_pack,
                evidence_manifest=evidence_manifest,
                failure_type=type(error).__name__,
                provider_attempt={
                    "provider": "openai",
                    "requested_model": requested_model,
                    **provider_metadata,
                },
            )
    elif non_tool_evidence:
        grounded_result = build_extractive_answer(non_tool_evidence)
        generation_profile = build_generation_profile(
            execution_mode="local_extractive",
            provider="certus_local",
            requested_model=requested_model,
            returned_model=model,
            query=generation_query,
            evidence_pack=evidence_pack,
            evidence_manifest=evidence_manifest,
        )
    elif tool_response_parts:
        grounded_result = {
            "answer_status": "action_completed",
            "grounding_profile": GROUNDING_PROFILE,
            "claims": [],
            "citations": [],
            "response": "\n\n".join(tool_response_parts),
        }
        generation_profile = build_generation_profile(
            execution_mode="local_action",
            provider="certus_local",
            requested_model=requested_model,
            returned_model=model,
            query=generation_query,
            evidence_pack=evidence_pack,
            evidence_manifest=evidence_manifest,
        )
    else:
        grounded_result = build_extractive_answer([])
        generation_profile = build_generation_profile(
            execution_mode="local_extractive",
            provider="certus_local",
            requested_model=requested_model,
            returned_model=model,
            query=generation_query,
            evidence_pack=evidence_pack,
            evidence_manifest=evidence_manifest,
        )

    final_response = grounded_result["response"]
    if tool_response_parts and grounded_result["answer_status"] != "action_completed":
        final_response = "\n\n".join([*tool_response_parts, final_response])
    if model.startswith("local-") and not provider_attempted:
        prompt_tokens = max(1, len(state["query"].split()))
        completion_tokens = max(1, len(final_response.split()))

    events.append({
        "agent": "executor",
        "action": (
            f"Produced {grounded_result['answer_status']} response with "
            f"{len(grounded_result['claims'])} atomic claim(s) and "
            f"{len(grounded_result['citations'])} selected citation(s)"
        ),
        "status": "completed",
        "details": {
            "citations_count": len(grounded_result["citations"]),
            "claims_count": len(grounded_result["claims"]),
            "answer_status": grounded_result["answer_status"],
            "grounding_profile": grounded_result["grounding_profile"],
            "semantic_support_status": "not_evaluated",
            "conversation_turn_count": int(
                state.get("conversation_context", {}).get("turn_count", 0)
            ),
            "conversation_context_sha256": state.get(
                "conversation_context", {}
            ).get("canonical_sha256"),
            "model": model,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "cached_input_tokens": int(
                provider_metadata.get("cached_input_tokens") or 0
            ),
            "embedding_tokens": int(state.get("embedding_tokens", 0) or 0),
            "estimated_cost_usd": estimated_cost_usd,
        },
        "timestamp": time.time(),
    })

    return {
        "response": final_response,
        "citations": grounded_result["citations"],
        "claim_evidence": grounded_result["claims"],
        "answer_status": grounded_result["answer_status"],
        "grounding_profile": grounded_result["grounding_profile"],
        "evidence_manifest": evidence_manifest,
        "generation_profile": generation_profile,
        "model": model,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "llm_latency_ms": int((time.monotonic() - generation_started_at) * 1000),
        "estimated_cost_usd": estimated_cost_usd,
        "agent_events": events,
    }


def critic_node(state: AgentState) -> Dict[str, Any]:
    _raise_if_cancelled(state)
    events = list(state.get("agent_events", []))
    iter_count = state.get("iteration_count", 0) + 1
    claims = state.get("claim_evidence", [])
    citations = state.get("citations", [])
    answer_status = state.get("answer_status", "pending")
    claim_by_id = {
        claim.get("claim_id"): claim
        for claim in claims
        if isinstance(claim, dict) and isinstance(claim.get("claim_id"), str)
    }
    citation_by_evidence = {
        citation.get("evidence_id"): citation
        for citation in citations
        if isinstance(citation, dict) and isinstance(citation.get("evidence_id"), str)
    }
    structural_integrity = (
        state.get("grounding_profile") == GROUNDING_PROFILE
        and len(claim_by_id) == len(claims)
        and len(citation_by_evidence) == len(citations)
    )
    substantive_status = answer_status in {"answered", "extractive", "conflicting_evidence"}
    if substantive_status != bool(claims):
        structural_integrity = False
    if answer_status in {"insufficient_evidence", "action_completed"} and citations:
        structural_integrity = False

    evidence_pack: List[Dict[str, Any]] = []
    if structural_integrity:
        try:
            evidence_pack = build_evidence_pack(
                state.get("retrieved_chunks", []),
                state.get("retrieved_memories", []),
                state.get("graph_context", {}).get("graph_triples", []),
                state.get("tool_results", []),
            )
            expected_manifest = build_evidence_manifest(evidence_pack)
            stored_manifest = state.get("evidence_manifest", {})
            generation_profile = state.get("generation_profile", {})
            if (
                stored_manifest != expected_manifest
                or not has_valid_canonical_sha256(stored_manifest)
                or not has_valid_canonical_sha256(generation_profile)
                or generation_profile.get("evidence_manifest_sha256")
                    != stored_manifest.get("canonical_sha256")
                or generation_profile.get("grounding_profile") != GROUNDING_PROFILE
            ):
                structural_integrity = False
        except (KeyError, TypeError, GroundingValidationError):
            structural_integrity = False

    for claim_id, claim in claim_by_id.items():
        if (
            claim.get("mechanical_validation", {}).get("status") != "passed"
            or claim.get("semantic_support_status") != "not_evaluated"
        ):
            structural_integrity = False
        for source_ref in claim.get("source_refs", []):
            if source_ref.get("source_kind") != "document":
                continue
            citation = citation_by_evidence.get(source_ref.get("source_id"))
            if not citation or claim_id not in citation.get("claim_ids", []):
                structural_integrity = False

    for evidence_id, citation in citation_by_evidence.items():
        for claim_id in citation.get("claim_ids", []):
            claim = claim_by_id.get(claim_id)
            if not claim or not any(
                source_ref.get("source_kind") == "document"
                and source_ref.get("source_id") == evidence_id
                for source_ref in claim.get("source_refs", [])
            ):
                structural_integrity = False

    if structural_integrity and answer_status != "action_completed":
        try:
            proposal_status = (
                "conflicting_evidence"
                if answer_status == "conflicting_evidence"
                else "insufficient_evidence"
                if answer_status == "insufficient_evidence"
                else "answer"
            )
            reconstructed = validate_answer_proposal(
                {
                    "status": proposal_status,
                    "claims": [
                        {
                            "text": claim["text"],
                            "source_ids": [
                                source_ref["source_id"]
                                for source_ref in claim["source_refs"]
                            ],
                        }
                        for claim in claims
                    ],
                },
                evidence_pack,
            )
            if (
                reconstructed["claims"] != claims
                or reconstructed["citations"] != citations
            ):
                structural_integrity = False
        except (KeyError, TypeError, GroundingValidationError):
            structural_integrity = False

    score = 1.0 if structural_integrity else 0.0
    critic_update: Dict[str, Any] = {}
    if not structural_integrity:
        critic_update = {
            "response": "I couldn’t verify the internal claim-to-evidence structure, so no answer was released.",
            "citations": [],
            "claim_evidence": [],
            "answer_status": "insufficient_evidence",
            "grounding_profile": GROUNDING_PROFILE,
        }

    events.append({
        "agent": "critic",
        "action": (
            "Atomic claim/evidence integrity passed"
            if structural_integrity
            else "Atomic claim/evidence integrity failed closed"
        ),
        "status": "completed" if structural_integrity else "warning",
        "details": {
            "metric": "mechanical_claim_evidence_integrity",
            "score": score,
            "structural_integrity": structural_integrity,
            "semantic_support_metric": False,
            "semantic_support_status": "not_evaluated",
            "iteration": iter_count,
        },
        "timestamp": time.time(),
    })

    return {
        **critic_update,
        "eval_score": score,
        "eval_details": {
            "metric": "mechanical_claim_evidence_integrity",
            "structural_integrity": structural_integrity,
            "semantic_support_metric": False,
            "semantic_support_status": "not_evaluated",
        },
        "iteration_count": iter_count,
        "agent_events": events,
    }

# ------------------------------------------------------------
# Build StateGraph
# ------------------------------------------------------------

def build_langgraph():
    workflow = StateGraph(AgentState)
    
    workflow.add_node("router", router_node)
    workflow.add_node("planner", planner_node)
    workflow.add_node("researcher", researcher_node)
    workflow.add_node("tool_dispatcher", tool_dispatcher_node)
    workflow.add_node("executor", executor_node)
    workflow.add_node("critic", critic_node)
    
    workflow.add_edge(START, "router")
    workflow.add_edge("router", "planner")
    workflow.add_edge("planner", "researcher")
    workflow.add_edge("researcher", "tool_dispatcher")
    workflow.add_edge("tool_dispatcher", "executor")
    workflow.add_edge("executor", "critic")
    workflow.add_edge("critic", END)
    
    return workflow.compile()

app_graph = build_langgraph()


def _json_list(value: Any) -> List[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else []
        except json.JSONDecodeError:
            return []
    return []


def _json_object(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def _provenance_summary(
    evidence_manifest: Dict[str, Any],
    generation_profile: Dict[str, Any],
    conversation_context: Optional[Dict[str, Any]] = None,
    evidence_manifest_db_sha256: Optional[str] = None,
    generation_profile_db_sha256: Optional[str] = None,
    conversation_context_db_sha256: Optional[str] = None,
) -> Dict[str, Any]:
    context = conversation_context or {}
    return {
        "evidence_manifest_profile": evidence_manifest.get("profile", "legacy_unavailable:v0"),
        "evidence_manifest_sha256": evidence_manifest.get("canonical_sha256"),
        "evidence_manifest_db_sha256": evidence_manifest_db_sha256,
        "evidence_source_count": int(evidence_manifest.get("source_count") or 0),
        "generation_profile": generation_profile.get("profile", "legacy_unavailable:v0"),
        "generation_profile_sha256": generation_profile.get("canonical_sha256"),
        "generation_profile_db_sha256": generation_profile_db_sha256,
        "execution_mode": generation_profile.get("execution_mode"),
        "provider": generation_profile.get("provider"),
        "requested_model": generation_profile.get("requested_model"),
        "returned_model": generation_profile.get("returned_model"),
        "model_revision_locked": bool(generation_profile.get("model_revision_locked", False)),
        "prompt_profile": generation_profile.get("prompt_profile"),
        "validator_profile": generation_profile.get("validator_profile"),
        "conversation_context_profile": context.get("profile"),
        "conversation_context_sha256": context.get("canonical_sha256"),
        "conversation_context_db_sha256": conversation_context_db_sha256,
        "conversation_turn_count": int(context.get("turn_count") or 0),
    }


def _result_from_row(
    row: Dict[str, Any],
    fallback_session_id: Optional[str] = None,
) -> Dict[str, Any]:
    evidence_manifest = _json_object(row.get("evidence_manifest"))
    generation_profile = _json_object(row.get("generation_profile"))
    conversation_context = _json_object(row.get("conversation_context"))
    return {
        "run_id": str(row["id"]),
        "session_id": (
            str(row["session_id"])
            if row.get("session_id")
            else fallback_session_id
        ),
        "response": row.get("output_response") or "",
        "model_used": row.get("model_used") or "auto",
        "citations": _json_list(row.get("citations")),
        "claims": _json_list(row.get("claim_evidence")),
        "answer_status": row.get("answer_status") or "legacy_unavailable",
        "grounding_profile": row.get("grounding_profile") or "legacy_unavailable:v0",
        "replay_of_run_id": (
            str(row["replay_of_run_id"]) if row.get("replay_of_run_id") else None
        ),
        "replay_mode": row.get("replay_mode") or "original",
        "provenance": _provenance_summary(
            evidence_manifest,
            generation_profile,
            conversation_context,
            row.get("evidence_manifest_db_sha256"),
            row.get("generation_profile_db_sha256"),
            row.get("conversation_context_db_sha256"),
        ),
        "eval_score": float(row.get("eval_score") or 0.0),
        "latency_ms": int(row.get("latency_ms") or 0),
        "prompt_tokens": int(row.get("prompt_tokens") or 0),
        "completion_tokens": int(row.get("completion_tokens") or 0),
        "total_tokens": int(row.get("total_tokens") or 0),
        "estimated_cost_usd": (
            float(row["estimated_cost_usd"])
            if row.get("estimated_cost_usd") is not None
            else None
        ),
        "agent_events": _json_list(row.get("events")),
    }


def _load_agent_run(cursor, run_id: str):
    cursor.execute(
        """
        SELECT id, user_id, tenant_id, session_id, input_query, model_used,
               request_fingerprint,
               output_response, citations, claim_evidence, answer_status,
               grounding_profile, evidence_manifest, generation_profile,
               conversation_context, evidence_manifest_db_sha256,
               generation_profile_db_sha256, conversation_context_db_sha256,
               replay_of_run_id, replay_mode,
               eval_score, latency_ms,
               prompt_tokens, completion_tokens, total_tokens,
               estimated_cost_usd, events, status, created_at
        FROM agent_runs
        WHERE id = %s
        """,
        (run_id,),
    )
    return cursor.fetchone()


def _ensure_chat_session(cursor, state: AgentState) -> str:
    try:
        session_id = str(uuid.UUID(state["session_id"]))
    except (AttributeError, TypeError, ValueError) as error:
        raise ValueError("Chat session ID must be a UUID") from error
    cursor.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
        (f"certus:chat-session:{session_id}",),
    )
    cursor.execute(
        """
        INSERT INTO chat_sessions (id, user_id, organization_id, title)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (id) DO UPDATE
        SET last_active_at = NOW()
        WHERE chat_sessions.user_id = EXCLUDED.user_id
          AND chat_sessions.organization_id = EXCLUDED.organization_id
        RETURNING id
        """,
        (
            session_id,
            state["user_id"],
            state["tenant_id"],
            state["query"][:255],
        ),
    )
    if not cursor.fetchone():
        raise ChatRunConflict("This chat session belongs to another workspace identity")
    state["session_id"] = session_id
    return session_id


def _load_conversation_context(
    cursor,
    state: AgentState,
    run_id: str,
) -> Dict[str, Any]:
    cursor.execute(
        """
        SELECT id
        FROM agent_runs
        WHERE tenant_id = %s
          AND user_id = %s
          AND session_id = %s
          AND status = 'running'
          AND id <> %s
        LIMIT 1
        """,
        (
            state["tenant_id"],
            state["user_id"],
            state["session_id"],
            run_id,
        ),
    )
    if cursor.fetchone():
        raise ChatRunConflict("Another request is active in this chat session")
    cursor.execute(
        """
        SELECT id, input_query, output_response, answer_status
        FROM agent_runs
        WHERE tenant_id = %s
          AND user_id = %s
          AND session_id = %s
          AND status = 'completed'
          AND replay_of_run_id IS NULL
          AND id <> %s
        ORDER BY created_at DESC, id DESC
        LIMIT 4
        """,
        (
            state["tenant_id"],
            state["user_id"],
            state["session_id"],
            run_id,
        ),
    )
    rows = list(reversed(cursor.fetchall()))
    return build_conversation_context(rows)


def _resolve_existing_agent_run(cursor, conn, state: AgentState, run_id: str, existing):
    if not existing or (
        existing["tenant_id"] != state["tenant_id"]
        or existing["user_id"] != state["user_id"]
        or existing["input_query"] != state["query"]
        or (
            existing.get("request_fingerprint") is None
            and (
                bool(state.get("selected_document_ids"))
                or state.get("selected_version_scope", "auto") != "auto"
            )
        )
        or (
            existing.get("request_fingerprint") is not None
            and existing["request_fingerprint"] != state["request_fingerprint"]
        )
    ):
        raise ChatRunConflict("This request ID is already in use")
    if existing["status"] == "completed":
        conn.commit()
        return _result_from_row(dict(existing), state["session_id"])
    if existing["status"] == "running":
        retry_deadline = time.monotonic() + 5
        while time.monotonic() < retry_deadline:
            time.sleep(0.1)
            refreshed = _load_agent_run(cursor, run_id)
            if refreshed and refreshed["status"] == "completed":
                conn.commit()
                return _result_from_row(dict(refreshed), state["session_id"])
            if not refreshed or refreshed["status"] != "running":
                existing = refreshed
                break
        if existing and existing["status"] == "running":
            raise ChatRunConflict("This request is still running")
        if not existing:
            raise ChatRunConflict("This request is no longer available")
    raise ChatRunConflict(
        f"This request previously stopped with status '{existing['status']}'"
    )


def _claim_agent_run(state: AgentState, run_id: str) -> Optional[Dict[str, Any]]:
    conn = acquire_db_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cursor:
            existing = _load_agent_run(cursor, run_id)
            if existing:
                return _resolve_existing_agent_run(cursor, conn, state, run_id, existing)

            session_id = _ensure_chat_session(cursor, state)
            if state.get("conversation_context"):
                state["conversation_context"] = validate_conversation_context(
                    state["conversation_context"]
                )
            else:
                state["conversation_context"] = _load_conversation_context(
                    cursor,
                    state,
                    run_id,
                )

            cursor.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (f"certus:daily-token-budget:{state['tenant_id']}:{state['user_id']}",),
            )
            cursor.execute(
                """
                UPDATE agent_runs
                SET status = 'timeout',
                    reserved_tokens = 0,
                    error_message = COALESCE(error_message, 'Agent run reservation expired'),
                    completed_at = NOW()
                WHERE tenant_id = %s
                  AND user_id = %s
                  AND status = 'running'
                  AND created_at < NOW() - %s * INTERVAL '1 minute'
                """,
                (
                    state["tenant_id"],
                    state["user_id"],
                    AGENT_RUN_RESERVATION_TTL_MINUTES,
                ),
            )
            cursor.execute(
                """
                WITH utc_day AS (
                    SELECT date_trunc('day', NOW() AT TIME ZONE 'UTC')
                           AT TIME ZONE 'UTC' AS starts_at
                )
                SELECT COALESCE(
                           (SELECT max_token_budget_daily
                            FROM tenant_config
                            WHERE organization_id = %s),
                           100000
                       )::bigint AS allocated_tokens,
                       COALESCE(SUM(
                           CASE
                               WHEN status = 'running'
                                   THEN GREATEST(total_tokens, reserved_tokens)
                               ELSE total_tokens
                           END
                       ), 0)::bigint AS consumed_or_reserved_tokens
                FROM agent_runs, utc_day
                WHERE tenant_id = %s
                  AND user_id = %s
                  AND created_at >= utc_day.starts_at
                  AND created_at < utc_day.starts_at + INTERVAL '1 day'
                """,
                (state["tenant_id"], state["tenant_id"], state["user_id"]),
            )
            budget = cursor.fetchone()
            allocated_tokens = int(budget["allocated_tokens"])
            consumed_or_reserved_tokens = int(budget["consumed_or_reserved_tokens"])
            reservation = token_reservation_for_budget(allocated_tokens)
            if consumed_or_reserved_tokens + reservation > allocated_tokens:
                raise ChatBudgetExceeded(
                    "The daily agent token budget has no capacity for another run. "
                    "Try again after 00:00 UTC."
                )

            cursor.execute(
                """
                INSERT INTO agent_runs (
                    id, user_id, tenant_id, session_id, input_query, model_used, status,
                    reserved_tokens, answer_status, grounding_profile, claim_evidence,
                    replay_of_run_id, replay_mode, request_fingerprint,
                    conversation_context
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, 'running', %s,
                    'pending', %s, '[]'::jsonb, %s, %s, %s, %s::jsonb
                )
                ON CONFLICT (id) DO NOTHING
                RETURNING id
                """,
                (
                    run_id,
                    state["user_id"],
                    state["tenant_id"],
                    session_id,
                    state["query"],
                    state["model"],
                    reservation,
                    GROUNDING_PROFILE,
                    state.get("replay_of_run_id"),
                    state.get("replay_mode", "original"),
                    state["request_fingerprint"],
                    json.dumps(state["conversation_context"]),
                ),
            )
            if cursor.fetchone():
                state["token_reservation"] = reservation
                conn.commit()
                return None

            conn.commit()
            existing = _load_agent_run(cursor, run_id)
            return _resolve_existing_agent_run(cursor, conn, state, run_id, existing)
    except Exception:
        conn.rollback()
        raise
    finally:
        release_db_connection(conn)


def _persist_agent_run(
    state: AgentState,
    run_id: str,
    latency_ms: int,
    status: str,
    partial_response: str = "",
    error_message: Optional[str] = None,
) -> None:
    response = state.get("response") or partial_response
    prompt_tokens = state.get("prompt_tokens", 0)
    completion_tokens = state.get("completion_tokens", 0)
    conn = acquire_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                UPDATE agent_runs
                SET plan = %s::jsonb,
                    model_used = %s,
                    retrieved_chunk_ids = %s::uuid[],
                    output_response = %s,
                    citations = %s::jsonb,
                    claim_evidence = %s::jsonb,
                    answer_status = %s,
                    grounding_profile = %s,
                    evidence_manifest = %s::jsonb,
                    generation_profile = %s::jsonb,
                    prompt_tokens = %s,
                    completion_tokens = %s,
                    total_tokens = %s,
                    reserved_tokens = CASE WHEN %s = 'running' THEN reserved_tokens ELSE 0 END,
                    estimated_cost_usd = %s,
                    latency_ms = %s,
                    retrieval_latency_ms = %s,
                    llm_latency_ms = %s,
                    eval_score = %s,
                    eval_details = %s::jsonb,
                    critic_iterations = %s,
                    events = %s::jsonb,
                    status = %s,
                    error_message = %s,
                    completed_at = CASE WHEN %s = 'running' THEN NULL ELSE NOW() END
                WHERE id = %s AND tenant_id = %s AND user_id = %s AND status = 'running'
                """,
                (
                    json.dumps(state.get("plan")),
                    state.get("model", "auto"),
                    [chunk["chunk_id"] for chunk in state.get("retrieved_chunks", [])],
                    response,
                    json.dumps(state.get("citations", [])),
                    json.dumps(state.get("claim_evidence", [])),
                    state.get("answer_status", "pending"),
                    state.get("grounding_profile", GROUNDING_PROFILE),
                    json.dumps(state.get("evidence_manifest", {})),
                    json.dumps(state.get("generation_profile", {})),
                    prompt_tokens,
                    completion_tokens,
                    prompt_tokens + completion_tokens,
                    status,
                    state.get("estimated_cost_usd"),
                    latency_ms,
                    state.get("retrieval_latency_ms", 0),
                    state.get("llm_latency_ms", 0),
                    state.get("eval_score", 0.0),
                    json.dumps(state.get("eval_details", {})),
                    state.get("iteration_count", 0),
                    json.dumps(state.get("agent_events", [])),
                    status,
                    error_message,
                    status,
                    run_id,
                    state["tenant_id"],
                    state["user_id"],
                ),
            )
            if cursor.rowcount != 1:
                raise ChatRunConflict("The run is no longer active")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        release_db_connection(conn)


def _initial_agent_state(
    query: str,
    tenant_id: str,
    user_id: str,
    session_id: str,
    model_override: Optional[str],
    run_id: str,
    cancel_event: Optional[Event] = None,
    replay_of_run_id: Optional[str] = None,
    replay_mode: str = "original",
    selected_document_ids: Optional[List[str]] = None,
    selected_version_scope: str = "auto",
    conversation_context: Optional[Dict[str, Any]] = None,
) -> AgentState:
    if selected_version_scope not in {"auto", "all_history", "current_only"}:
        raise ValueError("Unsupported product-selected version scope")
    canonical_document_ids = sorted({
        str(uuid.UUID(document_id))
        for document_id in (selected_document_ids or [])
    })
    fingerprint_payload = {
        "model": model_override or "auto",
        "query": query,
        "replay_mode": replay_mode,
        "replay_of_run_id": replay_of_run_id,
        "selected_document_ids": canonical_document_ids,
        "session_id": session_id,
        "conversation_context": conversation_context or {},
    }
    if selected_version_scope != "auto":
        fingerprint_payload["selected_version_scope"] = selected_version_scope
    request_fingerprint = hashlib.sha256(
        json.dumps(
            fingerprint_payload,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return {
        "query": query,
        "selected_document_ids": canonical_document_ids,
        "selected_version_scope": selected_version_scope,
        "request_fingerprint": request_fingerprint,
        "user_id": user_id,
        "tenant_id": tenant_id,
        "session_id": session_id,
        "conversation_context": conversation_context or {},
        "model": model_override or "auto",
        "plan": None,
        "retrieved_chunks": [],
        "retrieved_memories": [],
        "graph_context": {},
        "tool_calls": [],
        "tool_results": [],
        "response": "",
        "citations": [],
        "claim_evidence": [],
        "answer_status": "pending",
        "grounding_profile": GROUNDING_PROFILE,
        "evidence_manifest": {
            "profile": "pending:v1",
            "source_count": 0,
            "sources": [],
        },
        "generation_profile": {"profile": "pending:v1"},
        "replay_of_run_id": replay_of_run_id,
        "replay_mode": replay_mode,
        "eval_score": 0.0,
        "eval_details": {},
        "iteration_count": 0,
        "agent_events": [],
        "trace_id": run_id,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "embedding_tokens": 0,
        "embedding_cost_usd": 0.0,
        "embedding_pricing_profile": None,
        "token_reservation": 0,
        "estimated_cost_usd": 0.0,
        "retrieval_latency_ms": 0,
        "llm_latency_ms": 0,
        "cancel_event": cancel_event,
    }


def _result_from_state(state: AgentState, run_id: str, latency_ms: int) -> Dict[str, Any]:
    prompt_tokens = state.get("prompt_tokens", 0)
    completion_tokens = state.get("completion_tokens", 0)
    return {
        "run_id": run_id,
        "session_id": state["session_id"],
        "response": state.get("response", ""),
        "model_used": state.get("model", "auto"),
        "citations": state.get("citations", []),
        "claims": state.get("claim_evidence", []),
        "answer_status": state.get("answer_status", "pending"),
        "grounding_profile": state.get("grounding_profile", GROUNDING_PROFILE),
        "replay_of_run_id": state.get("replay_of_run_id"),
        "replay_mode": state.get("replay_mode", "original"),
        "provenance": _provenance_summary(
            state.get("evidence_manifest", {}),
            state.get("generation_profile", {}),
            state.get("conversation_context", {}),
        ),
        "eval_score": state.get("eval_score", 0.0),
        "latency_ms": latency_ms,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
        "estimated_cost_usd": state.get("estimated_cost_usd"),
        "agent_events": state.get("agent_events", []),
    }


def _done_event(result: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "type": "done",
        "citations": result["citations"],
        "claims": result["claims"],
        "answer_status": result["answer_status"],
        "grounding_profile": result["grounding_profile"],
        "replay_of_run_id": result.get("replay_of_run_id"),
        "replay_mode": result.get("replay_mode", "original"),
        "provenance": result["provenance"],
        "eval_score": result["eval_score"],
        "run_id": result["run_id"],
        "session_id": result["session_id"],
        "model_used": result["model_used"],
        "prompt_tokens": result["prompt_tokens"],
        "completion_tokens": result["completion_tokens"],
        "total_tokens": result["total_tokens"],
        "estimated_cost_usd": result["estimated_cost_usd"],
    }


class MultiAgentOrchestrator:
    @staticmethod
    def execute(
        query: str,
        tenant_id: str,
        user_id: str,
        session_id: Optional[str] = None,
        model_override: Optional[str] = None,
        request_id: Optional[str] = None,
        replay_of_run_id: Optional[str] = None,
        replay_mode: str = "original",
        document_ids: Optional[List[str]] = None,
        version_scope: str = "auto",
        conversation_context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        start_time = time.time()
        run_id = request_id or str(uuid.uuid4())
        state = _initial_agent_state(
            query,
            tenant_id,
            user_id,
            session_id or str(uuid.uuid4()),
            model_override,
            run_id,
            replay_of_run_id=replay_of_run_id,
            replay_mode=replay_mode,
            selected_document_ids=document_ids,
            selected_version_scope=version_scope,
            conversation_context=conversation_context,
        )
        cached = _claim_agent_run(state, run_id)
        if cached:
            return cached

        try:
            final_state = app_graph.invoke(state)
            latency_ms = int((time.time() - start_time) * 1000)
            _persist_agent_run(final_state, run_id, latency_ms, "completed")
            return _result_from_state(final_state, run_id, latency_ms)
        except Exception as error:
            latency_ms = int((time.time() - start_time) * 1000)
            try:
                _persist_agent_run(
                    state,
                    run_id,
                    latency_ms,
                    "failed",
                    error_message=type(error).__name__,
                )
            except Exception as persistence_error:
                logger.error("Failed to persist agent run failure: %s", persistence_error)
            raise

    @staticmethod
    def execute_frozen_replay(
        query: str,
        tenant_id: str,
        user_id: str,
        original_run_id: str,
        evidence_manifest: Dict[str, Any],
        conversation_context: Dict[str, Any],
        model_override: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Regenerate against the exact retained evidence pack without new retrieval/tools."""
        frozen_evidence = reconstruct_frozen_evidence(
            evidence_manifest,
            tenant_id,
            user_id,
            DATABASE_URL,
        )
        start_time = time.time()
        run_id = str(uuid.uuid4())
        state = _initial_agent_state(
            query,
            tenant_id,
            user_id,
            str(uuid.uuid4()),
            model_override,
            run_id,
            replay_of_run_id=original_run_id,
            replay_mode="frozen_evidence",
            conversation_context=conversation_context,
        )
        cached = _claim_agent_run(state, run_id)
        if cached:
            return cached

        try:
            state.update(router_node(state))
            state["tool_calls"] = []
            state.update(planner_node(state))
            state.update(frozen_evidence)
            replay_events = list(state.get("agent_events", []))
            replay_events.append({
                "agent": "researcher",
                "action": (
                    "Reconstructed the exact frozen evidence pack without fresh retrieval "
                    "or external tool execution"
                ),
                "status": "completed",
                "details": {
                    "replay_of_run_id": original_run_id,
                    "evidence_source_count": evidence_manifest.get("source_count", 0),
                    "evidence_manifest_sha256": evidence_manifest.get("canonical_sha256"),
                },
                "timestamp": time.time(),
            })
            state["agent_events"] = replay_events
            state.update(executor_node(state))
            state.update(critic_node(state))
            latency_ms = int((time.time() - start_time) * 1000)
            _persist_agent_run(state, run_id, latency_ms, "completed")
            return _result_from_state(state, run_id, latency_ms)
        except Exception as error:
            latency_ms = int((time.time() - start_time) * 1000)
            try:
                _persist_agent_run(
                    state,
                    run_id,
                    latency_ms,
                    "failed",
                    error_message=type(error).__name__,
                )
            except Exception as persistence_error:
                logger.error("Failed to persist frozen replay failure: %s", persistence_error)
            raise

    @staticmethod
    def execute_streaming(
        query: str,
        tenant_id: str,
        user_id: str,
        session_id: str,
        model_override: Optional[str],
        request_id: str,
        emit_event: Callable[[Dict[str, Any]], None],
        cancel_event: Event,
        document_ids: Optional[List[str]] = None,
        version_scope: str = "auto",
    ) -> Dict[str, Any]:
        start_time = time.time()
        partial_response: List[str] = []

        def emit_and_track(event: Dict[str, Any]) -> None:
            if event.get("type") == "token" and isinstance(event.get("content"), str):
                partial_response.append(event["content"])
            emit_event(event)

        state = _initial_agent_state(
            query,
            tenant_id,
            user_id,
            session_id,
            model_override,
            request_id,
            cancel_event=cancel_event,
            selected_document_ids=document_ids,
            selected_version_scope=version_scope,
        )
        cached = _claim_agent_run(state, request_id)
        if cached:
            for agent_event in cached["agent_events"]:
                emit_event({"type": "agent_event", **agent_event})
            if cached["response"]:
                emit_event({"type": "token", "content": cached["response"]})
            emit_event(_done_event(cached))
            return cached

        emitted_agent_events = 0
        try:
            for update in app_graph.stream(state, stream_mode="updates"):
                _raise_if_cancelled(state)
                if isinstance(update, dict):
                    for node_update in update.values():
                        if isinstance(node_update, dict):
                            state.update(node_update)

                current_events = state.get("agent_events", [])
                for agent_event in current_events[emitted_agent_events:]:
                    emit_event({"type": "agent_event", **agent_event})
                emitted_agent_events = len(current_events)

                latency_ms = int((time.time() - start_time) * 1000)
                _persist_agent_run(
                    state,
                    request_id,
                    latency_ms,
                    "running",
                    "".join(partial_response),
                )

            _raise_if_cancelled(state)
            if not partial_response and state.get("response"):
                emit_and_track({"type": "token", "content": state["response"]})

            latency_ms = int((time.time() - start_time) * 1000)
            _persist_agent_run(
                state,
                request_id,
                latency_ms,
                "completed",
                "".join(partial_response),
            )
            result = _result_from_state(state, request_id, latency_ms)
            emit_event(_done_event(result))
            return result
        except ChatRunCancelled:
            latency_ms = int((time.time() - start_time) * 1000)
            try:
                _persist_agent_run(
                    state,
                    request_id,
                    latency_ms,
                    "interrupted",
                    "".join(partial_response),
                    "Client disconnected",
                )
            except Exception as persistence_error:
                logger.error("Failed to persist interrupted agent run: %s", persistence_error)
            raise
        except Exception as error:
            latency_ms = int((time.time() - start_time) * 1000)
            try:
                _persist_agent_run(
                    state,
                    request_id,
                    latency_ms,
                    "failed",
                    "".join(partial_response),
                    type(error).__name__,
                )
            except Exception as persistence_error:
                logger.error("Failed to persist failed agent run: %s", persistence_error)
            raise
