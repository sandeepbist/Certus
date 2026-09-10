import re
import os
import logging
import threading
from typing import Any, Dict, List, Literal

from neo4j import GraphDatabase, Query
from services.shared.worker_runtime import bounded_int_env

logger = logging.getLogger("entity_extractor")

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

    @staticmethod
    def sync_to_neo4j(
        doc_id: str,
        doc_title: str,
        user_id: str,
        tenant_id: str,
        entities: List[ExtractedEntity],
    ):
        try:
            with entity_graph_driver().session() as session:
                session.run(
                    Query(
                        """
                    MERGE (tenant:Tenant {id: $tenant_id})
                    MERGE (u:User {id: $user_id})
                    MERGE (d:Document {id: $doc_id})
                    ON CREATE SET d.created_at = datetime()
                    SET d.title = $doc_title,
                        d.tenant_id = $tenant_id,
                        d.deleted_at = NULL,
                        d.status = 'active'
                    MERGE (u)-[:MEMBER_OF]->(tenant)
                    MERGE (tenant)-[:CONTAINS]->(d)
                    MERGE (u)-[:OWNS]->(d)
                        """,
                        timeout=NEO4J_QUERY_TIMEOUT_SECONDS,
                    ),
                    tenant_id=tenant_id,
                    user_id=user_id,
                    doc_id=doc_id,
                    doc_title=doc_title,
                ).consume()

                session.run(
                    Query(
                        """
                    MATCH (d:Document {id: $doc_id, tenant_id: $tenant_id})
                    MATCH (d)-[relationship:MENTIONS]->()
                    DELETE relationship
                        """,
                        timeout=NEO4J_QUERY_TIMEOUT_SECONDS,
                    ),
                    doc_id=doc_id,
                    tenant_id=tenant_id,
                ).consume()

                entity_rows = [
                    {
                        "normalized_name": entity.name.casefold(),
                        "name": entity.name,
                        "type": entity.type,
                        "mention_count": entity.mention_count,
                    }
                    for entity in entities
                ]
                if entity_rows:
                    session.run(
                        Query(
                            """
                        UNWIND $entities AS entity
                        MERGE (e:Entity {tenant_id: $tenant_id, normalized_name: entity.normalized_name})
                        ON CREATE SET e.name = entity.name, e.type = entity.type, e.mention_count = 0
                        SET e.stale = false
                        WITH e, entity
                        MATCH (d:Document {id: $doc_id, tenant_id: $tenant_id})
                        MERGE (d)-[mention:MENTIONS]->(e)
                        SET mention.frequency = entity.mention_count
                            """,
                            timeout=NEO4J_QUERY_TIMEOUT_SECONDS,
                        ),
                        tenant_id=tenant_id,
                        entities=entity_rows,
                        doc_id=doc_id,
                    ).consume()

                # Recompute aggregate counts after replacements. Entities with
                # no live document or task connections remain as explicitly
                # stale nodes so soft-deleted knowledge can be audited.
                session.run(
                    Query(
                        """
                    MATCH (entity:Entity {tenant_id: $tenant_id})
                    OPTIONAL MATCH (document:Document {tenant_id: $tenant_id})-[mention:MENTIONS]->(entity)
                    WHERE document.deleted_at IS NULL
                    WITH entity, coalesce(sum(mention.frequency), 0) AS total_mentions
                    SET entity.mention_count = total_mentions,
                        entity.stale = total_mentions = 0 AND NOT EXISTS {
                            MATCH (:Task {tenant_id: $tenant_id})-[:RELATES_TO]->(entity)
                        }
                        """,
                        timeout=NEO4J_QUERY_TIMEOUT_SECONDS,
                    ),
                    tenant_id=tenant_id,
                ).consume()
            logger.info(f"Successfully synced {len(entities)} entities to Neo4j for doc {doc_id}")
        except Exception as e:
            logger.warning(f"Neo4j sync skipped or failed (graceful degradation): {e}")

    @staticmethod
    def list_document_entities(
        doc_id: str,
        user_id: str,
        tenant_id: str,
    ) -> tuple[List[Dict[str, Any]], Literal["ready", "degraded"]]:
        try:
            with entity_graph_driver().session() as session:
                records = session.run(
                    Query(
                        """
                        MATCH (user:User {id: $user_id})-[:OWNS]->
                              (document:Document {id: $doc_id, tenant_id: $tenant_id})
                              -[mention:MENTIONS]->(entity:Entity {tenant_id: $tenant_id})
                        WHERE document.deleted_at IS NULL
                        RETURN entity.name AS name,
                               entity.type AS type,
                               mention.frequency AS mention_count
                        ORDER BY mention_count DESC, toLower(name) ASC
                        LIMIT 250
                        """,
                        timeout=NEO4J_QUERY_TIMEOUT_SECONDS,
                    ),
                    doc_id=doc_id,
                    user_id=user_id,
                    tenant_id=tenant_id,
                )
                return [dict(record) for record in records], "ready"
        except Exception as error:
            logger.warning("Could not load document entities for %s: %s", doc_id, error)
            return [], "degraded"

    @staticmethod
    def mark_document_deleted(doc_id: str, user_id: str, tenant_id: str, deleted_at: str) -> str:
        try:
            with entity_graph_driver().session() as session:
                session.run(
                    Query(
                        """
                        MATCH (user:User {id: $user_id})-[:OWNS]->
                              (document:Document {id: $doc_id, tenant_id: $tenant_id})
                        SET document.deleted_at = datetime($deleted_at),
                            document.status = 'deleted'
                        """,
                        timeout=NEO4J_QUERY_TIMEOUT_SECONDS,
                    ),
                    doc_id=doc_id,
                    user_id=user_id,
                    tenant_id=tenant_id,
                    deleted_at=deleted_at,
                ).consume()
                session.run(
                    Query(
                        """
                        MATCH (entity:Entity {tenant_id: $tenant_id})
                        OPTIONAL MATCH (active:Document {tenant_id: $tenant_id})-[mention:MENTIONS]->(entity)
                        WHERE active.deleted_at IS NULL
                        WITH entity, coalesce(sum(mention.frequency), 0) AS live_mentions
                        SET entity.mention_count = live_mentions,
                            entity.stale = live_mentions = 0 AND NOT EXISTS {
                                MATCH (:Task {tenant_id: $tenant_id})-[:RELATES_TO]->(entity)
                            }
                        """,
                        timeout=NEO4J_QUERY_TIMEOUT_SECONDS,
                    ),
                    tenant_id=tenant_id,
                ).consume()
            return "synced"
        except Exception as error:
            logger.warning("Document deletion graph synchronization degraded for %s: %s", doc_id, error)
            return "degraded"
