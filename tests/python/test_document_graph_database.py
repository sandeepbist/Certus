"""Behavior checks on migrated disposable PostgreSQL and Neo4j, including CI.

Byte storage is outside this test's scope. Upload reservations and immutable
completion responses use the real SQL authority; graph operations use Neo4j.
"""
from contextlib import closing, contextmanager
from dataclasses import replace
import os
import sys
import io
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from pathlib import Path
import unittest
from unittest.mock import patch
from uuid import uuid4

import psycopg2
from neo4j import GraphDatabase
from neo4j.exceptions import Neo4jError

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "services" / "ingestion"))

from services.ingestion.app import main as ingestion_main
from fastapi import UploadFile
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.datastructures import Headers
from services.shared.document_graph import DocumentGraph, ProjectionConflict, _target_from_row
from services.shared.object_storage import StoredObject
from services.orchestration.app.api import graph as graph_api
from services.orchestration.app.retrieval import graphrag
import hashlib


class FixtureByteStore:
    """In-memory byte adapter; parsing, quota, source catalog and graph SQL are real."""
    settings = SimpleNamespace(bucket="graph-fixture")

    def __init__(self):
        self.objects = {}

    def put_create_once(self, key, stream, digest, _mime_type, _metadata):
        data = stream.read()
        assert len(data) == digest.byte_length
        assert hashlib.sha256(data).hexdigest() == digest.sha256_hex
        self.objects[key] = data
        return StoredObject(self.settings.bucket,key,str(uuid4()),len(data),digest.sha256_hex,
                            digest.checksum_sha256_base64,None,None,None,None,None)


@unittest.skipUnless(os.getenv("CERTUS_TEST_DATABASE_URL") and os.getenv("CERTUS_TEST_NEO4J_URI"),
                     "set disposable PostgreSQL and Neo4j test URLs")
