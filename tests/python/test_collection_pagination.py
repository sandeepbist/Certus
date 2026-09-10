import sys
import unittest
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
from uuid import UUID

from fastapi import HTTPException

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
ORCHESTRATION_ROOT = REPOSITORY_ROOT / "services" / "orchestration"
sys.path.insert(0, str(ORCHESTRATION_ROOT))

from app.api import automations, memories, notifications, tasks, traces, webhooks
from app.core.identity import RequestIdentity

sys.path.remove(str(ORCHESTRATION_ROOT))
for module_name in list(sys.modules):
    if module_name == "app" or module_name.startswith("app."):
        del sys.modules[module_name]


IDENTITY = RequestIdentity(tenant_id="tenant-proof", user_id="user-proof")


class FakeCursor:
    def __init__(self, *, anchor=None, rows=None):
        self.anchor = anchor
        self.rows = list(rows or [])
        self.statements = []

    def execute(self, statement, params):
        self.statements.append((statement, params))

    def fetchone(self):
        return self.anchor

    def fetchall(self):
        return self.rows


class SequenceCursor(FakeCursor):
    def __init__(self, *, fetchones=None, fetchalls=None, rows=None):
        super().__init__(rows=rows)
        self.fetchones = list(fetchones or [])
        self.fetchalls = list(fetchalls or [])

    def fetchone(self):
        return self.fetchones.pop(0) if self.fetchones else None

    def fetchall(self):
        return self.fetchalls.pop(0) if self.fetchalls else super().fetchall()


@contextmanager
def cursor_context(cursor):
    yield cursor


def memory_row(value: int):
    return {
        "id": UUID(int=value),
        "fact": f"memory-{value}",
        "category": "fact",
        "confidence": 1.0,
        "access_count": 0,
        "is_active": True,
        "embedding_provider": "local",
        "embedding_profile": "local:v1",
        "created_at": datetime(2026, 9, 1, tzinfo=timezone.utc),
        "last_accessed_at": None,
    }


def automation_row(value: int):
    return {
        "id": UUID(int=value),
        "name": f"rule-{value}",
        "trigger_type": "on_document_uploaded",
        "trigger_conditions": {"type": "always"},
        "actions": [{"type": "notify"}],
        "is_active": True,
        "execution_count": 0,
        "last_executed_at": None,
        "version": 1,
        "created_at": datetime(2026, 9, 1, tzinfo=timezone.utc),
        "updated_at": datetime(2026, 9, 1, tzinfo=timezone.utc),
    }


def task_row(value: int):
    recorded_at = datetime(2026, 9, 1, tzinfo=timezone.utc)
    return {
        "id": UUID(int=value),
        "title": f"task-{value}",
        "description": "proof",
        "status": "pending",
        "priority": "medium",
        "due_date": None,
        "tags": ["proof"],
        "version": 1,
        "source_document_id": None,
        "source_agent_run_id": None,
        "created_at": recorded_at,
        "updated_at": recorded_at,
    }


def notification_row(value: int):
    return {
        "id": UUID(int=value),
        "type": "task_due",
        "title": f"notification-{value}",
        "body": "proof",
        "metadata": {},
        "action_url": "/tasks",
        "is_read": False,
        "created_at": datetime(2026, 9, 1, tzinfo=timezone.utc),
    }


def trace_row(value: int):
    return {
        "id": UUID(int=value),
        "input_query": f"proof trace {value}",
        "model_used": "proof-model",
        "total_tokens": value,
        "estimated_cost_usd": None,
        "latency_ms": value,
        "eval_score": None,
        "answer_status": "supported",
        "grounding_profile": "certus_atomic_claim_evidence:v1",
        "replay_of_run_id": None,
        "replay_mode": "original",
        "status": "completed",
        "created_at": datetime(2026, 9, 1, tzinfo=timezone.utc),
        "completed_at": datetime(2026, 9, 1, tzinfo=timezone.utc),
    }


