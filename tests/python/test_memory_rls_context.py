import sys
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


ORCHESTRATION_ROOT = Path(__file__).resolve().parents[2] / "services" / "orchestration"
MCP_TOOLS_ROOT = Path(__file__).resolve().parents[2] / "services" / "mcp-tools"


def clear_app_modules():
    for module_name in list(sys.modules):
        if module_name == "app" or module_name.startswith("app."):
            del sys.modules[module_name]


clear_app_modules()
mcp_path = str(MCP_TOOLS_ROOT)
mcp_path_index = sys.path.index(mcp_path) if mcp_path in sys.path else None
if mcp_path_index is not None:
    sys.path.remove(mcp_path)
sys.path.insert(0, str(ORCHESTRATION_ROOT))

from app.api import export as export_api  # noqa: E402
from app.core import db as db_module  # noqa: E402
from app.core.identity import RequestIdentity  # noqa: E402
from app.retrieval import memory as memory_module  # noqa: E402

sys.path.remove(str(ORCHESTRATION_ROOT))
if mcp_path_index is not None:
    sys.path.insert(min(mcp_path_index, len(sys.path)), mcp_path)
clear_app_modules()


IDENTITY = RequestIdentity(tenant_id="tenant-a", user_id="user-a")
SET_CONTEXT_SQL = "SELECT set_config('app.tenant_id', %s, true), set_config('app.user_id', %s, true)"


class RecordingCursor:
    def __init__(self, rows=None):
        self.statements = []
        self.rows = list(rows or [])

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, query, params=None):
        self.statements.append((" ".join(query.split()), params))

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return None


class RecordingConnection:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self, cursor_factory=None):
        del cursor_factory
        return self._cursor


@contextmanager
def connection_scope(connection):
    yield connection


class MemoryRlsContextTests(unittest.TestCase):
    def test_cursor_sets_parameterized_transaction_local_identity_before_query(self):
        cursor = RecordingCursor()
        connection = RecordingConnection(cursor)

        with patch.object(
            db_module,
            "get_db_connection",
            return_value=connection_scope(connection),
        ):
            with db_module.get_db_cursor(identity=IDENTITY) as active_cursor:
                active_cursor.execute("SELECT id FROM memories")

        self.assertEqual(cursor.statements[0], (SET_CONTEXT_SQL, ("tenant-a", "user-a")))
        self.assertEqual(cursor.statements[1], ("SELECT id FROM memories", None))

    def test_cursor_without_identity_does_not_supply_a_default_tenant(self):
        cursor = RecordingCursor()
        connection = RecordingConnection(cursor)

        with patch.object(
            db_module,
            "get_db_connection",
            return_value=connection_scope(connection),
        ):
            with db_module.get_db_cursor() as active_cursor:
                active_cursor.execute("SELECT id FROM memories")

        self.assertEqual(cursor.statements, [("SELECT id FROM memories", None)])

    def test_memory_retrieval_sets_context_for_read_and_access_count_update(self):
        cursor = RecordingCursor(rows=[{"id": "memory-1", "fact": "fact"}])
        connection = RecordingConnection(cursor)
        embedding = SimpleNamespace(
            vector=[0.1, 0.2],
            profile=SimpleNamespace(
                identifier="embedding-space:v1:local:local-lexical-v2:1536"
            ),
        )

        with patch.object(
            memory_module,
            "get_db_connection",
            return_value=connection_scope(connection),
        ):
            rows = memory_module.retrieve_relevant_memories(
                "query",
                IDENTITY.tenant_id,
                IDENTITY.user_id,
                query_embedding=embedding,
                allow_embedding_generation=False,
            )

        self.assertEqual(rows, [{"id": "memory-1", "fact": "fact"}])
        self.assertEqual(cursor.statements[0], (SET_CONTEXT_SQL, ("tenant-a", "user-a")))
        memory_queries = [
            index
            for index, (query, _) in enumerate(cursor.statements)
            if "FROM memories" in query or "UPDATE memories" in query
        ]
        self.assertEqual(len(memory_queries), 2)
        self.assertTrue(all(index > 0 for index in memory_queries))

    def test_export_sets_context_before_profile_scan_and_archive_memory_read(self):
        cursor = RecordingCursor()

        export_api._collect_export_data(cursor, IDENTITY)

        self.assertEqual(cursor.statements[0], (SET_CONTEXT_SQL, ("tenant-a", "user-a")))
        memory_queries = [
            index
            for index, (query, _) in enumerate(cursor.statements)
            if "FROM memories" in query
        ]
        self.assertEqual(len(memory_queries), 2)
        self.assertTrue(all(index > 0 for index in memory_queries))


if __name__ == "__main__":
    unittest.main()
