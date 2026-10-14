"""Durable current-document projections and PostgreSQL-authorized graph reads."""

from dataclasses import asdict, dataclass
from hashlib import sha256
import json
import logging
from random import uniform
from typing import Callable, Literal
from uuid import UUID, uuid4

from neo4j import Query
from psycopg2.extras import RealDictCursor

from services.shared.safe_errors import safe_error_summary

logger = logging.getLogger("document_graph")
PROJECTION_CONSTRAINT = "certus_document_projection_id_unique"
MAX_SNAPSHOT_BYTES = 262_144


class SnapshotUnavailable(ValueError):
    """The retained version has no valid, bounded entity snapshot."""


class ProjectionConflict(ValueError):
    """Stored projection ownership or revision disagrees with SQL authority."""


class ProjectionInvalid(ProjectionConflict):
    """Stored ownership or digest requires operator repair."""


@dataclass(frozen=True)
class GraphEntity:
    name: str
    type: str
    mention_count: int


@dataclass(frozen=True)
class EntitySnapshot:
    entities: tuple[GraphEntity, ...]


@dataclass(frozen=True)
class UnavailableSnapshot:
    reason: str


@dataclass(frozen=True)
class GraphReadResult:
    rows: list[dict]
    status: Literal["ready", "degraded"]
    truncated: bool = False


@dataclass(frozen=True)
class ProjectionTarget:
    document_id: str
    version_id: str
    tenant_id: str
    user_id: str
    revision: int
    title: str
    created_at: str
    deleted_at: str | None
    snapshot: EntitySnapshot | UnavailableSnapshot

    def payload(self):
        return {
            "document_id": self.document_id, "version_id": self.version_id,
            "tenant_id": self.tenant_id, "user_id": self.user_id,
            "revision": self.revision, "title": self.title,
            "created_at": self.created_at, "deleted_at": self.deleted_at,
            "entities": [asdict(item) for item in self.snapshot.entities] if isinstance(self.snapshot, EntitySnapshot) else None,
        }


def _target_from_row(row) -> ProjectionTarget:
    payload = row["source_payload"]
    snapshot = UnavailableSnapshot("retained entity snapshot unavailable or invalid")
    if isinstance(payload, dict):
        entities = payload.get("entities")
        count = payload.get("entity_count")
        valid = (
            payload.get("document_id") == str(row["document_id"])
            and payload.get("document_version_id") == str(row["current_version_id"])
            and type(entities) is list and len(entities) <= 250
            and type(count) is int and count == len(entities) == row["entity_count"]
        )
        normalized = set()
        if valid:
            for entity in entities:
                if not (
                    isinstance(entity, dict)
                    and isinstance(entity.get("name"), str) and 0 < len(entity["name"]) <= 512
                    and isinstance(entity.get("type"), str) and 0 < len(entity["type"]) <= 64
                    and type(entity.get("mention_count")) is int
                    and 0 < entity["mention_count"] <= 2_147_483_647
                    and entity["name"].casefold() not in normalized
                ):
                    valid = False
                    break
                normalized.add(entity["name"].casefold())
        if valid:
            snapshot = EntitySnapshot(tuple(GraphEntity(
                entity["name"], entity["type"], entity["mention_count"],
            ) for entity in entities))
    return ProjectionTarget(
        str(row["document_id"]), str(row["current_version_id"]),
        row["tenant_id"], row["user_id"], int(row["revision"]),
        row["title"] or "Untitled document", row["created_at"].isoformat(),
        row["deleted_at"].isoformat() if row["deleted_at"] else None, snapshot,
    )


def _token(row):
    try:
        revision = row["graph_revision"]
        if type(revision) is not int or not 1 <= revision <= 9_223_372_036_854_775_807:
            return None
        return str(UUID(str(row["document_id"]))), str(UUID(str(row["document_version_id"]))), revision
    except (KeyError, ValueError, TypeError):
        return None