class CollectionPaginationTests(unittest.TestCase):
    def test_webhook_delivery_page_is_bounded_and_stably_ordered(self):
        attempted_at = datetime(2026, 9, 1, tzinfo=timezone.utc)
        rows = [
            {
                "id": UUID(int=value), "delivery_key": f"key-{value}",
                "event_id": UUID(int=value + 10), "event_type": "document_ready",
                "response_status": 200, "response_body": "ok", "success": True,
                "error_message": None, "duration_ms": 10, "attempt_number": 1,
                "attempted_at": attempted_at,
            }
            for value in (3, 2, 1)
        ]
        cursor = FakeCursor(rows=rows)
        with patch.object(webhooks, "get_db_cursor", return_value=cursor_context(cursor)):
            response = webhooks.list_webhook_deliveries(
                webhook_id=UUID(int=50), limit=2, page_cursor=None, identity=IDENTITY,
            )

        self.assertEqual(len(response["deliveries"]), 2)
        self.assertEqual(response["pagination"]["next_cursor"], str(UUID(int=2)))
        statement, params = cursor.statements[0]
        self.assertIn("ORDER BY delivery.attempted_at DESC, delivery.id DESC", statement)
        self.assertNotIn("OFFSET", statement)
        self.assertEqual(params[-1], 3)

    def test_document_list_source_uses_filtered_tuple_keysets(self):
        source = (REPOSITORY_ROOT / "services" / "ingestion" / "app" / "main.py").read_text()
        endpoint = source[source.index('@app.get("/documents")'):source.index('@app.get("/documents/{document_id}")')]

        self.assertIn('alias="cursor"', endpoint)
        self.assertIn("title_search_vector @@ websearch_to_tsquery", endpoint)
        self.assertIn("lower(document.title) LIKE", endpoint)
        self.assertIn("document.tags @> ARRAY[%s]::TEXT[]", endpoint)
        self.assertIn("document.source_type = %s", endpoint)
        self.assertIn("(document.created_at, document.id) <", endpoint)
        self.assertIn("ORDER BY document.created_at DESC, document.id DESC", endpoint)
        self.assertNotIn("OFFSET", endpoint)

    def test_trace_page_is_filtered_bounded_and_stably_ordered(self):
        rows = [trace_row(3), trace_row(2), trace_row(1)]
        cursor = SequenceCursor(
            fetchalls=[rows, [{"model_used": "proof-model"}]],
        )
        with patch.object(traces, "get_db_cursor", return_value=cursor_context(cursor)):
            response = traces.list_traces(
                limit=2,
                page_cursor=None,
                q=" proof trace ",
                model="proof-model",
                status="completed",
                identity=IDENTITY,
            )

        self.assertEqual(len(response["traces"]), 2)
        self.assertEqual(response["pagination"]["next_cursor"], str(UUID(int=2)))
        page_statement, page_params = cursor.statements[0]
        self.assertIn("websearch_to_tsquery", page_statement)
        self.assertIn("ORDER BY created_at DESC, id DESC", page_statement)
        self.assertNotIn("OFFSET", page_statement)
        self.assertEqual(
            page_params,
            ["tenant-proof", "user-proof", "proof trace", "proof-model", "completed", 3],
        )

    def test_trace_continuation_requires_the_same_scope_and_filters(self):
        cursor = SequenceCursor(
            fetchones=[trace_row(2)],
            fetchalls=[[trace_row(1)], [{"model_used": "proof-model"}]],
        )
        with patch.object(traces, "get_db_cursor", return_value=cursor_context(cursor)):
            response = traces.list_traces(
                limit=2,
                page_cursor=UUID(int=2),
                q=None,
                model="proof-model",
                status="completed",
                identity=IDENTITY,
            )

        self.assertIsNone(response["pagination"]["next_cursor"])
        self.assertEqual(
            cursor.statements[0][1],
            [str(UUID(int=2)), "tenant-proof", "user-proof", "proof-model", "completed"],
        )
        self.assertIn("(created_at, id) <", cursor.statements[1][0])

    def test_foreign_trace_cursor_fails_closed(self):
        cursor = SequenceCursor(fetchones=[None])
        with patch.object(traces, "get_db_cursor", return_value=cursor_context(cursor)):
            with self.assertRaises(HTTPException) as raised:
                traces.list_traces(
                    limit=50,
                    page_cursor=UUID(int=99),
                    q=None,
                    model=None,
                    status=None,
                    identity=IDENTITY,
                )

        self.assertEqual(raised.exception.status_code, 422)

    def test_notification_page_is_bounded_and_returns_a_continuation(self):
        rows = [notification_row(3), notification_row(2), notification_row(1)]
        cursor = SequenceCursor(
            fetchones=[{"unread_count": 3}],
            fetchalls=[[{"type": "task_due"}], rows],
        )
        with patch.object(notifications, "get_db_cursor", return_value=cursor_context(cursor)):
            response = notifications.list_notifications(
                status="unread",
                notification_type="task_due",
                limit=2,
                page_cursor=None,
                identity=IDENTITY,
            )

        self.assertEqual(len(response["notifications"]), 2)
        self.assertEqual(response["pagination"]["next_cursor"], str(UUID(int=2)))
        page_statement, page_params = cursor.statements[-1]
        self.assertIn("ORDER BY created_at DESC, id DESC", page_statement)
        self.assertNotIn("OFFSET", page_statement)
        self.assertEqual(page_params[-1], 3)

    def test_notification_continuation_requires_the_same_scope_and_filters(self):
        anchor = notification_row(2)
        cursor = SequenceCursor(
            fetchones=[anchor, {"unread_count": 1}],
            fetchalls=[[{"type": "task_due"}], [notification_row(1)]],
        )
        with patch.object(notifications, "get_db_cursor", return_value=cursor_context(cursor)):
            response = notifications.list_notifications(
                status="unread",
                notification_type="task_due",
                limit=2,
                page_cursor=UUID(int=2),
                identity=IDENTITY,
            )

        self.assertIsNone(response["pagination"]["next_cursor"])
        self.assertEqual(
            cursor.statements[0][1],
            [str(UUID(int=2)), "tenant-proof", "user-proof", False, "task_due"],
        )
        self.assertIn("(created_at, id) <", cursor.statements[-1][0])

    def test_foreign_notification_cursor_fails_closed(self):
        cursor = SequenceCursor(fetchones=[None])
        with patch.object(notifications, "get_db_cursor", return_value=cursor_context(cursor)):
            with self.assertRaises(HTTPException) as raised:
                notifications.list_notifications(
                    status="all",
                    notification_type=None,
                    limit=25,
                    page_cursor=UUID(int=99),
                    identity=IDENTITY,
                )

        self.assertEqual(raised.exception.status_code, 422)

    def test_mark_all_read_returns_one_aggregate_row(self):
        cursor = SequenceCursor(fetchones=[(50_000,)])
        with patch.object(notifications, "get_db_cursor", return_value=cursor_context(cursor)):
            response = notifications.mark_all_notifications_read(identity=IDENTITY)

        self.assertEqual(response["updated_count"], 50_000)
        self.assertIn("WITH updated AS", cursor.statements[0][0])
        self.assertEqual(cursor.fetchalls, [])

    def test_memory_page_is_bounded_and_returns_an_opaque_continuation(self):
        cursor = FakeCursor(rows=[memory_row(3), memory_row(2), memory_row(1)])
        with patch.object(memories, "get_db_cursor", return_value=cursor_context(cursor)):
            response = memories.list_memories(limit=2, page_cursor=None, identity=IDENTITY)

        self.assertEqual([item["fact"] for item in response["memories"]], ["memory-3", "memory-2"])
        self.assertEqual(response["pagination"]["next_cursor"], str(UUID(int=2)))
        self.assertEqual(cursor.statements[-1][1][-1], 3)
        self.assertIn("ORDER BY confidence DESC, created_at DESC, id DESC", cursor.statements[-1][0])

    def test_memory_continuation_uses_the_scoped_anchor(self):
        anchor = memory_row(2)
        cursor = FakeCursor(anchor=anchor, rows=[memory_row(1)])
        with patch.object(memories, "get_db_cursor", return_value=cursor_context(cursor)):
            response = memories.list_memories(
                limit=2,
                page_cursor=UUID(int=2),
                identity=IDENTITY,
            )

        self.assertIsNone(response["pagination"]["next_cursor"])
        self.assertEqual(cursor.statements[0][1], (str(UUID(int=2)), "tenant-proof", "user-proof"))
        self.assertIn("(confidence, created_at, id) <", cursor.statements[1][0])

    def test_foreign_or_stale_memory_cursor_fails_closed(self):
        cursor = FakeCursor(anchor=None)
        with patch.object(memories, "get_db_cursor", return_value=cursor_context(cursor)):
            with self.assertRaises(HTTPException) as raised:
                memories.list_memories(
                    limit=50,
                    page_cursor=UUID(int=99),
                    identity=IDENTITY,
                )

        self.assertEqual(raised.exception.status_code, 422)

    def test_automation_rule_and_history_pages_are_bounded(self):
        rules_cursor = FakeCursor(
            rows=[automation_row(3), automation_row(2), automation_row(1)],
        )
        with patch.object(
            automations,
            "get_db_cursor",
            return_value=cursor_context(rules_cursor),
        ):
            rules = automations.list_automations(
                limit=2,
                page_cursor=None,
                identity=IDENTITY,
            )

        self.assertEqual(len(rules["rules"]), 2)
        self.assertEqual(rules["pagination"]["next_cursor"], str(UUID(int=2)))
        self.assertEqual(rules_cursor.statements[-1][1][-1], 3)

        history_rows = [
            {"id": UUID(int=value), "started_at": datetime(2026, 9, 1, tzinfo=timezone.utc)}
            for value in (3, 2, 1)
        ]
        history_cursor = FakeCursor(rows=history_rows)
        with patch.object(
            automations,
            "get_db_cursor",
            return_value=cursor_context(history_cursor),
        ):
            history = automations.automation_history(
                rule_id=UUID(int=10),
                limit=2,
                page_cursor=None,
                identity=IDENTITY,
            )

        self.assertEqual(len(history["executions"]), 2)
        self.assertEqual(history["pagination"]["next_cursor"], str(UUID(int=2)))
        self.assertIn("ORDER BY started_at DESC, id DESC", history_cursor.statements[-1][0])

    def test_task_page_is_filtered_bounded_and_returns_full_status_counts(self):
        counts = {"pending": 7, "in_progress": 2, "completed": 3, "cancelled": 1}
        cursor = SequenceCursor(
            rows=[task_row(3), task_row(2), task_row(1)],
            fetchones=[counts],
        )
        with patch.object(tasks, "get_db_cursor", return_value=cursor_context(cursor)):
            response = tasks.list_tasks(
                status="pending",
                q=" Proof ",
                limit=2,
                page_cursor=None,
                identity=IDENTITY,
            )

        self.assertEqual([item["title"] for item in response["tasks"]], ["task-3", "task-2"])
        self.assertEqual(response["pagination"]["next_cursor"], str(UUID(int=2)))
        self.assertEqual(response["status_counts"], counts)
        page_statement, page_params = cursor.statements[0]
        self.assertIn("websearch_to_tsquery", page_statement)
        self.assertIn("ORDER BY created_at DESC, id DESC", page_statement)
        self.assertEqual(page_params, ["tenant-proof", "user-proof", "pending", "Proof", "proof", 3])

    def test_task_continuation_requires_a_scoped_filtered_anchor(self):
        anchor = task_row(2)
        counts = {"pending": 1, "in_progress": 0, "completed": 0, "cancelled": 0}
        cursor = SequenceCursor(fetchones=[anchor, counts], rows=[task_row(1)])
        with patch.object(tasks, "get_db_cursor", return_value=cursor_context(cursor)):
            response = tasks.list_tasks(
                status="pending",
                q=None,
                limit=2,
                page_cursor=UUID(int=2),
                identity=IDENTITY,
            )

        self.assertIsNone(response["pagination"]["next_cursor"])
        self.assertEqual(
            cursor.statements[0][1],
            [str(UUID(int=2)), "tenant-proof", "user-proof", "pending"],
        )
        self.assertIn("(created_at, id) <", cursor.statements[1][0])


if __name__ == "__main__":
    unittest.main()
