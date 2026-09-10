import unittest

from services.shared.automation import condition_matches, extract_summary


class WorkflowContractTests(unittest.TestCase):
    def test_automation_conditions_are_structured_and_deterministic(self):
        event = {"tags": ["urgent", "audit"], "mime_type": "application/pdf"}

        self.assertTrue(condition_matches({"type": "always"}, event))
        self.assertTrue(condition_matches({"type": "tag_contains", "value": "urgent"}, event))
        self.assertTrue(
            condition_matches(
                {"type": "mime_type_equals", "value": "application/pdf"},
                event,
            )
        )
        self.assertFalse(condition_matches({"type": "tag_contains", "value": "other"}, event))

        with self.assertRaises(ValueError):
            condition_matches({"type": "arbitrary_code"}, event)

        with self.assertRaisesRegex(ValueError, "missing a supported type"):
            condition_matches({"expression": "true"}, event)

    def test_extract_summary_is_bounded_without_cutting_mid_word(self):
        summary = extract_summary("Alpha beta gamma delta", limit=12)

        self.assertEqual(summary, "Alpha beta…")


if __name__ == "__main__":
    unittest.main()
