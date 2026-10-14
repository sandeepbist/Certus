import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from services.shared.document_graph import DocumentGraph
from services.ingestion.app.originals import record_upload_error


class IngestionDiagnosticBoundaryTests(unittest.TestCase):
    def test_upload_failure_persistence_drops_exception_instance_data(self):
        secret = "sk-proj-upload-diagnostic"
        connection = MagicMock()
        connection_context = MagicMock()
        connection_context.__enter__.return_value = connection
        cursor = connection.cursor.return_value.__enter__.return_value
        cursor.fetchone.return_value = {
            "status": "object_stored",
            "attempt_count": 1,
            "locked_at": object(),
        }

        record_upload_error(
            lambda: connection_context,
            "10000000-0000-4000-8000-000000000001",
            RuntimeError(f"Authorization: Bearer {secret}"),
        )

        persisted_parameters = cursor.execute.call_args_list[-1].args[1]
        self.assertEqual(
            persisted_parameters[0],
            "document upload failed (RuntimeError)",
        )
        self.assertNotIn(secret, str(persisted_parameters))

    def test_graph_degradation_logs_drop_exception_instance_data(self):
        secret = "neo4j://user:private-password@graph.internal"
        failure = RuntimeError(secret)
        graph = DocumentGraph(lambda: None, MagicMock(side_effect=failure), timeout_seconds=10)
        with self.assertLogs("document_graph", level="WARNING") as captured:
            self.assertEqual(graph.entities("document", "tenant", "user"), ([], "degraded"))
        diagnostics = "\n".join(captured.output)
        self.assertIn("document entity lookup failed (RuntimeError)", diagnostics)
        self.assertNotIn(secret, diagnostics)
        self.assertNotIn("private-password", diagnostics)

    def test_ingestion_runtime_has_no_traceback_or_raw_persistence_sinks(self):
        source = Path("services/ingestion/app/main.py").read_text(encoding="utf-8")

        self.assertNotIn("logger.exception", source)
        self.assertNotIn("exc_info=True", source)
        self.assertNotIn("str(error)[:2000]", source)
        self.assertNotIn('f"Ingestion error: {e}"', source)


if __name__ == "__main__":
    unittest.main()
