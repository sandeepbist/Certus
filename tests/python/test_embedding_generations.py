import unittest

from evals.embedding_generations import check_embedding_generation_report


class EmbeddingGenerationReportTests(unittest.TestCase):
    def test_complete_report_passes(self):
        report = {
            "schema": {
                "latest_migration": "064_automatic_embedding_generation_refresh.sql"
            },
            "empty_workspace_lifecycle": {
                "cutover": ["retired", "active"],
                "rollback": ["active", "rolled_back"],
                "stale": ["active", "stale"],
                "forged_report_rejected": True,
                "automatic_refresh": True,
            },
            "nonempty_workspace_lifecycle": {
                "skipped": False,
                "wrong_owner_rejected": True,
                "coverage_complete": True,
                "sealed": True,
                "activated": True,
                "post_activation_insert_rejected": True,
                "serving_query_uses_hnsw": True,
                "served_generation_bound": True,
                "serving_membership_complete": True,
                "cutover_serving_counts": [0, 2],
                "rollback_serving_counts": [2, 0],
                "active_baseline_retained_after_corpus_change": True,
                "automatic_refresh_reused_all_vectors": True,
                "automatic_refresh_chunk_count": 3,
                "automatic_refresh_provider_candidates": 0,
                "automatic_refresh_activated": True,
                "automatic_refresh_cutover_counts": [0, 3],
                "manual_generation_required_operator_activation": True,
                "automatic_refresh_waited_for_processing": True,
                "chunk_count": 2,
                "worker_dispatch_batches": 1,
                "attempt_ceiling_failed_generation": True,
                "integrity_rejected_zero_vectors": True,
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
                "stale": ["stale", "building"],
                "forged_report_rejected": False,
            },
            "nonempty_workspace_lifecycle": {
                "skipped": False,
                "wrong_owner_rejected": False,
                "coverage_complete": False,
                "sealed": False,
                "activated": False,
                "post_activation_insert_rejected": False,
                "serving_query_uses_hnsw": False,
                "served_generation_bound": False,
                "serving_membership_complete": False,
                "cutover_serving_counts": [1, 1],
                "rollback_serving_counts": [0, 2],
                "active_baseline_retained_after_corpus_change": False,
                "chunk_count": 2,
                "integrity_rejected_zero_vectors": False,
            },
            "run": {"persistent_rows": 1, "provider_calls": 1},
        }
        failures = check_embedding_generation_report(report)
        self.assertEqual(len(failures), 28)

    def test_empty_database_may_skip_only_the_nonempty_probe(self):
        report = {
            "schema": {
                "latest_migration": "064_automatic_embedding_generation_refresh.sql"
            },
            "empty_workspace_lifecycle": {
                "cutover": ["retired", "active"],
                "rollback": ["active", "rolled_back"],
                "stale": ["active", "stale"],
                "forged_report_rejected": True,
                "automatic_refresh": True,
            },
            "nonempty_workspace_lifecycle": {"skipped": True},
            "run": {"persistent_rows": 0, "provider_calls": 0},
        }
        self.assertEqual(check_embedding_generation_report(report), [])


if __name__ == "__main__":
    unittest.main()
