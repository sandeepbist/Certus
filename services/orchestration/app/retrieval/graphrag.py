import os
import re
import logging
from contextlib import contextmanager
from threading import Lock
from typing import List, Dict, Any

from services.shared.safe_errors import safe_error_summary
from services.shared.document_graph import DocumentGraph

logger = logging.getLogger("graphrag")

NEO4J_URI = os.getenv("NEO4J_URI", "bolt://localhost:7687")
NEO4J_USER = os.getenv("NEO4J_USER", "neo4j")
NEO4J_PASS = os.getenv("NEO4J_PASSWORD", "nexus_neo4j_dev")
NEO4J_QUERY_TIMEOUT_SECONDS = float(os.getenv("NEO4J_QUERY_TIMEOUT_SECONDS", "5"))
NEO4J_CONNECTION_TIMEOUT_SECONDS = float(
    os.getenv("NEO4J_CONNECTION_TIMEOUT_SECONDS", "3")
)
NEO4J_CONNECTION_ACQUIRE_TIMEOUT_SECONDS = float(
    os.getenv("NEO4J_CONNECTION_ACQUIRE_TIMEOUT_SECONDS", "2")
)
NEO4J_MAX_CONNECTION_POOL_SIZE = int(
    os.getenv("NEO4J_MAX_CONNECTION_POOL_SIZE", "12")
)

if not 0.1 <= NEO4J_QUERY_TIMEOUT_SECONDS <= 60:
    raise ValueError("NEO4J_QUERY_TIMEOUT_SECONDS must be between 0.1 and 60")
if not 0.1 <= NEO4J_CONNECTION_TIMEOUT_SECONDS <= 30:
    raise ValueError("NEO4J_CONNECTION_TIMEOUT_SECONDS must be between 0.1 and 30")
if not 0.1 <= NEO4J_CONNECTION_ACQUIRE_TIMEOUT_SECONDS <= 30:
    raise ValueError("NEO4J_CONNECTION_ACQUIRE_TIMEOUT_SECONDS must be 0.1..30")
if not 1 <= NEO4J_MAX_CONNECTION_POOL_SIZE <= 100:
    raise ValueError("NEO4J_MAX_CONNECTION_POOL_SIZE must be between 1 and 100")

_driver = None
_driver_lock = Lock()


def get_graph_driver():
    global _driver
    if _driver is not None:
        return _driver
    with _driver_lock:
        if _driver is None:
            from neo4j import GraphDatabase

            _driver = GraphDatabase.driver(
                NEO4J_URI,
                auth=(NEO4J_USER, NEO4J_PASS),
                connection_timeout=NEO4J_CONNECTION_TIMEOUT_SECONDS,
                connection_acquisition_timeout=NEO4J_CONNECTION_ACQUIRE_TIMEOUT_SECONDS,
                max_connection_pool_size=NEO4J_MAX_CONNECTION_POOL_SIZE,
            )
    return _driver


def bounded_graph_query(text: str):
    from neo4j import Query

    return Query(text, timeout=NEO4J_QUERY_TIMEOUT_SECONDS)


def close_graph_driver() -> None:
    global _driver
    with _driver_lock:
        driver = _driver
        _driver = None
    if driver is not None:
        driver.close()


@contextmanager
def _graph_database():
    from ..core.db import get_db_connection
    with get_db_connection() as connection:
        yield connection


document_graph = DocumentGraph(_graph_database, get_graph_driver, timeout_seconds=NEO4J_QUERY_TIMEOUT_SECONDS)

class GraphRAGEngine:
    @staticmethod
    def extract_query_entities(query: str) -> List[str]:
        words = [word.casefold() for word in re.findall(r'\b[A-Za-z0-9_-]{2,}\b', query)]
        stop_words = {
            "what", "when", "where", "which", "who", "how", "does", "certus", "nexus",
            "with", "from", "that", "this", "have", "been", "used", "connect", "connects",
            "connected", "between", "about", "show", "find", "graph", "knowledge",
        }
        words = [word for word in words if len(word) > 2 and word not in stop_words]
        candidates = set(words)
        for width in (2, 3):
            for index in range(len(words) - width + 1):
                candidates.add(" ".join(words[index:index + width]))
        return sorted(candidates, key=lambda value: (-len(value.split()), value))[:30]

    @staticmethod
    def traverse_graph(
        entities: List[str], user_id: str, tenant_id: str, max_depth: int = 2,
    ) -> Dict[str, Any]:
        if not entities:
            return {"graph_triples": [], "connected_entities": [], "relationships_count": 0}
        depth = max(1, min(int(max_depth), 2))
        try:
            projection = document_graph.traverse(entities, tenant_id, user_id, depth)
            relations = projection.rows
            return {
                "graph_triples": [f"({row['root']}) -[CO_MENTIONED]- ({row['target']} [{row['target_type']}])"
                                  for row in relations],
                "connected_entities": sorted({row["target"] for row in relations}),
                "relationships_count": len(relations), "depth": depth,
                "projection_status": projection.status,
            }
        except Exception as error:
            logger.warning("GraphRAG traversal error: %s",
                           safe_error_summary(error, operation="GraphRAG traversal"))
            raise
