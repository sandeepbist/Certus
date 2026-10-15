"""Export admission and snapshot checks on migrated disposable PostgreSQL."""

from contextlib import closing, contextmanager
from io import BytesIO
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
from uuid import UUID, uuid4
from zipfile import ZipFile

import psycopg2
from psycopg2.extras import RealDictCursor
from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "services/orchestration"))
from app.api import export as export_api
from app.core.identity import RequestIdentity
from app.retrieval.export_archive import ExportCollectionBudget
sys.path.pop(0)
for name in list(sys.modules):
    if name == "app" or name.startswith("app."):
        del sys.modules[name]


@unittest.skipUnless(os.getenv("CERTUS_TEST_DATABASE_URL"), "set a migrated disposable PostgreSQL test URL")
class ExportDatabaseTests(unittest.TestCase):
    def setUp(self):
        self.tenant = "export-budget-" + str(uuid4())
        self.user = "user-" + str(uuid4())
        self.identity = RequestIdentity(tenant_id=self.tenant, user_id=self.user)
        self.transferred = []
        self.addCleanup(self.cleanup)

    @contextmanager
    def connection(self):
        with closing(psycopg2.connect(os.environ["CERTUS_TEST_DATABASE_URL"],
            connect_timeout=3, options="-c statement_timeout=10000 -c lock_timeout=3000")) as connection, connection:
            yield connection

    @contextmanager
    def cursor(self):
        transferred = self.transferred
        class ObservedCursor(RealDictCursor):
            def execute(self, query, params=None):
                result = super().execute(query, params)
                if self.description:
                    transferred.append(({column.name for column in self.description}, self.rowcount))
                return result
        with self.connection() as connection, connection.cursor(cursor_factory=ObservedCursor) as cursor:
            yield cursor

    def sql(self, query, parameters=()):
        with self.connection() as connection, connection.cursor() as cursor:
            cursor.execute(query, parameters)
            return cursor.fetchall() if cursor.description else None

    def task(self, *, tenant=None, user=None, description="Exact task text"):
        return self.sql("""INSERT INTO tasks(tenant_id,user_id,title,description,tags)
            VALUES(%s,%s,'Export task',%s,ARRAY['proof','café']) RETURNING id""",
            (tenant or self.tenant, user or self.user, description))[0][0]

    def cleanup(self):
        with self.connection() as connection, connection.cursor() as cursor:
            for tenant,user in ((self.tenant,self.user),(self.tenant+"-other",self.user),(self.tenant,self.user+"-other")):
                export_api.set_db_request_context(cursor,RequestIdentity(tenant_id=tenant,user_id=user))
                cursor.execute("DELETE FROM memories WHERE tenant_id=%s AND user_id=%s",(tenant,user))
        self.sql("DELETE FROM data_exports WHERE organization_id=%s", (self.tenant,))
        self.sql("DELETE FROM tasks WHERE tenant_id=ANY(%s)", ([self.tenant,self.tenant+"-other"],))
        self.sql("DELETE FROM realtime_events WHERE organization_id=ANY(%s)", ([self.tenant,self.tenant+"-other"],))
        self.sql("DELETE FROM webhook_events WHERE organization_id=ANY(%s)", ([self.tenant,self.tenant+"-other"],))
        self.sql("DELETE FROM organization WHERE id=%s", (self.tenant,))

    def generate(self):
        with patch.object(export_api,"get_db_cursor",self.cursor):
            return export_api.generate_data_export(self.identity,include_originals=False)

    def assert_no_task_payload_transferred(self):
        self.assertFalse(any({"title","description","tenant_id"} <= columns and count > 0
                             for columns,count in self.transferred),
                         "oversized task rows reached the client before admission")
        self.assertEqual(self.sql("SELECT count(*) FROM data_exports WHERE organization_id=%s",(self.tenant,))[0][0],0)

    def test_wide_rows_are_rejected_before_the_driver_buffers_them(self):
        task = self.task(description="x" * 32_768)
        stored_bytes, text_bytes = self.sql("""SELECT pg_column_size(description),octet_length(description)
            FROM tasks WHERE id=%s""",(str(task),))[0]
        self.assertLess(stored_bytes,1024)
        self.assertGreater(text_bytes,1024)
        with patch.object(export_api.settings,"MAX_EXPORT_SOURCE_BYTES",1024):
            with self.assertRaises(HTTPException) as error:
                self.generate()
        self.assertEqual(error.exception.status_code,413)
        self.assert_no_task_payload_transferred()

    def test_record_limit_rejects_before_collection_transfer(self):
        self.sql("""INSERT INTO tasks(tenant_id,user_id,title)
            SELECT %s,%s,'Task '||number FROM generate_series(1,101) number""",(self.tenant,self.user))
        with patch.object(export_api.settings,"MAX_EXPORT_RECORDS",100):
            with self.assertRaises(HTTPException) as error:
                self.generate()
        self.assertEqual(error.exception.status_code,413)
        self.assert_no_task_payload_transferred()

    def test_singleton_metadata_is_also_admitted_before_transfer(self):
        self.sql('''INSERT INTO organization(id,name,slug,"createdAt",metadata)
            VALUES(%s,'Budget fixture',%s,NOW(),%s)''',(self.tenant,self.tenant,"x"*32_768))
        with patch.object(export_api.settings,"MAX_EXPORT_SOURCE_BYTES",1024):
            with self.assertRaises(HTTPException) as error:
                self.generate()
        self.assertEqual(error.exception.status_code,413)
        self.assertFalse(any({"slug","metadata"} <= columns and count > 0 for columns,count in self.transferred))

    def test_valid_archive_preserves_scope_values_and_download_authorization(self):
        own = self.task(description="Résumé € exact content")
        self.task(tenant=self.tenant+"-other")
        self.task(user=self.user+"-other")
        with self.connection() as connection, connection.cursor() as cursor:
            for tenant,user in ((self.tenant,self.user),(self.tenant+"-other",self.user),(self.tenant,self.user+"-other")):
                export_api.set_db_request_context(cursor,RequestIdentity(tenant_id=tenant,user_id=user))
                cursor.execute("""INSERT INTO memories(tenant_id,user_id,fact,embedding_profile)
                    VALUES(%s,%s,%s,'embedding-space:v1:local:local-lexical-v2:1536')""",
                    (tenant,user,tenant+":"+user))
        result = self.generate()
        self.assertEqual(result["record_counts"]["tasks"],1)
        with patch.object(export_api,"get_db_cursor",self.cursor):
            response = export_api.download_data_export(UUID(result["export_id"]),self.identity)
            with self.assertRaises(HTTPException) as error:
                export_api.download_data_export(UUID(result["export_id"]),
                    RequestIdentity(tenant_id=self.tenant,user_id=self.user+"-other"))
        self.assertEqual(error.exception.status_code,404)
        with ZipFile(BytesIO(response.body)) as archive:
            payload = json.loads(archive.read("certus-export.json"))
        self.assertEqual(payload["manifest"]["schema_version"],13)
        self.assertEqual(payload["data"]["tasks"][0]["id"],str(own))
        self.assertEqual(payload["data"]["tasks"][0]["description"],"Résumé € exact content")
        self.assertEqual(payload["data"]["tasks"][0]["tags"],["proof","café"])
        self.assertEqual([row["fact"] for row in payload["data"]["memories"]],[self.tenant+":"+self.user])

    def test_admission_and_payload_share_the_repeatable_read_snapshot(self):
        task = self.task(description="Snapshot text")
        with self.cursor() as cursor:
            export_api._configure_export_transaction(cursor,self.identity)
            wrapped = export_api._BudgetedExportCursor(cursor,
                ExportCollectionBudget(max_source_bytes=1024,max_records=10))
            original_execute = wrapped.execute
            def concurrent_update(query, params=None):
                result = original_execute(query,params)
                if "AS export_admission" in query:
                    self.sql("UPDATE tasks SET description=%s WHERE id=%s",("x"*32_768,str(task)))
                return result
            with patch.object(wrapped,"execute",side_effect=concurrent_update):
                rows = export_api._rows(wrapped,"SELECT description FROM tasks WHERE id=%s",(str(task),))
        self.assertEqual(rows,[{"description":"Snapshot text"}])
        self.assertEqual(len(self.sql("SELECT description FROM tasks WHERE id=%s",(str(task),))[0][0]),32_768)

    def test_collection_uses_remaining_global_bytes_before_the_next_transfer(self):
        task = self.task(description="x"*800)
        with self.cursor() as cursor:
            export_api._configure_export_transaction(cursor,self.identity)
            wrapped = export_api._BudgetedExportCursor(cursor,
                ExportCollectionBudget(max_source_bytes=1024,max_records=10))
            query = "SELECT description FROM tasks WHERE id=%s"
            self.assertEqual(len(export_api._rows(wrapped,query,(str(task),))),1)
            with self.assertRaises(export_api.ExportArchiveLimitExceeded):
                export_api._rows(wrapped,query,(str(task),))
        self.assertEqual(sum(columns == {"description"} and count == 1 for columns,count in self.transferred),1)

    def test_deadline_after_admission_aborts_before_payload_transfer(self):
        task = self.task()
        with self.cursor() as cursor:
            export_api._configure_export_transaction(cursor,self.identity)
            wrapped = export_api._BudgetedExportCursor(cursor,
                ExportCollectionBudget(max_source_bytes=1024,max_records=10),deadline=100)
            with patch.object(export_api,"monotonic",side_effect=[0,200]):
                with self.assertRaises(HTTPException) as error:
                    export_api._rows(wrapped,"SELECT description FROM tasks WHERE id=%s",(str(task),))
        self.assertEqual(error.exception.status_code,504)
        self.assertFalse(any(columns == {"description"} and count > 0 for columns,count in self.transferred))


if __name__ == "__main__":
    unittest.main()
