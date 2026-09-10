import os
import unittest
from unittest.mock import Mock, patch

from services.ingestion.app.reconciler_runtime import (
    UploadReconcilerConfig,
    upload_reconciler_is_ready,
)


class UploadReconcilerRuntimeTests(unittest.TestCase):
    def _environment(self, **overrides: str) -> dict[str, str]:
        values = {
            "UPLOAD_RECONCILER_ENABLED": "true",
            "UPLOAD_RECONCILER_POLL_SECONDS": "5",
            "UPLOAD_RECONCILER_LEASE_SECONDS": "300",
            "UPLOAD_RECONCILER_MAX_ATTEMPTS": "12",
            "UPLOAD_RECONCILER_SHUTDOWN_SECONDS": "30",
        }
        values.update(overrides)
        return values

    def test_configuration_accepts_explicit_boundary_values(self):
        for environment, expected in (
            (
                self._environment(
                    UPLOAD_RECONCILER_ENABLED="false",
                    UPLOAD_RECONCILER_POLL_SECONDS="1",
                    UPLOAD_RECONCILER_LEASE_SECONDS="60",
                    UPLOAD_RECONCILER_MAX_ATTEMPTS="1",
                    UPLOAD_RECONCILER_SHUTDOWN_SECONDS="1",
                ),
                (False, 1, 60, 1, 1),
            ),
            (
                self._environment(
                    UPLOAD_RECONCILER_POLL_SECONDS="300",
                    UPLOAD_RECONCILER_LEASE_SECONDS="3600",
                    UPLOAD_RECONCILER_MAX_ATTEMPTS="100",
                    UPLOAD_RECONCILER_SHUTDOWN_SECONDS="300",
                ),
                (True, 300, 3600, 100, 300),
            ),
        ):
            with self.subTest(expected=expected), patch.dict(os.environ, environment):
                config = UploadReconcilerConfig.from_environment()
                self.assertEqual(
                    (
                        config.enabled,
                        config.poll_seconds,
                        config.lease_seconds,
                        config.max_attempts,
                        config.shutdown_seconds,
                    ),
                    expected,
                )

    def test_configuration_rejects_invalid_or_unbounded_values(self):
        invalid_values = (
            ("UPLOAD_RECONCILER_ENABLED", "yes"),
            ("UPLOAD_RECONCILER_POLL_SECONDS", "0"),
            ("UPLOAD_RECONCILER_POLL_SECONDS", "301"),
            ("UPLOAD_RECONCILER_LEASE_SECONDS", "59"),
            ("UPLOAD_RECONCILER_LEASE_SECONDS", "3601"),
            ("UPLOAD_RECONCILER_MAX_ATTEMPTS", "0"),
            ("UPLOAD_RECONCILER_MAX_ATTEMPTS", "101"),
            ("UPLOAD_RECONCILER_SHUTDOWN_SECONDS", "0"),
            ("UPLOAD_RECONCILER_SHUTDOWN_SECONDS", "301"),
            ("UPLOAD_RECONCILER_SHUTDOWN_SECONDS", "forever"),
        )
        for name, value in invalid_values:
            with (
                self.subTest(name=name, value=value),
                patch.dict(os.environ, self._environment(**{name: value})),
                self.assertRaises(ValueError),
            ):
                UploadReconcilerConfig.from_environment()

    def test_readiness_requires_the_enabled_thread_to_be_alive(self):
        enabled = UploadReconcilerConfig(True, 5, 300, 12, 30)
        disabled = UploadReconcilerConfig(False, 5, 300, 12, 30)
        live_worker = Mock()
        live_worker.is_alive.return_value = True
        dead_worker = Mock()
        dead_worker.is_alive.return_value = False

        self.assertTrue(upload_reconciler_is_ready(disabled, None))
        self.assertFalse(upload_reconciler_is_ready(enabled, None))
        self.assertTrue(upload_reconciler_is_ready(enabled, live_worker))
        self.assertFalse(upload_reconciler_is_ready(enabled, dead_worker))


if __name__ == "__main__":
    unittest.main()
