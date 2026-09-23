import sys
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import UUID

# Keep this test runnable without repository-wide PYTHONPATH configuration.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "services" / "ingestion"))

import psycopg2
from fastapi import HTTPException

from services.ingestion.app import main as ingestion_main
from services.ingestion.app.extractors import entity_extractor


DOCUMENT_ID = "00000000-0000-4000-8000-000000000001"
VERSION_ID = "00000000-0000-4000-8000-000000000002"


class RestoreStore:
    def __init__(self, *, owner=True, quota_full=False, version_status="ready", derivations=None):
        self.deleted = True
        self.owner = owner
        self.quota_full = quota_full
        self.version_status = version_status
        self.derivations = derivations or []
        self.commits = 0
        self.rollbacks = 0
        self.statements = []
        self.inserted_job_rows = []

    def connection(self):
        return RestoreConnection(self)


class RestoreConnection:
    def __init__(self, store):
        self.store = store

    def __enter__(self):
        return self

    def __exit__(self, exc_type, _exc, _traceback):
        if exc_type:
            self.store.rollbacks += 1
        else:
            self.store.commits += 1

    def cursor(self, **_kwargs):
        return RestoreCursor(self.store)


class RestoreCursor:
    def __init__(self, store):
        self.store = store
        self.rowcount = 0
        self._one = None
        self._all = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def execute(self, query, params=None):
        statement = " ".join(query.split())
        self.store.statements.append((statement, params))
        self._one = None
        self._all = []
        if statement.startswith("SELECT id FROM document_embedding_jobs"):
            return
        if "FROM documents AS document" in statement and "FOR UPDATE OF document" in statement:
            if self.store.deleted and self.store.owner:
                self._one = {
                    "id": UUID(DOCUMENT_ID),
                    "entity_count": 0,
                    "status": self.store.version_status,
                    "error_message": None,
                }
            return
        if "document_derivations AS derivation" in statement:
            self._all = self.store.derivations
            return
        if statement.startswith("UPDATE documents AS document"):
            if self.store.quota_full:
                raise psycopg2.errors.RaiseException(
                    "workspace document quota prevents restoration"
                )
            if self.store.deleted and self.store.owner:
                self.store.deleted = False
                self.rowcount = 1
                self._one = {
                    "id": UUID(DOCUMENT_ID),
                    "entity_count": 0,
                    "status": self.store.version_status,
                }

    def fetchone(self):
        return self._one

    def fetchall(self):
        return self._all


class DocumentRestoreTests(unittest.TestCase):
    def call_restore(self, store, *, tenant_id="tenant-a", user_id="user-a"):
        def capture_job_rows(_cursor, _query, rows, **_kwargs):
            store.inserted_job_rows.extend(rows)

        with patch.object(ingestion_main, "get_db", side_effect=store.connection), patch.object(
            ingestion_main.EntityExtractor,
            "restore_document",
            return_value="synced",
        ) as sync, patch.object(
            ingestion_main,
            "execute_values",
            side_effect=capture_job_rows,
        ) as insert_jobs:
            result = ingestion_main.restore_document(
                DOCUMENT_ID,
                tenant_id=tenant_id,
                user_id=user_id,
            )
        return result, sync, insert_jobs

    def test_restores_owned_document_and_reactivates_graph(self):
        store = RestoreStore()

        result, sync, insert_jobs = self.call_restore(store)

        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["graph_sync"], "synced")
        self.assertFalse(store.deleted)
        self.assertEqual(store.commits, 1)
        self.assertEqual(store.rollbacks, 0)
        sync.assert_called_once_with(
            DOCUMENT_ID,
            "user-a",
            "tenant-a",
            0,
        )
        insert_jobs.assert_not_called()
        document_read = store.statements[1]
        self.assertIn("document.tenant_id = %s AND document.user_id = %s", document_read[0])
        self.assertEqual(document_read[1], (DOCUMENT_ID, "tenant-a", "user-a"))

    def test_unowned_document_is_not_disclosed(self):
        store = RestoreStore(owner=False)

        with self.assertRaises(HTTPException) as raised:
            self.call_restore(store, tenant_id="tenant-other", user_id="user-other")

        self.assertEqual(raised.exception.status_code, 404)
        self.assertTrue(store.deleted)
        self.assertEqual(store.commits, 0)
        self.assertEqual(store.rollbacks, 1)
        self.assertEqual(
            store.statements[1][1],
            (DOCUMENT_ID, "tenant-other", "user-other"),
        )

    def test_restore_requeues_only_unfinished_current_derivation_batches(self):
        store = RestoreStore(
            version_status="processing",
            derivations=[
                {
                    "document_version_id": UUID(VERSION_ID),
                    "derivation_id": UUID("00000000-0000-4000-8000-000000000003"),
                    "processing_generation": UUID("00000000-0000-4000-8000-000000000004"),
                    "processing_total_chunks": 25,
                    "embedding_profile": "embedding-space:v1:local:test:1536",
                }
            ],
        )

        result, _, insert_jobs = self.call_restore(store)

        self.assertEqual(result["status"], "processing")
        self.assertEqual(
            [(row[6], row[7]) for row in store.inserted_job_rows],
            [(0, 20), (20, 25)],
        )
        self.assertEqual(insert_jobs.call_count, 1)
        derivation_scope = store.statements[3][0]
        self.assertIn("version.current_derivation_id = derivation.id", derivation_scope)
        self.assertIn(
            "version.processing_generation = derivation.processing_generation",
            derivation_scope,
        )
        self.assertIn("derivation.status = 'processing'", derivation_scope)
        self.assertIn(
            "document_embedding_jobs.status = 'obsolete'",
            insert_jobs.call_args.args[1],
        )

    def test_quota_trigger_rejection_returns_conflict_and_rolls_back(self):
        store = RestoreStore(quota_full=True)

        with self.assertRaises(HTTPException) as raised:
            self.call_restore(store)

        self.assertEqual(raised.exception.status_code, 409)
        self.assertTrue(store.deleted)
        self.assertEqual(store.commits, 0)
        self.assertEqual(store.rollbacks, 1)


