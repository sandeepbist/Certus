"""Fail-fast configuration and readiness for durable upload recovery."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Protocol

from services.shared.worker_runtime import bounded_int_env


class ThreadState(Protocol):
    def is_alive(self) -> bool: ...


def _strict_boolean_env(name: str, default: bool) -> bool:
    value = os.getenv(name, str(default).lower()).strip().lower()
    if value not in {"true", "false"}:
        raise ValueError(f"{name} must be true or false")
    return value == "true"


@dataclass(frozen=True)
class UploadReconcilerConfig:
    enabled: bool
    poll_seconds: int
    lease_seconds: int
    max_attempts: int
    shutdown_seconds: int

    @classmethod
    def from_environment(cls) -> "UploadReconcilerConfig":
        return cls(
            enabled=_strict_boolean_env("UPLOAD_RECONCILER_ENABLED", True),
            poll_seconds=bounded_int_env(
                "UPLOAD_RECONCILER_POLL_SECONDS", 5, 1, 300
            ),
            lease_seconds=bounded_int_env(
                "UPLOAD_RECONCILER_LEASE_SECONDS", 300, 60, 3_600
            ),
            max_attempts=bounded_int_env(
                "UPLOAD_RECONCILER_MAX_ATTEMPTS", 12, 1, 100
            ),
            shutdown_seconds=bounded_int_env(
                "UPLOAD_RECONCILER_SHUTDOWN_SECONDS", 30, 1, 300
            ),
        )


def upload_reconciler_is_ready(
    config: UploadReconcilerConfig,
    worker: ThreadState | None,
) -> bool:
    return not config.enabled or (worker is not None and worker.is_alive())
