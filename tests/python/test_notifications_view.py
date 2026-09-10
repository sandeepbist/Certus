import unittest

from services.orchestration.app.retrieval.notifications_view import (
    notification_record,
    safe_action_url,
)


class NotificationProjectionTests(unittest.TestCase):
    def test_safe_action_url_only_allows_same_origin_paths(self):
        self.assertEqual(safe_action_url(" /documents/abc "), "/documents/abc")
        self.assertIsNone(safe_action_url("https://attacker.example/path"))
        self.assertIsNone(safe_action_url("//attacker.example/path"))
        self.assertIsNone(safe_action_url("javascript:alert(1)"))
        self.assertIsNone(safe_action_url(None))

    def test_notification_record_normalizes_nullable_json(self):
        record = notification_record({"id": "one", "metadata": None, "action_url": "/tasks"})
        self.assertEqual(record["metadata"], {})
        self.assertEqual(record["action_url"], "/tasks")


if __name__ == "__main__":
    unittest.main()