class DocumentGraph:
    def __init__(self, database: Callable, driver: Callable, *, timeout_seconds: int | float):
        self._database = database
        self._driver = driver
        self._timeout = timeout_seconds

    def _claim(self):
        owner = str(uuid4())
        with self._database() as connection:
            with connection.cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute(
                    """
                    SELECT state.* FROM document_graph_state AS state
                    WHERE state.acknowledged_revision < state.revision
                      AND state.blocked_reason IS NULL AND state.available_at <= NOW()
                      AND (state.lease_until IS NULL OR state.lease_until <= NOW())
                    ORDER BY state.available_at, state.document_id
                    LIMIT 1 FOR UPDATE OF state SKIP LOCKED
                    """,
                )
                row = cursor.fetchone()
                if row is None:
                    return None
                # A joined locking query can refresh state through EvalPlanQual
                # while retaining old document tuples. Read source in a fresh
                # statement while the claimed state remains locked.
                cursor.execute(
                    """
                    SELECT document.current_version_id, document.title,
                           document.entity_count, document.created_at, document.deleted_at,
                           CASE WHEN octet_length(intent.response_payload::text) <= %s
                                THEN intent.response_payload ELSE NULL END AS source_payload
                    FROM documents AS document
                    LEFT JOIN document_upload_intents AS intent
                      ON intent.document_version_id = document.current_version_id
                     AND intent.document_id = document.id
                     AND intent.tenant_id = document.tenant_id AND intent.user_id = document.user_id
                     AND intent.status = 'completed'
                    WHERE document.id = %s AND document.tenant_id = %s AND document.user_id = %s
                    """,
                    (MAX_SNAPSHOT_BYTES, str(row["document_id"]), row["tenant_id"], row["user_id"]),
                )
                row.update(cursor.fetchone())
                cursor.execute(
                    """
                    UPDATE document_graph_state
                    SET lease_owner = %s, lease_until = NOW() + (%s * INTERVAL '1 second'),
                        attempts = LEAST(attempts + 1, 1000)
                    WHERE document_id = %s AND revision = %s
                    """,
                    (owner, max(300, int(2 * self._timeout) + 180), str(row["document_id"]), row["revision"]),
                )
        return row, owner

    def _publish(self, target: ProjectionTarget):
        if target.deleted_at is None and isinstance(target.snapshot, UnavailableSnapshot):
            raise SnapshotUnavailable(target.snapshot.reason)
        parameters = target.payload()
        digest = sha256(json.dumps(parameters, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        with self._driver().session() as session:
            constraint = session.run(Query(
                """SHOW CONSTRAINTS YIELD name, type, labelsOrTypes, properties
                WHERE name = $name AND type = 'UNIQUENESS'
                  AND labelsOrTypes = ['DocumentProjection'] AND properties = ['id']
                RETURN name""", timeout=self._timeout), name=PROJECTION_CONSTRAINT).single()
            if not constraint:
                raise RuntimeError("document projection uniqueness constraint is missing")
            with session.begin_transaction(timeout=self._timeout) as transaction:
                existing = transaction.run(
                    """
                    MERGE (document:DocumentProjection {id: $document_id})
                    ON CREATE SET document.tenant_id = $tenant_id, document.user_id = $user_id
                    SET document._projection_lock = true
                    REMOVE document._projection_lock
                    RETURN document.tenant_id AS tenant_id, document.user_id AS user_id,
                           coalesce(document.revision, 0) AS revision,
                           document.digest AS digest,
                           EXISTS {
                               MATCH (other:User)-[:OWNS]->(document)
                               WHERE other.id <> $user_id
                           } AS other_owner
                    """, **parameters,
                ).single()
                if (existing["tenant_id"] != target.tenant_id or existing["user_id"] != target.user_id
                        or existing["other_owner"]):
                    raise ProjectionInvalid("projection owner mismatch")
                if existing["revision"] > target.revision:
                    raise ProjectionConflict("projection revision superseded")
                if existing["revision"] == target.revision:
                    if existing["digest"] != digest:
                        raise ProjectionInvalid("projection digest mismatch")
                    transaction.rollback()
                    return
                transaction.run(
                    """
                    MATCH (document:DocumentProjection {id: $document_id})
                    OPTIONAL MATCH (document)-[mention:MENTIONS]->()
                    DELETE mention
                    """, document_id=target.document_id,
                ).consume()
                if target.deleted_at is None:
                    transaction.run(
                        """
                        UNWIND $entities AS item
                        MERGE (entity:Entity {tenant_id: $tenant_id, normalized_name: item.normalized_name})
                        ON CREATE SET entity.name = item.name, entity.type = item.type
                        WITH entity, item
                        MATCH (document:DocumentProjection {id: $document_id})
                        CREATE (document)-[mention:MENTIONS]->(entity)
                        SET mention.name = item.name, mention.type = item.type,
                            mention.frequency = item.mention_count
                        """, entities=[{**asdict(item), "normalized_name": item.name.casefold()}
                                        for item in target.snapshot.entities],
                        tenant_id=target.tenant_id, document_id=target.document_id,
                    ).consume()
                transaction.run(
                    """
                    MATCH (document:DocumentProjection {id: $document_id})
                    MERGE (tenant:Tenant {id: $tenant_id})
                    MERGE (user:User {id: $user_id})
                    MERGE (tenant)-[:CONTAINS]->(document)
                    MERGE (user)-[:OWNS]->(document)
                    MERGE (user)-[:MEMBER_OF]->(tenant)
                    SET document.version_id = $version_id, document.revision = $revision,
                        document.digest = $digest, document.title = $title,
                        document.created_at = datetime($created_at),
                        document.deleted_at = CASE WHEN $deleted_at IS NULL THEN NULL ELSE datetime($deleted_at) END
                    """, **parameters, digest=digest,
                ).consume()
                transaction.commit()

    def reconcile_once(self) -> bool:
        claim = self._claim()
        if claim is None:
            return False
        row, owner = claim
        error_summary = None
        blocked_reason = None
        try:
            self._publish(_target_from_row(row))
        except Exception as error:
            error_summary = safe_error_summary(error, operation="document graph reconciliation")
            if isinstance(error, SnapshotUnavailable):
                blocked_reason = "retained entity snapshot unavailable or invalid"
            elif isinstance(error, ProjectionInvalid):
                blocked_reason = "projection ownership or digest requires repair"
            logger.warning("%s", error_summary)
        with self._database() as connection:
            with connection.cursor() as cursor:
                retry_seconds = min(300, 2 ** min(int(row["attempts"]) + 1, 9))
                cursor.execute(
                    """
                    UPDATE document_graph_state
                    SET acknowledged_revision = CASE WHEN %s IS NULL THEN revision ELSE acknowledged_revision END,
                        lease_owner = NULL, lease_until = NULL, blocked_reason = %s, last_error = %s,
                        available_at = NOW() + (%s * INTERVAL '1 second')
                    WHERE document_id = %s AND revision = %s AND lease_owner = %s
                    """,
                    (error_summary, blocked_reason, error_summary,
                     uniform(retry_seconds / 2, retry_seconds),
                     str(row["document_id"]), row["revision"], owner),
                )
        return True

    def _authorized(self, rows, tenant_id, user_id):
        tokens = sorted({token for row in rows if (token := _token(row)) is not None})
        if len(tokens) > 2001:
            raise ValueError("graph authorization batch exceeds 2001 documents")
        with self._database() as connection:
            with connection.cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute("SET LOCAL statement_timeout = '2000ms'")
                cursor.execute(
                    """
                    WITH authorized AS (
                    SELECT candidate.document_id, candidate.version_id, candidate.revision,
                           document.title, document.created_at
                    FROM unnest(%s::uuid[], %s::uuid[], %s::bigint[])
                         AS candidate(document_id, version_id, revision)
                    JOIN documents AS document
                      ON document.id = candidate.document_id
                     AND document.current_version_id = candidate.version_id
                    JOIN document_graph_state AS state ON state.document_id = document.id
                    WHERE document.tenant_id = %s AND document.user_id = %s
                      AND state.tenant_id = document.tenant_id AND state.user_id = document.user_id
                      AND document.deleted_at IS NULL
                      AND state.revision = candidate.revision
                      AND state.acknowledged_revision = candidate.revision
                      AND state.blocked_reason IS NULL
                    )
                    SELECT authorized.*, EXISTS (
                        SELECT 1 FROM document_graph_state AS pending
                        JOIN documents AS document ON document.id = pending.document_id
                        WHERE pending.tenant_id = %s AND pending.user_id = %s
                          AND document.deleted_at IS NULL
                          AND pending.acknowledged_revision < pending.revision
                    ) AS pending
                    FROM (SELECT 1) AS singleton LEFT JOIN authorized ON TRUE
                    """,
                    (list(item[0] for item in tokens), list(item[1] for item in tokens),
                     list(item[2] for item in tokens), tenant_id, user_id, tenant_id, user_id),
                )
                rows = cursor.fetchall()
                status = "degraded" if rows[0]["pending"] else "ready"
                return ({(str(item["document_id"]), str(item["version_id"]), int(item["revision"])): item
                         for item in rows if item["document_id"] is not None}, status)

    def entities(self, document_id, tenant_id, user_id):
        try:
            with self._driver().session() as session:
                rows = session.run(Query(
                    """
                    MATCH (document:DocumentProjection {id: $document_id, tenant_id: $tenant_id, user_id: $user_id})
                    WHERE document.deleted_at IS NULL
                    OPTIONAL MATCH (document)-[mention:MENTIONS]->(:Entity {tenant_id: $tenant_id})
                    RETURN document.id AS document_id, document.version_id AS document_version_id,
                           document.revision AS graph_revision, mention.name AS name,
                           mention.type AS type, mention.frequency AS mention_count
                    ORDER BY mention_count DESC, name ASC LIMIT 251
                    """, timeout=self._timeout), document_id=str(document_id), tenant_id=tenant_id, user_id=user_id).data()
            valid, _ = self._authorized(rows, tenant_id, user_id)
            if not rows or len(rows) > 250 or any(_token(row) not in valid for row in rows):
                return [], "degraded"
            return [{key: row[key] for key in ("name", "type", "mention_count")}
                    for row in rows if row["name"] is not None], "ready"
        except Exception as error:
            logger.warning("%s", safe_error_summary(error, operation="document entity lookup"))
            return [], "degraded"

    def workspace(self, tenant_id, user_id, entity_type=None):
        with self._driver().session() as session:
            rows = session.run(Query(
                """
                MATCH (document:DocumentProjection {tenant_id: $tenant_id, user_id: $user_id})
                      -[mention:MENTIONS]->(entity:Entity {tenant_id: $tenant_id})
                WHERE document.deleted_at IS NULL AND ($entity_type IS NULL OR mention.type = $entity_type)
                RETURN document.id AS document_id, document.version_id AS document_version_id,
                       document.revision AS graph_revision, entity.normalized_name AS normalized_name,
                       mention.name AS entity_name, mention.type AS entity_type, mention.frequency AS frequency
                ORDER BY frequency DESC, document.id, normalized_name LIMIT 2001
                """, timeout=self._timeout), tenant_id=tenant_id, user_id=user_id, entity_type=entity_type).data()
        valid, status = self._authorized(rows, tenant_id, user_id)
        accepted = []
        for row in rows:
            if authority := valid.get(_token(row)):
                accepted.append({**row, "document_title": authority["title"] or "Untitled document",
                                 "document_created_at": authority["created_at"].isoformat()})
        return GraphReadResult(accepted, status, len(rows) > 2000)

    def _neighbors(self, session, roots, tenant_id, user_id):
        return session.run(Query(
            """
            MATCH (document:DocumentProjection {tenant_id: $tenant_id, user_id: $user_id})
                  -[root_mention:MENTIONS]->(root:Entity {tenant_id: $tenant_id})
            WHERE document.deleted_at IS NULL AND root.normalized_name IN $roots
            MATCH (document)-[target_mention:MENTIONS]->(target:Entity {tenant_id: $tenant_id})
            WHERE root <> target
            RETURN document.id AS document_id, document.version_id AS document_version_id,
                   document.revision AS graph_revision, root.normalized_name AS root_key,
                   target.normalized_name AS target_key, root_mention.name AS root,
                   target_mention.name AS target, target_mention.type AS target_type
            ORDER BY target_mention.frequency DESC, document.id, root_key, target_key LIMIT 40
            """, timeout=self._timeout), roots=roots, tenant_id=tenant_id, user_id=user_id).data()

    def traverse(self, roots, tenant_id, user_id, depth=2):
        names = sorted({name.strip().casefold() for name in roots if name.strip()})[:30]
        if not names:
            return GraphReadResult([], "ready")
        with self._driver().session() as session:
            direct = self._neighbors(session, names, tenant_id, user_id)
            first_valid, _ = self._authorized(direct, tenant_id, user_id)
            parents = {}
            for row in direct:
                if _token(row) in first_valid:
                    parents.setdefault(row["target_key"], set()).add(_token(row))
            second = self._neighbors(session, sorted(parents), tenant_id, user_id) if depth > 1 and parents else []
        # Recheck both hop sources after all graph I/O, before formatting evidence.
        final_valid, status = self._authorized(direct + second, tenant_id, user_id)
        accepted = [row for row in direct if _token(row) in final_valid]
        accepted.extend(row for row in second if _token(row) in final_valid
                        and any(parent in final_valid for parent in parents.get(row["root_key"], ()))
                        and row["target_key"] not in names)
        seen = set()
        result = []
        for row in accepted:
            edge = tuple(sorted((row["root_key"], row["target_key"])))
            if edge not in seen:
                seen.add(edge)
                result.append({key: row[key] for key in ("root", "target", "target_type")})
        return GraphReadResult(result, status)

    def health(self):
        with self._database() as connection:
            with connection.cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute("SET LOCAL statement_timeout = '2000ms'")
                cursor.execute("""SELECT blocked_reason,
                    GREATEST(0, EXTRACT(EPOCH FROM NOW() - queued_at))::float AS age_seconds
                    FROM document_graph_state
                    WHERE acknowledged_revision < revision ORDER BY queued_at LIMIT 5001""")
                rows = cursor.fetchall()
                return {"pending": min(len(rows), 5000), "counts_capped": len(rows) > 5000,
                        "blocked_in_sample": sum(row["blocked_reason"] is not None for row in rows),
                        "oldest_pending_age_seconds": rows[0]["age_seconds"] if rows else 0}
