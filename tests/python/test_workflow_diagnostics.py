import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from services.workflows.app import dispatcher


class WorkflowDiagnosticBoundaryTests(unittest.TestCase):
    def test_outbox_release_functions_never_persist_exception_instance_data(self):
        secret = "whsec_private-diagnostic"
        failure = RuntimeError(f"Authorization: Bearer {secret}")
        cases = (
            (
                dispatcher.release_automation_event,
                "automation publication failed (RuntimeError)",
            ),
            (
                dispatcher.release_embedding_job,
                "embedding publication failed (RuntimeError)",
            ),
            (
                dispatcher.release_webhook_event,
                "webhook dispatch failed (RuntimeError)",
            ),
            (
                dispatcher.release_notification_event,
                "notification publication failed (RuntimeError)",
            ),
            (
                dispatcher.release_realtime_event,
                "realtime publication failed (RuntimeError)",
            ),
        )

        for release, expected_summary in cases:
            with self.subTest(release=release.__name__):
                connection = MagicMock()
                connection_context = MagicMock()
                connection_context.__enter__.return_value = connection
                cursor = connection.cursor.return_value.__enter__.return_value
                with patch.object(
                    dispatcher,
                    "database_connection",
                    return_value=connection_context,
                ):
                    release(
                        "10000000-0000-4000-8000-000000000001",
                        2,
                        failure,
                    )

                parameters = cursor.execute.call_args.args[1]
                self.assertEqual(parameters[1], expected_summary)
                self.assertNotIn(secret, str(parameters))

    def test_workflow_runtime_diagnostics_do_not_stringify_exceptions(self):
        for relative_path in (
            "services/workflows/app/activities.py",
            "services/workflows/app/worker.py",
            "services/workflows/app/workflows.py",
        ):
            source = Path(relative_path).read_text(encoding="utf-8")
            with self.subTest(relative_path=relative_path):
                self.assertNotIn("str(error)", source)
                self.assertNotIn("logger.exception", source)


if __name__ == "__main__":
    unittest.main()