class DocumentGraphDatabaseTests(unittest.TestCase):
    def setUp(self):
        self.tenant = "graph-test-" + str(uuid4())
        self.user = "user-" + str(uuid4())
        self.documents = []
        self.storage = FixtureByteStore()
        self.driver = GraphDatabase.driver(os.environ["CERTUS_TEST_NEO4J_URI"],
                                          auth=("neo4j", os.environ["CERTUS_TEST_NEO4J_PASSWORD"]),
                                          notifications_min_severity="WARNING")
        self.addCleanup(self.driver.close)
        with self.driver.session() as session:
            for statement in (Path(__file__).resolve().parents[2] / "infra/neo4j/schema.cypher").read_text().split(";"):
                if "CREATE CONSTRAINT" in statement or "CREATE INDEX" in statement:
                    session.run(statement).consume()
        self.graph = DocumentGraph(self.database, lambda: self.driver, timeout_seconds=10)
        self.addCleanup(self.cleanup)

    @contextmanager
    def database(self):
        with closing(psycopg2.connect(os.environ["CERTUS_TEST_DATABASE_URL"],
                                     connect_timeout=3, options="-c statement_timeout=10000 -c lock_timeout=3000")) as connection, connection:
            yield connection

    def sql(self, query, parameters=(), *, one=False):
        with self.database() as connection:
            with connection.cursor() as cursor:
                cursor.execute(query, parameters)
                return cursor.fetchone() if one else None

    def create_document(self, names, *, existing=None, unavailable=False):
        text = ". ".join(names) + ".\n" + str(uuid4())
        if unavailable:
            text = "X" + "x" * 600 + ".\n" + str(uuid4())
        upload = UploadFile(file=io.BytesIO(text.encode()),filename="graph.txt",
                            headers=Headers({"content-type":"text/plain"}))
        with patch.object(ingestion_main,"get_db",self.database), patch.object(
            ingestion_main,"ready_original_storage",return_value=self.storage):
            response = ingestion_main.ingest_document(
                file=upload,tenant_id=self.tenant,user_id=self.user,idempotency_key=str(uuid4()),
                chunk_strategy="token",tags="",source_time=None,replace_document_id=existing,
            )
        document_id=response.document_id
        self.documents.append(document_id)
        return document_id

    def due(self, document_id):
        self.sql("""UPDATE document_graph_state SET available_at=NOW(),lease_until=NULL,lease_owner=NULL
                    WHERE document_id=%s""", (document_id,))

    def drain(self):
        for _ in range(30):
            if not self.graph.reconcile_once():
                return
        self.fail("graph queue did not drain")

    def names(self, document_id):
        entities, status = self.graph.entities(document_id,self.tenant,self.user)
        return {item["name"] for item in entities}, status

    def cleanup(self):
        self.storage.objects.clear()
        self.sql("UPDATE document_source_objects SET status='delete_pending' WHERE tenant_id=%s AND user_id=%s",(self.tenant,self.user))
        self.sql("UPDATE document_source_objects SET status='deleted',deleted_at=NOW() WHERE tenant_id=%s AND user_id=%s",(self.tenant,self.user))
        self.sql("DELETE FROM documents WHERE tenant_id=%s AND user_id=%s", (self.tenant,self.user))
        self.sql("DELETE FROM document_upload_intents WHERE tenant_id=%s AND user_id=%s", (self.tenant,self.user))
        with self.driver.session() as session:
            session.run("MATCH (n {tenant_id:$tenant}) DETACH DELETE n", tenant=self.tenant).consume()
            session.run("MATCH (n:Tenant {id:$tenant}) DETACH DELETE n", tenant=self.tenant).consume()
            session.run("MATCH (n:User {id:$user}) DETACH DELETE n", user=self.user).consume()

    def test_outage_recovers_replacement_delete_restore_and_stale_lease(self):
        document = self.create_document(["Alpha", "Beta"])
        with patch.object(self.graph, "_driver", side_effect=RuntimeError("private fixture connection")):
            with self.assertLogs("document_graph", level="WARNING") as captured:
                self.assertTrue(self.graph.reconcile_once())
        self.assertNotIn("private fixture connection", "\n".join(captured.output))
        self.assertEqual(self.names(document), (set(), "degraded"))
        self.due(document); self.drain()
        self.assertEqual(self.names(document), ({"Alpha", "Beta"}, "ready"))
        old_row, old_owner = self.graph._claim() or (None,None)
        self.assertIsNone(old_row)  # No completed work is leased again.
        self.create_document(["Gamma", "Delta"], existing=document)
        old_row, old_owner = self.graph._claim()
        old_target = _target_from_row(old_row)
        self.sql("UPDATE documents SET deleted_at=NOW(),status='deleted' WHERE id=%s", (document,))
        self.drain()
        with self.assertRaises(ProjectionConflict):
            self.graph._publish(old_target)
        self.assertEqual(self.names(document), (set(), "degraded"))
        self.sql("UPDATE documents SET deleted_at=NULL,status='processing' WHERE id=%s", (document,))
        self.drain()
        self.assertEqual(self.names(document), ({"Gamma", "Delta"}, "ready"))
        with self.assertRaises(ProjectionConflict):
            self.graph._publish(old_target)

    def test_lost_acknowledgement_retries_exact_projection_and_empty_snapshot(self):
        document = self.create_document([])
        original_database = self.graph._database
        calls = 0
        @contextmanager
        def lost_acknowledgement():
            nonlocal calls
            calls += 1
            if calls == 2:
                raise psycopg2.OperationalError("lost acknowledgement")
            with original_database() as connection:
                yield connection
        with patch.object(self.graph, "_database", lost_acknowledgement):
            with self.assertRaises(psycopg2.OperationalError):
                self.graph.reconcile_once()
        self.assertEqual(self.names(document), (set(), "degraded"))
        self.due(document); self.drain()
        self.assertEqual(self.names(document), (set(), "ready"))
        with self.assertRaises(psycopg2.errors.RaiseException):
            self.sql("DELETE FROM document_upload_intents WHERE document_id=%s", (document,))

    def test_missing_snapshot_blocks_without_publishing_or_guessing_empty(self):
        document = self.create_document(["Unknown"],unavailable=True)
        with patch.object(self.graph,"_driver",side_effect=AssertionError("unknown snapshot must not publish")):
            with self.assertLogs("document_graph",level="WARNING"):
                self.graph.reconcile_once()
        self.assertEqual(self.names(document), (set(),"degraded"))
        blocked, acknowledged = self.sql("SELECT blocked_reason,acknowledged_revision FROM document_graph_state WHERE document_id=%s", (document,),one=True)
        self.assertIsNotNone(blocked); self.assertEqual(acknowledged,0)
        self.assertFalse(self.graph.reconcile_once())
        self.sql("UPDATE documents SET deleted_at=NOW(),status='deleted' WHERE id=%s", (document,))
        self.drain()  # A tombstone does not need an entity snapshot.

    def test_final_sql_gate_checks_both_hops_and_never_trusts_shared_entity_metadata(self):
        first = self.create_document(["Alpha", "Bridge"])
        second = self.create_document(["Bridge", "Target"])
        self.drain()
        with self.driver.session() as session:
            session.run("MATCH(e:Entity {tenant_id:$tenant}) SET e.name='Wrong shared name',e.type='Wrong shared type'",tenant=self.tenant).consume()
        self.assertEqual(self.names(first), ({"Alpha","Bridge"},"ready"))
        self.assertTrue(any(row["target"] == "Target" for row in self.graph.traverse(["alpha"],self.tenant,self.user).rows))
        original_neighbors = self.graph._neighbors
        calls = 0
        def deleting_during_traversal(*args):
            nonlocal calls
            result = original_neighbors(*args); calls += 1
            if calls == 2:
                self.sql("UPDATE documents SET deleted_at=NOW(),status='deleted' WHERE id=%s",(first,))
            return result
        with patch.object(self.graph,"_neighbors",side_effect=deleting_during_traversal):
            self.assertEqual(self.graph.traverse(["alpha"],self.tenant,self.user).rows,[])
        rows = self.graph.workspace(self.tenant,self.user).rows
        self.assertEqual({row["document_id"] for row in rows},{second})
        self.assertEqual(self.graph.entities(second,"other-tenant",self.user),( [],"degraded"))
        self.assertEqual(self.graph.workspace(self.tenant,"other-user").rows,[])

    def test_namespace_fence_preserves_tasks_and_refuses_poisoned_owners(self):
        document = self.create_document(["Alpha","Beta"])
        self.drain()
        with self.driver.session() as session:
            session.run("""MERGE(d:Document {id:$id}) SET d.tenant_id=$tenant,d.deleted_at=NULL
                MERGE(e:Entity {tenant_id:$tenant,normalized_name:'alpha'})
                MERGE(t:Task {id:$task}) SET t.tenant_id=$tenant
                MERGE(t)-[:RELATES_TO]->(e)""",id=document,tenant=self.tenant,task=str(uuid4())).consume()
        self.sql("UPDATE documents SET deleted_at=NOW(),status='deleted' WHERE id=%s",(document,))
        self.drain()
        self.assertEqual(self.graph.workspace(self.tenant,self.user).rows,[])
        with self.driver.session() as session:
            self.assertEqual(session.run("MATCH(t:Task {tenant_id:$tenant})-[:RELATES_TO]->() RETURN count(t) AS count",tenant=self.tenant).single()["count"],1)
        self.sql("UPDATE documents SET deleted_at=NULL,status='processing' WHERE id=%s",(document,))
        row,_ = self.graph._claim()
        with self.assertRaises(ProjectionConflict):
            self.graph._publish(replace(_target_from_row(row),user_id="wrong-owner"))

    def test_claim_refreshes_source_after_concurrent_replacement(self):
        document = self.create_document(["Old", "Snapshot"])
        old_version = self.sql("SELECT current_version_id FROM documents WHERE id=%s",(document,),one=True)[0]
        self.create_document(["New", "Snapshot"], existing=document)
        new_version = self.sql("SELECT current_version_id FROM documents WHERE id=%s",(document,),one=True)[0]
        self.sql("UPDATE documents SET current_version_id=%s WHERE id=%s",(old_version,document))
        key = uuid4().int % 2_147_483_647
        original_database = self.graph._database

        class DelayedCursor:
            def __init__(self, cursor):
                self.cursor = cursor

            def __enter__(self):
                self.cursor.__enter__()
                return self

            def __exit__(self, *args):
                return self.cursor.__exit__(*args)

            def execute(self, query, parameters=None):
                if "FOR UPDATE OF state SKIP LOCKED" in query:
                    query = query.replace("FROM document_graph_state AS state",
                        f"FROM document_graph_state AS state CROSS JOIN (SELECT pg_advisory_xact_lock({key})) AS gate")
                    query = query.replace("WHERE state.acknowledged_revision",
                        f"WHERE state.document_id = '{document}'::uuid AND state.acknowledged_revision")
                return self.cursor.execute(query, parameters)

            def __getattr__(self, name):
                return getattr(self.cursor, name)

        class DelayedConnection:
            def __init__(self, connection):
                self.connection = connection

            def cursor(self, **kwargs):
                return DelayedCursor(self.connection.cursor(**kwargs))

        @contextmanager
        def delayed_database():
            with original_database() as connection:
                yield DelayedConnection(connection)

        with self.database() as blocker, ThreadPoolExecutor(max_workers=1) as executor:
            with blocker.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_xact_lock(%s)", (key,))
            with patch.object(self.graph, "_database", delayed_database):
                future = executor.submit(self.graph._claim)
                try:
                    deadline = time.monotonic() + 2
                    while time.monotonic() < deadline:
                        waiting = self.sql("""SELECT EXISTS(SELECT 1 FROM pg_stat_activity
                            WHERE wait_event = 'advisory' AND query LIKE %s)""", (f"%{key}%",), one=True)[0]
                        if waiting:
                            break
                        time.sleep(0.02)
                    self.assertTrue(waiting, "claim must acquire its statement snapshot before replacement")
                    # This lifecycle transaction starts before the claim's NOW(),
                    # then commits after the claim acquired its statement snapshot.
                    with blocker.cursor() as cursor:
                        cursor.execute("UPDATE documents SET current_version_id=%s WHERE id=%s",(new_version,document))
                finally:
                    blocker.commit()
                row, _ = future.result(timeout=5)
        current_version, revision = self.sql("""SELECT d.current_version_id,s.revision
            FROM documents d JOIN document_graph_state s ON s.document_id=d.id WHERE d.id=%s""",
            (document,), one=True)
        self.assertEqual(row["revision"], revision)
        self.assertEqual(str(row["current_version_id"]), str(current_version))
        self.assertEqual({entity.name for entity in _target_from_row(row).snapshot.entities}, {"New", "Snapshot"})

    def test_expired_worker_cannot_acknowledge_a_new_lease(self):
        document = self.create_document(["Alpha", "Beta"])
        original_publish = self.graph._publish
        replacement_owner = None
        def another_worker_claims(target):
            nonlocal replacement_owner
            self.due(document)
            _, replacement_owner = self.graph._claim()
            original_publish(target)
        with patch.object(self.graph, "_publish", side_effect=another_worker_claims):
            self.graph.reconcile_once()
        acknowledged, owner = self.sql("""SELECT acknowledged_revision,lease_owner
            FROM document_graph_state WHERE document_id=%s""", (document,), one=True)
        self.assertEqual(acknowledged, 0)
        self.assertEqual(str(owner), replacement_owner)
        self.assertEqual(self.names(document), (set(), "degraded"))
        self.due(document); self.drain()
        self.assertEqual(self.names(document), ({"Alpha", "Beta"}, "ready"))

    def test_overlapping_publishers_cannot_regress_revision(self):
        document = self.create_document(["Old", "Snapshot"])
        row, _ = self.graph._claim()
        old = _target_from_row(row)
        self.create_document(["New", "Snapshot"], existing=document)
        row, _ = self.graph._claim()
        new = _target_from_row(row)
        with self.driver.session() as session, ThreadPoolExecutor(max_workers=2) as executor:
            with session.begin_transaction(timeout=10) as blocker:
                blocker.run("""MERGE(d:DocumentProjection {id:$id})
                    SET d.tenant_id=$tenant,d.user_id=$user,d.lock=true REMOVE d.lock""",
                    id=document,tenant=self.tenant,user=self.user).consume()
                futures = [executor.submit(self.graph._publish, target) for target in (new, old)]
                # Both publications contend for the same database node lock.
                time.sleep(0.1)
                blocker.commit()
            for future in futures:
                try:
                    future.result(timeout=15)
                except ProjectionConflict:
                    pass  # Only a superseded worker may be refused.
        with self.driver.session() as session:
            record = session.run("""MATCH(d:DocumentProjection {id:$id})-[:MENTIONS]->(e)
                RETURN d.revision AS revision,collect(e.normalized_name) AS names""",id=document).single()
        self.assertEqual(record["revision"], new.revision)
        self.assertEqual(set(record["names"]), {"new", "snapshot"})
        self.due(document); self.drain()
        self.assertEqual(self.names(document), ({"New", "Snapshot"}, "ready"))

    def test_empty_read_checks_sql_and_pending_status_is_scoped(self):
        document = self.create_document(["Alpha", "Beta"])
        self.assertEqual(self.graph.workspace(self.tenant,self.user).status, "degraded")
        self.assertEqual(self.graph.traverse(["alpha"],self.tenant,self.user).status, "degraded")
        self.assertEqual(self.graph.workspace(self.tenant,"other-user").status, "ready")
        with patch.object(self.graph,"_database",side_effect=psycopg2.OperationalError("SQL unavailable")):
            with self.assertRaises(psycopg2.OperationalError):
                self.graph.workspace("empty-tenant","empty-user")
        self.drain()
        self.assertEqual(self.graph.workspace(self.tenant,self.user).status, "ready")
        with self.driver.session() as session:
            session.run("MATCH(d:DocumentProjection {id:$id}) SET d.user_id='poisoned-owner'",id=document).consume()
        self.sql("UPDATE documents SET title='Changed' WHERE id=%s",(document,))
        with self.assertLogs("document_graph",level="WARNING"):
            self.graph.reconcile_once()
        self.assertFalse(self.graph.reconcile_once())
        self.assertEqual(self.graph.health()["blocked_in_sample"], 1)

    def test_failed_neo4j_transaction_restores_previous_mentions_and_revision(self):
        document = self.create_document(["Old", "Snapshot"])
        self.drain()
        self.create_document(["New", "Snapshot"], existing=document)
        row, _ = self.graph._claim()
        target = _target_from_row(row)
        # datetime() fails in the final write, after deleting and recreating edges.
        with self.assertRaises(Neo4jError):
            self.graph._publish(replace(target, created_at="invalid timestamp"))
        with self.driver.session() as session:
            record = session.run("""MATCH(d:DocumentProjection {id:$id})-[m:MENTIONS]->()
                RETURN d.revision AS revision,collect(m.name) AS names""",id=document).single()
        self.assertLess(record["revision"], target.revision)
        self.assertEqual(set(record["names"]), {"Old", "Snapshot"})
        self.assertEqual(self.names(document), (set(), "degraded"))
        self.due(document); self.drain()
        self.assertEqual(self.names(document), ({"New", "Snapshot"}, "ready"))

    def test_graph_api_and_retrieval_publish_readiness_and_deny_other_owners(self):
        document = self.create_document(["Alpha", "Beta"])
        app = FastAPI()
        app.include_router(graph_api.router)
        headers = {"X-Certus-Tenant-Id":self.tenant,"X-Certus-User-Id":self.user}
        with patch.object(graph_api,"document_graph",self.graph), patch.object(graphrag,"document_graph",self.graph):
            with TestClient(app) as client:
                pending = client.get("/graph",headers=headers)
                self.assertEqual(pending.status_code, 200)
                self.assertEqual(pending.json()["projection_status"], "degraded")
                self.assertEqual(pending.json()["nodes"], [])
                self.drain()
                ready = client.get("/graph",headers=headers).json()
                self.assertEqual(ready["projection_status"], "ready")
                self.assertIn(document, {node["record_id"] for node in ready["nodes"]})
                retrieval = graphrag.GraphRAGEngine.traverse_graph(["alpha"],self.user,self.tenant)
                self.assertEqual(retrieval["projection_status"], "ready")
                self.assertEqual(retrieval["connected_entities"], ["Beta"])
                wrong_headers = {**headers,"X-Certus-User-Id":"other-owner"}
                self.assertEqual(client.get("/graph",headers=wrong_headers).json()["nodes"], [])
                with patch.object(self.graph,"_database",side_effect=psycopg2.OperationalError("SQL unavailable")):
                    self.assertEqual(client.get("/graph",headers=wrong_headers).status_code, 503)



if __name__ == "__main__":
    unittest.main()
