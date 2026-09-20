import unittest

from services.shared.safe_errors import safe_error_summary


class SafeErrorSummaryTests(unittest.TestCase):
    def test_never_copies_exception_instance_data(self):
        secret = "sk-proj-example-secret-value"
        error = RuntimeError(
            f"Authorization: Bearer {secret}\n"
            f"postgresql://user:{secret}@database.internal/certus"
        )

        summary = safe_error_summary(error, operation="embedding provider request")

        self.assertEqual(
            summary,
            "embedding provider request failed (RuntimeError)",
        )
        self.assertNotIn(secret, summary)
        self.assertNotIn("database.internal", summary)
        self.assertNotIn("\n", summary)

    def test_sanitizes_programmer_supplied_labels(self):
        summary = safe_error_summary(
            ValueError("untrusted instance text"),
            operation="worker\noperation/token",
        )

        self.assertEqual(summary, "worker_operation_token failed (ValueError)")


if __name__ == "__main__":
    unittest.main()
