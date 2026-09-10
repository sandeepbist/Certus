import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

ORCHESTRATION_ROOT = Path(__file__).resolve().parents[2] / "services" / "orchestration"
sys.path.insert(0, str(ORCHESTRATION_ROOT))

from app.api import export as export_api  # noqa: E402
from app.api.export import (  # noqa: E402
    _BudgetedExportCursor,
    _configure_export_transaction,
)
from app.core.identity import RequestIdentity  # noqa: E402
from app.retrieval.export_archive import (  # noqa: E402
    ExportArchiveLimitExceeded,
    ExportCollectionBudget,
)


class FakeCursor:
    def __init__(self, *, batches=None, lock_acquired=True):
        self.batches = list(batches or [])
        self.lock_acquired = lock_acquired
        self.executions = []

    def execute(self, query, params=None):
        self.executions.append((" ".join(query.split()), params))

    def fetchmany(self, size):
        del size
        return self.batches.pop(0) if self.batches else []

    def fetchone(self):
        return {"acquired": self.lock_acquired}


class ExportGuardrailTests(unittest.TestCase):
    def test_budgeted_cursor_fetches_in_batches_and_stops_before_overflow(self):
        cursor = FakeCursor(batches=[[{"id": "one"}], [{"id": "two"}]])
        budget = ExportCollectionBudget(max_source_bytes=23, max_records=10)
        wrapped = _BudgetedExportCursor(cursor, budget)

        with patch.object(export_api.settings, "EXPORT_FETCH_BATCH_SIZE", 1):
            with self.assertRaises(ExportArchiveLimitExceeded):
                wrapped.fetchall()

        self.assertEqual(budget.records, 1)
        self.assertEqual(budget.source_bytes, 12)

    def test_transaction_is_repeatable_read_timed_and_single_flight(self):
        cursor = FakeCursor(lock_acquired=True)
        identity = RequestIdentity(tenant_id="tenant-a", user_id="user-a")

        _configure_export_transaction(cursor, identity)

        self.assertEqual(
            cursor.executions[0],
            ("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ", None),
        )
        self.assertEqual(
            cursor.executions[1][0],
            "SELECT set_config('statement_timeout', %s, true)",
        )
        self.assertEqual(
            cursor.executions[1][1],
            (f"{export_api.settings.EXPORT_STATEMENT_TIMEOUT_MS}ms",),
        )
        self.assertIn("pg_try_advisory_xact_lock", cursor.executions[2][0])
        self.assertEqual(
            cursor.executions[2][1],
            ("certus:data-export:tenant-a:user-a",),
        )

    def test_transaction_rejects_concurrent_export(self):
        cursor = FakeCursor(lock_acquired=False)

        with self.assertRaises(HTTPException) as raised:
            _configure_export_transaction(
                cursor,
                RequestIdentity(tenant_id="tenant-a", user_id="user-a"),
            )

        self.assertEqual(raised.exception.status_code, 409)


if __name__ == "__main__":
    unittest.main()
