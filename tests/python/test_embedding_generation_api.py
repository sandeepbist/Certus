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

from app.api import embedding_generations
from app.core.identity import RequestIdentity

sys.path.remove(str(ORCHESTRATION_ROOT))
for module_name in list(sys.modules):
    if module_name == "app" or module_name.startswith("app."):
        del sys.modules[module_name]


IDENTITY = RequestIdentity(tenant_id="tenant-proof", user_id="user-proof")
GENERATION_ID = UUID("10000000-0000-4000-8000-000000000001")


def generation_row() -> dict:
    now = datetime(2026, 9, 10, tzinfo=timezone.utc)
    return {
        "id": GENERATION_ID,
        "embedding_profile": (
            "embedding-space:v1:local:local-lexical-v2:1536"
        ),
        "source_corpus_revision": 4,
        "current_corpus_revision": 5,
        "status": "building",
        "expected_chunk_count": 10,
        "embedded_chunk_count": 7,
        "failed_chunk_count": 1,
        "previous_generation_id": None,
        "evaluation_report": {
            "schema_version": 2,
            "evaluation_profile": "embedding_generation_integrity_v1",
            "evaluation_source": "database_authoritative",
            "decision": "approved",
            "gates_passed": True,
            "quality_claim": "not_evaluated",
            "baseline_fingerprint": "base",
            "candidate_fingerprint": "candidate",
            "metrics": {"observed_chunk_count": 10},
            "private_diagnostics": "must-not-leak",
        },
        "last_error": "private provider diagnostic",
        "created_at": now,
        "updated_at": now,
        "sealed_at": None,
        "activated_at": None,
        "retired_at": None,
        "stale_at": None,
        "rollback_until": None,
        "retain_until": None,
    }


class FakeCursor:
    def __init__(self, *, fetchones=None, fetchalls=None):
        self.fetchones = list(fetchones or [])
        self.fetchalls = list(fetchalls or [])
        self.statements = []

    def execute(self, statement, params):
        self.statements.append((statement, params))

    def fetchone(self):
        return self.fetchones.pop(0) if self.fetchones else None

    def fetchall(self):
        return self.fetchalls.pop(0) if self.fetchalls else []


@contextmanager
def cursor_context(cursor):
    yield cursor


class EmbeddingGenerationApiTests(unittest.TestCase):
    def test_start_profile_must_be_a_supported_serving_space(self):
        request = embedding_generations.StartEmbeddingGenerationRequest(
            embedding_profile=(
                "embedding-space:v1:local:local-lexical-v2:1536"
            )
        )
        self.assertEqual(
            request.embedding_profile,
            "embedding-space:v1:local:local-lexical-v2:1536",
        )
        with self.assertRaises(ValueError):
            embedding_generations.StartEmbeddingGenerationRequest(
                embedding_profile="embedding-space:v1:legacy:unknown:1536"
            )

    def test_list_is_scoped_bounded_and_redacts_private_diagnostics(self):
        cursor = FakeCursor(fetchalls=[[generation_row(), generation_row()]])
        with patch.object(
            embedding_generations,
            "get_db_cursor",
            return_value=cursor_context(cursor),
        ):
            response = embedding_generations.list_embedding_generations(
                status="building",
                limit=1,
                page_cursor=None,
                identity=IDENTITY,
            )

        self.assertEqual(response["pagination"]["next_cursor"], str(GENERATION_ID))
        item = response["generations"][0]
        self.assertEqual(item["progress"]["remaining"], 3)
        self.assertFalse(item["corpus"]["is_current"])
        self.assertTrue(item["has_error"])
        self.assertNotIn("last_error", item)
        self.assertNotIn("private_diagnostics", item["evaluation"])
        self.assertEqual(item["evaluation"]["quality_claim"], "not_evaluated")
        sql, params = cursor.statements[-1]
        self.assertIn("tenant_id = %s", sql)
        self.assertIn("user_id = %s", sql)
        self.assertEqual(params[:3], ["tenant-proof", "user-proof", "building"])
        self.assertEqual(params[-1], 2)

    def test_start_uses_database_snapshot_function_and_returns_progress(self):
        cursor = FakeCursor(
            fetchones=[{"generation_id": GENERATION_ID}, generation_row()],
        )
        request = embedding_generations.StartEmbeddingGenerationRequest(
            embedding_profile=(
                "embedding-space:v1:local:local-lexical-v2:1536"
            )
        )
        with patch.object(
            embedding_generations,
            "get_db_cursor",
            return_value=cursor_context(cursor),
        ):
            response = embedding_generations.start_embedding_generation(
                request=request,
                identity=IDENTITY,
            )

        self.assertIn("start_workspace_embedding_generation", cursor.statements[0][0])
        self.assertEqual(cursor.statements[0][1][:2], ("tenant-proof", "user-proof"))
        self.assertEqual(response["generation"]["id"], str(GENERATION_ID))

    def test_cancel_and_rollback_fail_closed_on_scope_or_state(self):
        missing = FakeCursor(fetchones=[None, None])
        with patch.object(
            embedding_generations,
            "get_db_cursor",
            return_value=cursor_context(missing),
        ):
            with self.assertRaises(HTTPException) as raised:
                embedding_generations.cancel_embedding_generation(
                    generation_id=GENERATION_ID,
                    identity=IDENTITY,
                )
        self.assertEqual(raised.exception.status_code, 404)

        active = FakeCursor(
            fetchones=[{"status": "active"}, {"rolled_back": True}]
        )
        with patch.object(
            embedding_generations,
            "get_db_cursor",
            return_value=cursor_context(active),
        ):
            response = embedding_generations.rollback_embedding_generation(
                generation_id=GENERATION_ID,
                identity=IDENTITY,
            )
        self.assertEqual(response["status"], "rolled_back")
        self.assertIn("pg_advisory_xact_lock", active.statements[0][0])
        self.assertIn("rollback_workspace_embedding_generation", active.statements[2][0])

    def test_activation_requires_ready_scope_and_bounded_rollback_window(self):
        cursor = FakeCursor(
            fetchones=[{"status": "ready"}, {"activated": True}]
        )
        request = embedding_generations.ActivateEmbeddingGenerationRequest(
            rollback_window_hours=72
        )
        with patch.object(
            embedding_generations,
            "get_db_cursor",
            return_value=cursor_context(cursor),
        ):
            response = embedding_generations.activate_embedding_generation(
                generation_id=GENERATION_ID,
                request=request,
                identity=IDENTITY,
            )

        self.assertEqual(response["status"], "active")
        self.assertEqual(response["rollback_window_hours"], 72)
        self.assertIn("pg_advisory_xact_lock", cursor.statements[0][0])
        self.assertIn(
            "activate_workspace_embedding_generation",
            cursor.statements[2][0],
        )
        self.assertEqual(cursor.statements[2][1], (str(GENERATION_ID), 72))

        with self.assertRaises(ValueError):
            embedding_generations.ActivateEmbeddingGenerationRequest(
                rollback_window_hours=721
            )


if __name__ == "__main__":
    unittest.main()
