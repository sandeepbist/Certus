"""Bounded dependency connections and durable background-worker heartbeats."""

from __future__ import annotations

import json
import os
import re
import socket
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping

import psycopg2


WORKER_STATUSES = frozenset({"starting", "running", "draining", "stopped"})


def bounded_int_env(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError as error:
        raise ValueError(f"{name} must be an integer") from error
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


def bounded_retention_days_env(name: str, default: int) -> int:
    """Read an operational-history retention window with a ten-year safety cap."""
    return bounded_int_env(name, default, 1, 3_650)


def connect_database(
    database_url: str,
    *,
    application_name: str,
    connect_timeout_seconds: int,
    statement_timeout_ms: int | None = None,
    lock_timeout_ms: int | None = None,
):
    options = []
    if statement_timeout_ms is not None:
        options.append(f"-c statement_timeout={statement_timeout_ms}")
    if lock_timeout_ms is not None:
        options.append(f"-c lock_timeout={lock_timeout_ms}")
    connection_options = {
        "application_name": application_name,
        "connect_timeout": connect_timeout_seconds,
    }
    if options:
        connection_options["options"] = " ".join(options)
    return psycopg2.connect(database_url, **connection_options)


def redis_connection_options(
    *,
    connect_timeout_seconds: int,
    socket_timeout_seconds: int,
) -> dict[str, Any]:
    return {
        "decode_responses": True,
        "socket_connect_timeout": connect_timeout_seconds,
        "socket_timeout": socket_timeout_seconds,
        "socket_keepalive": True,
        "health_check_interval": 30,
    }


@dataclass(frozen=True)
class WorkerIdentity:
    worker_type: str
    instance_id: uuid.UUID = field(default_factory=uuid.uuid4)
    hostname: str = field(default_factory=socket.gethostname)
    process_id: int = field(default_factory=os.getpid)

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[a-z][a-z0-9_-]{1,63}", self.worker_type):
            raise ValueError("worker_type must use 2-64 lowercase identifier characters")
        if not self.hostname or len(self.hostname) > 255:
            raise ValueError("hostname must contain between 1 and 255 characters")
        if self.process_id <= 0:
            raise ValueError("process_id must be positive")


def write_worker_heartbeat(
    database_url: str,
    identity: WorkerIdentity,
    status: str,
    metadata: Mapping[str, Any],
    *,
    connect_timeout_seconds: int,
) -> datetime:
    if status not in WORKER_STATUSES:
        raise ValueError(f"Unsupported worker status: {status}")
    encoded_metadata = json.dumps(metadata, separators=(",", ":"), sort_keys=True)
    if len(encoded_metadata.encode("utf-8")) > 12_000:
        raise ValueError("Worker heartbeat metadata exceeds 12000 bytes")

    recorded_at = datetime.now(timezone.utc)
    with connect_database(
        database_url,
        application_name=f"certus-{identity.worker_type}-heartbeat",
        connect_timeout_seconds=connect_timeout_seconds,
        statement_timeout_ms=2_000,
        lock_timeout_ms=1_000,
    ) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SET LOCAL statement_timeout = '2000ms'")
            cursor.execute(
                """
                INSERT INTO service_worker_heartbeats (
                    worker_type, instance_id, hostname, process_id, status,
                    started_at, heartbeat_at, stopped_at, metadata
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s,
                    CASE WHEN %s = 'stopped' THEN %s ELSE NULL END,
                    %s::jsonb
                )
                ON CONFLICT (worker_type, instance_id) DO UPDATE SET
                    hostname = EXCLUDED.hostname,
                    process_id = EXCLUDED.process_id,
                    status = EXCLUDED.status,
                    heartbeat_at = EXCLUDED.heartbeat_at,
                    stopped_at = EXCLUDED.stopped_at,
                    metadata = EXCLUDED.metadata
                """,
                (
                    identity.worker_type,
                    str(identity.instance_id),
                    identity.hostname,
                    identity.process_id,
                    status,
                    recorded_at,
                    recorded_at,
                    status,
                    recorded_at,
                    encoded_metadata,
                ),
            )
            # Heartbeat history is operational, not product data. Keep terminal
            # rows briefly for diagnosis without allowing unbounded growth.
            cursor.execute(
                """
                DELETE FROM service_worker_heartbeats
                WHERE stopped_at < NOW() - INTERVAL '7 days'
                """
            )
    return recorded_at
