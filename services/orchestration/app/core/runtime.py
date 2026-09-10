from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import os
from threading import Lock
from typing import Any


RETRIEVAL_PARALLEL_WORKERS = int(
    os.getenv("ORCHESTRATION_RETRIEVAL_PARALLEL_WORKERS", "12")
)
RETRIEVAL_STAGE_TIMEOUT_SECONDS = float(
    os.getenv("ORCHESTRATION_RETRIEVAL_STAGE_TIMEOUT_SECONDS", "8")
)
RETRIEVAL_STATEMENT_TIMEOUT_MS = int(
    os.getenv("ORCHESTRATION_RETRIEVAL_STATEMENT_TIMEOUT_MS", "5000")
)
OPENAI_EMBEDDING_TIMEOUT_SECONDS = float(
    os.getenv("OPENAI_EMBEDDING_TIMEOUT_SECONDS", "10")
)
OPENAI_EMBEDDING_MAX_RETRIES = int(
    os.getenv("OPENAI_EMBEDDING_MAX_RETRIES", "1")
)

if not 3 <= RETRIEVAL_PARALLEL_WORKERS <= 64:
    raise ValueError("ORCHESTRATION_RETRIEVAL_PARALLEL_WORKERS must be between 3 and 64")
if not 1 <= RETRIEVAL_STAGE_TIMEOUT_SECONDS <= 60:
    raise ValueError("ORCHESTRATION_RETRIEVAL_STAGE_TIMEOUT_SECONDS must be between 1 and 60")
if not 100 <= RETRIEVAL_STATEMENT_TIMEOUT_MS <= 60_000:
    raise ValueError("ORCHESTRATION_RETRIEVAL_STATEMENT_TIMEOUT_MS must be 100..60000")
if not 1 <= OPENAI_EMBEDDING_TIMEOUT_SECONDS <= 60:
    raise ValueError("OPENAI_EMBEDDING_TIMEOUT_SECONDS must be between 1 and 60")
if not 0 <= OPENAI_EMBEDDING_MAX_RETRIES <= 3:
    raise ValueError("OPENAI_EMBEDDING_MAX_RETRIES must be between 0 and 3")


_resource_lock = Lock()
_retrieval_executor: ThreadPoolExecutor | None = None
_openai_clients: dict[tuple[str, str, float, int], Any] = {}


def get_retrieval_executor() -> ThreadPoolExecutor:
    global _retrieval_executor
    if _retrieval_executor is not None:
        return _retrieval_executor
    with _resource_lock:
        if _retrieval_executor is None:
            _retrieval_executor = ThreadPoolExecutor(
                max_workers=RETRIEVAL_PARALLEL_WORKERS,
                thread_name_prefix="certus-retrieval",
            )
    return _retrieval_executor


def get_openai_client(
    purpose: str,
    api_key: str,
    timeout_seconds: float,
    max_retries: int,
):
    """Reuse one immutable-config SDK client per process and purpose."""
    cache_key = (purpose, api_key, timeout_seconds, max_retries)
    client = _openai_clients.get(cache_key)
    if client is not None:
        return client
    with _resource_lock:
        client = _openai_clients.get(cache_key)
        if client is None:
            from openai import OpenAI

            client = OpenAI(
                api_key=api_key,
                timeout=timeout_seconds,
                max_retries=max_retries,
            )
            _openai_clients[cache_key] = client
    return client


def close_runtime_resources() -> None:
    """Release process-owned thread and HTTP pools during service shutdown."""
    global _retrieval_executor
    with _resource_lock:
        executor = _retrieval_executor
        _retrieval_executor = None
        clients = list(_openai_clients.values())
        _openai_clients.clear()
    if executor is not None:
        executor.shutdown(wait=True, cancel_futures=True)
    for client in clients:
        client.close()
