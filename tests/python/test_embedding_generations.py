import unittest

from evals.embedding_generations import check_embedding_generation_report


class EmbeddingGenerationReportTests(unittest.TestCase):
    def test_complete_report_passes(self):
        report = {
            "schema": {"latest_migration": "043_embedding_generation_concurrency.sql"},
            "empty_workspace_lifecycle": {
                "cutover": ["retired", "active"],
                "rollback": ["active", "rolled_back"],
                "stale": ["stale", "stale"],
            },
            "nonempty_workspace_lifecycle": {
                "skipped": False,
                "wrong_owner_rejected": True,
                "coverage_complete": True,
                "sealed": True,
            },
            "run": {"persistent_rows": 0, "provider_calls": 0},
        }
        self.assertEqual(check_embedding_generation_report(report), [])

    def test_report_names_safety_regressions(self):
        report = {
            "schema": {"latest_migration": "041_embedding_generation_leases.sql"},
            "empty_workspace_lifecycle": {
                "cutover": ["active", "active"],
                "rollback": ["retired", "active"],
                "stale": ["active", "building"],
            },
            "nonempty_workspace_lifecycle": {
                "skipped": False,
                "wrong_owner_rejected": False,
                "coverage_complete": False,
                "sealed": False,
            },
            "run": {"persistent_rows": 1, "provider_calls": 1},
        }
        failures = check_embedding_generation_report(report)
        self.assertEqual(len(failures), 9)

    def test_empty_database_may_skip_only_the_nonempty_probe(self):
        report = {
            "schema": {"latest_migration": "043_embedding_generation_concurrency.sql"},
            "empty_workspace_lifecycle": {
                "cutover": ["retired", "active"],
                "rollback": ["active", "rolled_back"],
                "stale": ["stale", "stale"],
            },
            "nonempty_workspace_lifecycle": {"skipped": True},
            "run": {"persistent_rows": 0, "provider_calls": 0},
        }
        self.assertEqual(check_embedding_generation_report(report), [])


if __name__ == "__main__":
    unittest.main()
