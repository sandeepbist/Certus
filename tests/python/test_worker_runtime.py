import os
import unittest
import uuid
from unittest.mock import MagicMock, patch

from services.shared.worker_runtime import (
    WorkerIdentity,
    bounded_int_env,
    bounded_retention_days_env,
    connect_database,
    redis_connection_options,
    write_worker_heartbeat,
)


class WorkerRuntimeTests(unittest.TestCase):
    def test_database_connection_has_identity_and_server_enforced_budgets(self):
        with patch("services.shared.worker_runtime.psycopg2.connect") as connect:
            connect_database(
                "postgresql://example",
                application_name="certus-worker",
                connect_timeout_seconds=3,
                statement_timeout_ms=15_000,
                lock_timeout_ms=3_000,
            )

        connect.assert_called_once_with(
            "postgresql://example",
            application_name="certus-worker",
            connect_timeout=3,
            options="-c statement_timeout=15000 -c lock_timeout=3000",
        )

    def test_bounded_integer_environment_contract_fails_closed(self):
        with patch.dict(os.environ, {"CERTUS_TEST_BOUND": "7"}):
            self.assertEqual(bounded_int_env("CERTUS_TEST_BOUND", 3, 1, 10), 7)
        with patch.dict(os.environ, {"CERTUS_TEST_BOUND": "11"}):
            with self.assertRaisesRegex(ValueError, "between 1 and 10"):
                bounded_int_env("CERTUS_TEST_BOUND", 3, 1, 10)
        with patch.dict(os.environ, {"CERTUS_TEST_BOUND": "not-an-integer"}):
            with self.assertRaisesRegex(ValueError, "CERTUS_TEST_BOUND must be an integer"):
                bounded_int_env("CERTUS_TEST_BOUND", 3, 1, 10)

    def test_operational_retention_has_finite_policy_bounds(self):
        for value in ("1", "3650"):
            with self.subTest(value=value), patch.dict(
                os.environ,
                {"CERTUS_TEST_RETENTION": value},
            ):
                self.assertEqual(
                    bounded_retention_days_env("CERTUS_TEST_RETENTION", 30),
                    int(value),
                )
        for value in ("0", "3651", "forever"):
            with (
                self.subTest(value=value),
                patch.dict(os.environ, {"CERTUS_TEST_RETENTION": value}),
                self.assertRaises(ValueError),
            ):
                bounded_retention_days_env("CERTUS_TEST_RETENTION", 30)

    def test_redis_connections_are_bounded_and_health_checked(self):
        options = redis_connection_options(
            connect_timeout_seconds=3,
            socket_timeout_seconds=5,
        )

        self.assertEqual(options["socket_connect_timeout"], 3)
        self.assertEqual(options["socket_timeout"], 5)
        self.assertEqual(options["health_check_interval"], 30)
        self.assertTrue(options["socket_keepalive"])
        self.assertTrue(options["decode_responses"])

    def test_worker_identity_rejects_database_invalid_names(self):
        with self.assertRaises(ValueError):
            WorkerIdentity("Workflow Worker")
        identity = WorkerIdentity(
            "workflow-worker",
            instance_id=uuid.UUID("10000000-0000-0000-0000-000000000001"),
            hostname="worker-a",
            process_id=42,
        )
        self.assertEqual(identity.worker_type, "workflow-worker")

    def test_heartbeat_is_parameterized_and_terminal_rows_are_bounded(self):
        connection = MagicMock()
        connection.__enter__.return_value = connection
        cursor = MagicMock()
        connection.cursor.return_value.__enter__.return_value = cursor
        identity = WorkerIdentity(
            "embedding-worker",
            instance_id=uuid.UUID("10000000-0000-0000-0000-000000000001"),
            hostname="worker-a",
            process_id=42,
        )

        with patch(
            "services.shared.worker_runtime.connect_database",
            return_value=connection,
        ) as connect:
            recorded_at = write_worker_heartbeat(
                "postgresql://example",
                identity,
                "stopped",
                {"queue": {"outstanding": 0}},
                connect_timeout_seconds=3,
            )

        self.assertIsNotNone(recorded_at.tzinfo)
        connect.assert_called_once_with(
            "postgresql://example",
            application_name="certus-embedding-worker-heartbeat",
            connect_timeout_seconds=3,
            statement_timeout_ms=2_000,
            lock_timeout_ms=1_000,
        )
        insert_sql, insert_parameters = cursor.execute.call_args_list[1].args
        self.assertIn("ON CONFLICT (worker_type, instance_id)", insert_sql)
        self.assertEqual(insert_parameters[0], "embedding-worker")
        self.assertEqual(insert_parameters[4], "stopped")
        self.assertIn('"outstanding":0', insert_parameters[-1])
        self.assertIn("INTERVAL '7 days'", cursor.execute.call_args_list[2].args[0])

    def test_heartbeat_metadata_has_a_pre_database_size_bound(self):
        identity = WorkerIdentity("embedding-worker")
        with self.assertRaisesRegex(ValueError, "exceeds 12000 bytes"):
            write_worker_heartbeat(
                "postgresql://example",
                identity,
                "running",
                {"oversized": "x" * 12_001},
                connect_timeout_seconds=3,
            )


if __name__ == "__main__":
    unittest.main()