class ArchivedDocumentListTests(unittest.TestCase):
    def test_deleted_status_lists_only_owned_documents(self):
        class Connection:
            def __init__(self):
                self.statement = ""
                self.params = ()
                self.rows = []

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def cursor(self, **_kwargs):
                return self

            def execute(self, query, params=None):
                self.statement = " ".join(query.split())
                self.params = params or ()
                self.rows = []
                if tuple(self.params[:2]) == ("tenant-a", "user-a"):
                    self.rows = [{
                        "id": UUID(DOCUMENT_ID),
                        "title": "Archived proof",
                        "status": "deleted",
                    }]

            def fetchall(self):
                return self.rows

            def fetchone(self):
                return None

        connection = Connection()
        with patch.object(ingestion_main, "get_db", return_value=connection):
            owned = ingestion_main.list_documents(
                tenant_id="tenant-a",
                user_id="user-a",
                limit=50,
                page_cursor=None,
                search="",
                status="deleted",
                tag="",
                source_type="",
            )
            unowned = ingestion_main.list_documents(
                tenant_id="tenant-other",
                user_id="user-other",
                limit=50,
                page_cursor=None,
                search="",
                status="deleted",
                tag="",
                source_type="",
            )

        self.assertEqual([item["title"] for item in owned["documents"]], ["Archived proof"])
        self.assertEqual(unowned["documents"], [])
        self.assertIn("document.tenant_id = %s", connection.statement)
        self.assertIn("document.user_id = %s", connection.statement)
        self.assertIn("document.deleted_at IS NOT NULL", connection.statement)
        self.assertEqual(tuple(connection.params[:2]), ("tenant-other", "user-other"))


class GraphRestoreTests(unittest.TestCase):
    def run_restore(self, record):
        class Result:
            def single(self):
                return record

            def consume(self):
                return None

        class Session:
            def __init__(self):
                self.queries = []

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def run(self, query, **_params):
                self.queries.append(query.text)
                return Result()

        class Driver:
            def __init__(self):
                self.opened_session = Session()

            def session(self):
                return self.opened_session

        driver = Driver()
        with patch.object(entity_extractor, "entity_graph_driver", return_value=driver):
            result = entity_extractor.EntityExtractor.restore_document(
                DOCUMENT_ID,
                "user-a",
                "tenant-a",
                expected_entity_count=1,
            )
        return result, driver.opened_session.queries

    def test_graph_restore_refuses_cross_tenant_owner_and_reports_degraded(self):
        result, queries = self.run_restore(None)

        self.assertEqual(result, "degraded")
        self.assertIn("existing.tenant_id = $tenant_id", queries[0])
        self.assertIn("other.id <> $user_id", queries[0])
        self.assertNotIn("SET document.tenant_id = $tenant_id", queries[0])

    def test_graph_restore_reports_missing_mentions_as_degraded(self):
        result, queries = self.run_restore({"existed": True, "has_mentions": False})

        self.assertEqual(result, "degraded")
        self.assertIn("-[:MENTIONS]->", queries[0])


if __name__ == "__main__":
    unittest.main()
