import os
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from secrets import compare_digest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
import uvicorn
import psycopg2
from dotenv import load_dotenv

MODULE_PATH = Path(__file__).resolve()
REPOSITORY_ROOT = next(
    (parent for parent in MODULE_PATH.parents if (parent / ".env.example").exists()),
    MODULE_PATH.parents[1],
)
load_dotenv(REPOSITORY_ROOT / ".env")

from app.api.chat import router as chat_router
from app.api.traces import router as traces_router
from app.api.memories import router as memories_router
from app.api.automations import router as automations_router
from app.api.analytics import router as analytics_router
from app.api.export import router as export_router
from app.api.tasks import router as tasks_router
from app.api.graph import router as graph_router
from app.api.dashboard import router as dashboard_router
from app.api.notifications import router as notifications_router
from app.api.search import router as search_router
from app.api.webhooks import router as webhooks_router
from app.core.db import DatabasePoolTimeout, close_db_pool, get_db_cursor
from app.core.runtime import close_runtime_resources
from app.retrieval.graphrag import close_graph_driver

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("orchestration_service")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    yield
    # Retrieval workers finish/cancel before their shared transports are closed.
    close_runtime_resources()
    close_graph_driver()
    close_db_pool()

app = FastAPI(
    title="Certus Orchestration & Cognitive Architecture Service",
    description="Modular LangGraph multi-agent execution, GraphRAG, Traces Replay, Memory, and Automations Engine",
    version="1.0.0",
    lifespan=lifespan,
)

INTERNAL_SERVICE_TOKEN = os.getenv("INTERNAL_SERVICE_TOKEN", "")
WORKER_HEARTBEAT_STALE_SECONDS = int(
    os.getenv("WORKER_HEARTBEAT_STALE_SECONDS", "30")
)
if not 15 <= WORKER_HEARTBEAT_STALE_SECONDS <= 300:
    raise ValueError("WORKER_HEARTBEAT_STALE_SECONDS must be between 15 and 300")


@app.exception_handler(DatabasePoolTimeout)
async def database_pool_timeout_handler(_request: Request, error: DatabasePoolTimeout):
    return JSONResponse(
        status_code=503,
        content={"detail": str(error)},
        headers={"Retry-After": "1"},
    )

@app.middleware("http")
async def require_internal_service_token(request: Request, call_next):
    if request.url.path in {"/health", "/health/ready"}:
        return await call_next(request)
    provided_token = request.headers.get("x-internal-service-token", "")
    if not INTERNAL_SERVICE_TOKEN:
        return JSONResponse(status_code=503, content={"detail": "Service authentication is not configured"})
    if not compare_digest(provided_token, INTERNAL_SERVICE_TOKEN):
        return JSONResponse(status_code=401, content={"detail": "Unauthorized"})
    return await call_next(request)

@app.get("/health")
def health():
    return {"status": "ok", "service": "orchestration", "version": "1.0.0"}


@app.get("/health/ready")
def readiness():
    database_ready = False
    worker_capabilities = {
        "embedding": {"status": "unseen"},
        "workflows": {"status": "unseen"},
    }
    try:
        with get_db_cursor(dict_cursor=False) as cursor:
            cursor.execute("SET LOCAL statement_timeout = '2000ms'")
            cursor.execute("SELECT 1")
            database_ready = cursor.fetchone() == (1,)
            cursor.execute(
                """
                SELECT DISTINCT ON (worker_type)
                       worker_type,
                       CASE
                           WHEN status = 'stopped' THEN 'stopped'
                           WHEN status IN ('starting', 'running', 'draining')
                            AND heartbeat_at >= NOW() - (%s * INTERVAL '1 second')
                           THEN 'ready'
                           ELSE 'stale'
                       END AS health_status,
                       GREATEST(
                           0,
                           EXTRACT(EPOCH FROM (NOW() - heartbeat_at))::BIGINT
                       ) AS heartbeat_age_seconds
                FROM service_worker_heartbeats
                WHERE worker_type IN ('embedding-worker', 'workflow-worker')
                ORDER BY worker_type, heartbeat_at DESC
                """,
                (WORKER_HEARTBEAT_STALE_SECONDS,),
            )
            for worker_type, health_status, age_seconds in cursor.fetchall():
                capability_name = (
                    "embedding" if worker_type == "embedding-worker" else "workflows"
                )
                worker_capabilities[capability_name] = {
                    "status": health_status,
                    "heartbeat_age_seconds": int(age_seconds),
                }
    except (psycopg2.Error, DatabasePoolTimeout):
        pass
    return JSONResponse(
        status_code=200 if database_ready else 503,
        content={
            "status": "ready" if database_ready else "not_ready",
            "service": "orchestration",
            "dependencies": {"postgresql": "ready" if database_ready else "not_ready"},
            "capabilities": {
                "generation": "provider_optional",
                "knowledge_graph": "degrades_when_unavailable",
                "background_workers": worker_capabilities,
            },
        },
        headers={"Cache-Control": "no-store"},
    )

# Mount Modular API Routers
app.include_router(chat_router)
app.include_router(traces_router)
app.include_router(memories_router)
app.include_router(automations_router)
app.include_router(analytics_router)
app.include_router(export_router)
app.include_router(tasks_router)
app.include_router(graph_router)
app.include_router(dashboard_router)
app.include_router(notifications_router)
app.include_router(search_router)
app.include_router(webhooks_router)

if __name__ == "__main__":
    port = int(os.getenv("PORT", 8002))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=True)
