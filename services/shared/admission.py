"""Small async admission primitive for bounded blocking service lanes."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import AsyncIterator


class AdmissionCapacityExceeded(RuntimeError):
    """The lane did not gain capacity within its bounded queue wait."""


class AsyncAdmissionController:
    def __init__(self, capacity: int, queue_timeout_ms: int) -> None:
        if not 1 <= capacity <= 1_000:
            raise ValueError("capacity must be between 1 and 1000")
        if not 1 <= queue_timeout_ms <= 60_000:
            raise ValueError("queue_timeout_ms must be between 1 and 60000")
        self.capacity = capacity
        self.queue_timeout_ms = queue_timeout_ms
        self._slots = asyncio.Semaphore(capacity)

    @asynccontextmanager
    async def acquire(self) -> AsyncIterator[None]:
        try:
            await asyncio.wait_for(
                self._slots.acquire(),
                timeout=self.queue_timeout_ms / 1_000,
            )
        except TimeoutError as error:
            raise AdmissionCapacityExceeded("admission capacity is full") from error
        try:
            yield
        finally:
            self._slots.release()
