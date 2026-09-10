import os
import re
import logging
from threading import Lock
from typing import List, Dict, Any, Set

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
        entities: List[str],
        user_id: str,
        tenant_id: str,
        max_depth: int = 2,
    ) -> Dict[str, Any]:
        normalized_entities = sorted({entity.casefold().strip() for entity in entities if entity.strip()})
        if not normalized_entities:
            return {"graph_triples": [], "connected_entities": [], "relationships_count": 0}

        depth = max(1, min(int(max_depth), 2))

        try:
            triples: List[str] = []
            connected_set: Set[str] = set()
            seen_edges: Set[tuple[str, str]] = set()

            with get_graph_driver().session() as session:
                direct_results = session.run(
                    bounded_graph_query(
                        """
                MATCH (u:User {id: $user_id})-[:OWNS]->(d:Document {tenant_id: $tenant_id})-[:MENTIONS]->(e:Entity {tenant_id: $tenant_id})
                WHERE d.deleted_at IS NULL
                  AND e.normalized_name IN $entities
                MATCH (d)-[:MENTIONS]->(connected:Entity)
                WHERE connected <> e
                RETURN e.name AS root_entity,
                       connected.name AS target_entity,
                       connected.type AS target_type,
                       1 AS distance
                LIMIT 40
                        """
                    ),
                    entities=normalized_entities,
                    user_id=user_id,
                    tenant_id=tenant_id,
                )

                direct_entities: Set[str] = set()
                for record in direct_results:
                    root = record["root_entity"]
                    target = record["target_entity"]
                    target_type = record["target_type"] or "CONCEPT"
                    edge = tuple(sorted((root.casefold(), target.casefold())))
                    if edge in seen_edges:
                        continue
                    seen_edges.add(edge)
                    triples.append(f"({root}) -[CO_MENTIONED]- ({target} [{target_type}])")
                    connected_set.add(target)
                    direct_entities.add(target.casefold())

                if depth == 2 and direct_entities:
                    second_results = session.run(
                        bounded_graph_query(
                            """
                            MATCH (u:User {id: $user_id})-[:OWNS]->(d:Document {tenant_id: $tenant_id})
                                  -[:MENTIONS]->(middle:Entity {tenant_id: $tenant_id})
                            WHERE d.deleted_at IS NULL
                              AND middle.normalized_name IN $middle_entities
                            MATCH (d)-[:MENTIONS]->(target:Entity {tenant_id: $tenant_id})
                            WHERE target <> middle
                            RETURN middle.name AS root_entity,
                                   target.name AS target_entity,
                                   target.type AS target_type,
                                   2 AS distance
                            LIMIT 40
                            """
                        ),
                        middle_entities=sorted(direct_entities),
                        user_id=user_id,
                        tenant_id=tenant_id,
                    )
                    for record in second_results:
                        root = record["root_entity"]
                        target = record["target_entity"]
                        target_type = record["target_type"] or "CONCEPT"
                        if target.casefold() in normalized_entities:
                            continue
                        edge = tuple(sorted((root.casefold(), target.casefold())))
                        if edge in seen_edges:
                            continue
                        seen_edges.add(edge)
                        triples.append(f"({root}) -[CO_MENTIONED]- ({target} [{target_type}])")
                        connected_set.add(target)
            
            return {
                "graph_triples": triples,
                "connected_entities": list(connected_set),
                "relationships_count": len(triples),
                "depth": depth,
            }
        except Exception as e:
            logger.warning(f"GraphRAG traversal error: {e}")
            return {"graph_triples": [], "connected_entities": [], "relationships_count": 0}
