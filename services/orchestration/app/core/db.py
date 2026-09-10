from contextlib import contextmanager
import os
from threading import BoundedSemaphore, Lock
from typing import Iterator

import psycopg2
from psycopg2.extensions import connection as PsycopgConnection
from psycopg2.extras import RealDictCursor
from psycopg2.pool import ThreadedConnectionPool

from app.core.config import settings


class DatabasePoolTimeout(RuntimeError):
    """Raised when request concurrency exhausts the bounded database pool."""


_pool: ThreadedConnectionPool | None = None
_pool_pid: int | None = None
_pool_lock = Lock()
_pool_slots = BoundedSemaphore(settings.DB_POOL_MAX_SIZE)


def _get_pool() -> ThreadedConnectionPool:
    global _pool, _pool_pid
    process_id = os.getpid()
    if _pool is not None and _pool_pid == process_id:
        return _pool
    with _pool_lock:
        if _pool is None or _pool_pid != process_id:
            # Pools and their sockets are process-owned. Lazy construction means
            # normal pre-fork servers create one only after the worker starts.
            _pool = ThreadedConnectionPool(
                settings.DB_POOL_MIN_SIZE,
                settings.DB_POOL_MAX_SIZE,
                dsn=settings.DATABASE_URL,
                application_name="certus-orchestration",
                connect_timeout=settings.DB_CONNECT_TIMEOUT_SECONDS,
            )
            _pool_pid = process_id
    return _pool


@contextmanager
def get_db_connection() -> Iterator[PsycopgConnection]:
    connection = acquire_db_connection()
    transaction_resolved = False
    try:
        yield connection
        connection.commit()
        transaction_resolved = True
    except Exception:
        if not connection.closed:
            try:
                connection.rollback()
                transaction_resolved = True
            except Exception:
                pass
        raise
    finally:
        release_db_connection(connection, reset=not transaction_resolved)


def acquire_db_connection() -> PsycopgConnection:
    """Lease a clean process-owned connection for code managing its transaction."""
    if not _pool_slots.acquire(timeout=settings.DB_POOL_ACQUIRE_TIMEOUT_SECONDS):
        raise DatabasePoolTimeout(
            "The orchestration database pool is at capacity; retry the request"
        )
    connection: PsycopgConnection | None = None
    try:
        connection = _get_pool().getconn()
        if connection.closed:
            raise psycopg2.InterfaceError(
                "The database pool returned a closed connection"
            )
        # A returned connection must never leak transaction state across users.
        connection.rollback()
        return connection
    except Exception:
        if connection is not None:
            try:
                _get_pool().putconn(connection, close=True)
            except Exception:
                try:
                    connection.close()
                except Exception:
                    pass
        _pool_slots.release()
        raise


def release_db_connection(
    connection: PsycopgConnection,
    *,
    reset: bool = True,
) -> None:
    """Return a lease without allowing transaction state to cross requests."""
    discard = bool(connection.closed)
    if reset and not discard:
        try:
            connection.rollback()
        except Exception:
            discard = True
    try:
        _get_pool().putconn(connection, close=discard)
    except Exception:
        try:
            connection.close()
        except Exception:
            pass
    finally:
        _pool_slots.release()


@contextmanager
def get_db_cursor(dict_cursor: bool = True):
    with get_db_connection() as connection:
        with connection.cursor(
            cursor_factory=RealDictCursor if dict_cursor else None
        ) as cursor:
            yield cursor


def close_db_pool() -> None:
    """Close process-owned pooled connections during service shutdown."""
    global _pool, _pool_pid
    with _pool_lock:
        pool = _pool
        _pool = None
        _pool_pid = None
    if pool is not None:
        pool.closeall()
