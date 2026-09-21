import unittest

from services.shared.safe_errors import safe_error_summary, safe_log_value


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

    def test_log_values_are_single_line_bounded_and_keep_field_boundaries(self):
        malicious = "document-a\r\n2026-09-21 [INFO] forged\x00tail" + ("x" * 200)

        value = safe_log_value(malicious)

        self.assertEqual(len(value), 160)
        self.assertNotIn("\r", value)
        self.assertNotIn("\n", value)
        self.assertNotIn("\x00", value)
        self.assertIn("document-a_2026-09-21 [INFO] forged_tail", value)


if __name__ == "__main__":
    unittest.main()
