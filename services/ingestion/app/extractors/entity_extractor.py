import re
import os
import threading
from typing import Any, Dict, List

from neo4j import GraphDatabase
from services.shared.safe_errors import safe_error_summary, safe_log_value
from services.shared.worker_runtime import bounded_int_env

KNOWN_TECHNOLOGIES = {
    "postgresql", "pgvector", "redis", "neo4j", "fastapi", "fastify", "next.js", "nextjs",
    "react", "langgraph", "langchain", "temporal", "temporal.io", "docker", "kubernetes",
    "graphql", "grpc", "protobuf", "tailwind", "better auth", "python", "typescript",
    "openai", "gpt-4o", "claude", "gemini", "embedding", "hnsw", "rag", "vector"
}
MAX_EXTRACTED_ENTITIES = 250
NEO4J_CONNECT_TIMEOUT_SECONDS = bounded_int_env(
    "INGESTION_NEO4J_CONNECT_TIMEOUT_SECONDS", 3, 1, 30
)
NEO4J_ACQUISITION_TIMEOUT_SECONDS = bounded_int_env(
    "INGESTION_NEO4J_ACQUISITION_TIMEOUT_SECONDS", 5, 1, 60
)
NEO4J_QUERY_TIMEOUT_SECONDS = bounded_int_env(
    "INGESTION_NEO4J_QUERY_TIMEOUT_SECONDS", 10, 1, 120
)
NEO4J_POOL_SIZE = bounded_int_env("INGESTION_NEO4J_POOL_SIZE", 20, 1, 100)

_graph_driver = None
_graph_driver_lock = threading.Lock()


def entity_graph_driver():
    global _graph_driver
    if _graph_driver is None:
        with _graph_driver_lock:
            if _graph_driver is None:
                _graph_driver = GraphDatabase.driver(
                    os.getenv("NEO4J_URI", "bolt://localhost:7687"),
                    auth=(
                        os.getenv("NEO4J_USER", "neo4j"),
                        os.getenv("NEO4J_PASSWORD", "nexus_neo4j_dev"),
                    ),
                    connection_timeout=NEO4J_CONNECT_TIMEOUT_SECONDS,
                    connection_acquisition_timeout=NEO4J_ACQUISITION_TIMEOUT_SECONDS,
                    max_connection_pool_size=NEO4J_POOL_SIZE,
                    max_transaction_retry_time=NEO4J_QUERY_TIMEOUT_SECONDS,
                )
    return _graph_driver


def close_entity_graph_driver() -> None:
    global _graph_driver
    with _graph_driver_lock:
        driver = _graph_driver
        _graph_driver = None
    if driver is not None:
        driver.close()


CAPITALIZED_TERM_STOPLIST = {
    "accept", "add", "all", "and", "because", "build", "configure", "current",
    "each", "every", "exercise", "for", "how", "keep", "official", "pin",
    "recommended", "return", "search", "see", "that", "the", "then", "therefore",
    "they", "this", "tool", "translate", "true", "use", "what", "when", "where",
}

class ExtractedEntity:
    def __init__(self, name: str, entity_type: str, mention_count: int = 1):
        self.name = name
        self.type = entity_type
        self.mention_count = mention_count

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "type": self.type,
            "mention_count": self.mention_count
        }

class EntityExtractor:
    @staticmethod
    def extract_from_text(text: str) -> List[ExtractedEntity]:
        entities: Dict[str, ExtractedEntity] = {}
        
        # 1. Match known technologies (case-insensitive)
        lower_text = text.lower()
        for tech in sorted(KNOWN_TECHNOLOGIES):
            pattern = rf'\b{re.escape(tech)}\b'
            matches = len(re.findall(pattern, lower_text))
            if matches > 0:
                canonical = tech.upper() if len(tech) <= 4 else tech.title()
                entities[canonical.casefold()] = ExtractedEntity(canonical, "TECHNOLOGY", matches)

        # 2. Extract capitalized noun phrases (potential Projects, Organizations, Persons)
        capitalized = re.findall(
            r'\b[A-Z][a-zA-Z0-9_-]{2,}(?:[ \t]+[A-Z][a-zA-Z0-9_-]{2,}){0,3}\b',
            text,
        )
        for cap in capitalized:
            if cap.lower() in [t.lower() for t in KNOWN_TECHNOLOGIES]:
                continue
            if len(cap) < 3 or cap.casefold() in CAPITALIZED_TERM_STOPLIST:
                continue
            
            # Heuristic typing
            etype = "CONCEPT"
            if any(term in cap for term in ["Inc", "Corp", "LLC", "Labs", "Foundation", "Team", "Organization"]):
                etype = "ORGANIZATION"
            elif any(term in cap for term in ["Project", "System", "Engine", "Service", "Protocol", "Framework", "OS"]):
                etype = "PROJECT"
                
            normalized_cap = cap.casefold()
            if normalized_cap in entities:
                entities[normalized_cap].mention_count += 1
            else:
                entities[normalized_cap] = ExtractedEntity(cap, etype, 1)

        return sorted(
            entities.values(),
            key=lambda entity: (-entity.mention_count, entity.name.casefold()),
        )[:MAX_EXTRACTED_ENTITIES]
