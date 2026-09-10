import json
import asyncio
import logging
from threading import Event
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, UUID4
from typing import Optional, List, Dict, Any, Literal
from app.agents.graph import (
    ChatBudgetExceeded,
    ChatRunCancelled,
    ChatRunConflict,
    MultiAgentOrchestrator,
)
from app.core.identity import RequestIdentity, require_request_identity
from app.core.db import get_db_cursor

router = APIRouter(prefix="", tags=["Chat & Streaming"])
logger = logging.getLogger("orchestration_chat")


def _preferred_model(identity: RequestIdentity, requested_model: Optional[str]) -> Optional[str]:
    if requested_model and requested_model != "auto":
        return requested_model
    with get_db_cursor() as cursor:
        cursor.execute(
            """
            SELECT default_model
            FROM user_preferences
            WHERE user_id = %s AND organization_id = %s
            """,
            (identity.user_id, identity.tenant_id),
        )
        preferences = cursor.fetchone()
    preferred = preferences["default_model"] if preferences else "auto"
    return None if preferred == "auto" else preferred

class ChatQueryRequest(BaseModel):
    query: str = Field(min_length=1, max_length=20_000)
    session_id: Optional[UUID] = None
    model: Optional[str] = None
    request_id: Optional[UUID4] = None
    document_ids: List[UUID] = Field(default_factory=list, max_length=10)
    version_scope: Literal["auto", "all_history", "current_only"] = "auto"

class ChatQueryResponse(BaseModel):
    run_id: str
    session_id: str
    response: str
    model_used: str
    eval_score: float
    latency_ms: int
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    estimated_cost_usd: Optional[float] = None
    citations: List[Dict[str, Any]] = Field(default_factory=list)
    claims: List[Dict[str, Any]] = Field(default_factory=list)
    answer_status: str
    grounding_profile: str
    provenance: Dict[str, Any] = Field(default_factory=dict)
    replay_of_run_id: Optional[str] = None
    replay_mode: str = "original"
    agent_events: List[Dict[str, Any]] = Field(default_factory=list)

@router.post("/chat", response_model=ChatQueryResponse)
async def chat_endpoint(
    req: ChatQueryRequest,
    identity: RequestIdentity = Depends(require_request_identity),
):
    if not req.query.strip():
        raise HTTPException(status_code=400, detail="Query cannot be empty")
    
    preferred_model = await asyncio.to_thread(_preferred_model, identity, req.model)
    session_id = str(req.session_id or req.request_id or uuid4())
    try:
        result = await asyncio.to_thread(
            MultiAgentOrchestrator.execute,
            req.query,
            identity.tenant_id,
            identity.user_id,
            session_id,
            preferred_model,
            str(req.request_id) if req.request_id else None,
            document_ids=[str(document_id) for document_id in req.document_ids],
            version_scope=req.version_scope,
        )
    except ChatRunConflict as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except ChatBudgetExceeded as error:
        raise HTTPException(status_code=429, detail=str(error)) from error
    return ChatQueryResponse(**result)

@router.post("/chat/stream")
async def chat_stream_endpoint(
    req: ChatQueryRequest,
    identity: RequestIdentity = Depends(require_request_identity),
):
    preferred_model = await asyncio.to_thread(_preferred_model, identity, req.model)
    request_id = str(req.request_id) if req.request_id else None
    if not request_id:
        raise HTTPException(status_code=422, detail="request_id is required for streaming chat")
    session_id = str(req.session_id or req.request_id)

    async def event_generator():
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[tuple[str, Optional[Dict[str, Any]]]] = asyncio.Queue()
        cancelled = Event()

        def emit_event(event: Dict[str, Any]) -> None:
            if cancelled.is_set():
                raise ChatRunCancelled("The downstream stream was closed")
            loop.call_soon_threadsafe(queue.put_nowait, ("event", event))

        def execute_run() -> None:
            try:
                MultiAgentOrchestrator.execute_streaming(
                    req.query,
                    identity.tenant_id,
                    identity.user_id,
                    session_id,
                    preferred_model,
                    request_id,
                    emit_event,
                    cancelled,
                    document_ids=[str(document_id) for document_id in req.document_ids],
                    version_scope=req.version_scope,
                )
            except ChatRunCancelled:
                pass
            except ChatBudgetExceeded as error:
                if not cancelled.is_set():
                    emit_event({
                        "type": "error",
                        "code": "TOKEN_BUDGET_EXCEEDED",
                        "message": str(error),
                    })
            except ChatRunConflict as error:
                if not cancelled.is_set():
                    emit_event({
                        "type": "error",
                        "code": "REQUEST_CONFLICT",
                        "message": str(error),
                    })
            except Exception as error:
                logger.exception("Streaming agent run failed", exc_info=error)
                if not cancelled.is_set():
                    emit_event({
                        "type": "error",
                        "code": "ORCHESTRATION_FAILED",
                        "message": "The agent run could not be completed.",
                    })
            finally:
                try:
                    loop.call_soon_threadsafe(queue.put_nowait, ("complete", None))
                except RuntimeError:
                    pass

        worker = asyncio.create_task(asyncio.to_thread(execute_run))
        try:
            while True:
                message_type, event = await queue.get()
                if message_type == "complete":
                    break
                yield f"data: {json.dumps(event, separators=(',', ':'))}\n\n"
        finally:
            cancelled.set()
            if worker.done():
                await worker
            else:
                try:
                    await asyncio.wait_for(asyncio.shield(worker), timeout=5)
                except asyncio.TimeoutError:
                    worker.add_done_callback(lambda task: task.exception())

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
        },
    )
