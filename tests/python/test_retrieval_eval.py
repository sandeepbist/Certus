import copy
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from evals.retrieval import (
    EvaluationDataError,
    check_baseline,
    evaluate_dataset,
    load_dataset,
)
from services.shared.retrieval import reciprocal_rank_fusion


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
DATASET_DIRECTORY = REPOSITORY_ROOT / "evals/datasets/certus_seed_v1"
MANIFEST_PATH = DATASET_DIRECTORY / "manifest.json"
BASELINE_PATH = REPOSITORY_ROOT / "evals/baselines/certus_seed_v1.json"


class ReciprocalRankFusionTests(unittest.TestCase):
    def test_fuses_rankings_deterministically_without_duplicate_boosts(self):
        fused = reciprocal_rank_fusion([
            ["alpha", "beta", "beta"],
            ["beta", "gamma"],
        ])

        self.assertEqual([item_id for item_id, _ in fused], ["beta", "alpha", "gamma"])
        self.assertAlmostEqual(fused[0][1], (1 / 62) + (1 / 61))

    def test_rejects_a_nonpositive_rank_constant(self):
        with self.assertRaises(ValueError):
            reciprocal_rank_fusion([["alpha"]], rank_constant=0)


class RetrievalEvaluationContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dataset = load_dataset(MANIFEST_PATH)
        cls.report = evaluate_dataset(cls.dataset)

    def test_manifest_binds_documents_and_exact_evidence_spans(self):
        self.assertEqual(self.dataset.dataset_id, "certus-seed")
        self.assertEqual(len(self.dataset.documents), 14)
        self.assertEqual(len(self.dataset.queries), 11)
        self.assertTrue(any(not query.answerable for query in self.dataset.queries))
        self.assertTrue(any(
            query.expected_answer_status == "conflicting_evidence"
            for query in self.dataset.queries
        ))

        for query in self.dataset.queries:
            for evidence in query.evidence:
                document = next(
                    item
                    for item in self.dataset.documents
                    if item.document_id == evidence.document_id
                )
                self.assertEqual(
                    document.content[evidence.start_char:evidence.end_char],
                    evidence.quote,
                )

    def test_provider_free_report_has_replayable_rankings_and_metrics(self):
        hybrid = self.report["methods"]["hybrid_rrf"]["aggregate"]

        self.assertEqual(self.report["run"]["provider_calls"], 0)
        self.assertEqual(self.report["corpus"]["chunk_count"], 14)
        self.assertGreaterEqual(hybrid["answerable_evidence_recall_at_5"], 1.0)
        self.assertGreaterEqual(hybrid["answerable_mrr_at_20"], 0.9)
        self.assertEqual(hybrid["multi_evidence_complete_at_5"], 1.0)
        self.assertEqual(hybrid["conflicting_evidence_complete_at_5"], 1.0)
        self.assertIn("no_answer_empty_rate", hybrid)

        second = evaluate_dataset(self.dataset)
        self.assertEqual(self.report["run"]["run_fingerprint"], second["run"]["run_fingerprint"])
        self.assertEqual(self.report["methods"], second["methods"])
        self.assertEqual(
            [query["methods"] for query in self.report["queries"]],
            [query["methods"] for query in second["queries"]],
        )

    def test_committed_baseline_gates_the_corpus_and_runner_identity(self):
        self.assertEqual(check_baseline(self.report, BASELINE_PATH), [])

        regressed = copy.deepcopy(self.report)
        regressed["methods"]["hybrid_rrf"]["aggregate"][
            "answerable_evidence_recall_at_5"
        ] = 0.5
        failures = check_baseline(regressed, BASELINE_PATH)
        self.assertEqual(len(failures), 1)
        self.assertIn("answerable_evidence_recall_at_5", failures[0])

        changed = copy.deepcopy(self.report)
        changed["run"]["run_fingerprint"] = "0" * 64
        with self.assertRaises(EvaluationDataError):
            check_baseline(changed, BASELINE_PATH)

    def test_schema_v1_manifests_remain_loadable(self):
        with tempfile.TemporaryDirectory(prefix="certus-eval-v1-") as temp_dir:
            copied_dataset = Path(temp_dir) / "certus_seed_v1"
            shutil.copytree(DATASET_DIRECTORY, copied_dataset)
            manifest_path = copied_dataset / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["schema_version"] = 1
            for query in manifest["queries"]:
                expected_status = query.pop("expected_answer_status")
                query["answerable"] = expected_status != "insufficient_evidence"
            manifest_path.write_text(
                json.dumps(manifest, indent=2) + "\n",
                encoding="utf-8",
            )

            dataset = load_dataset(manifest_path)

            self.assertEqual(len(dataset.queries), 11)
            self.assertTrue(any(not query.answerable for query in dataset.queries))
            self.assertFalse(any(
                query.expected_answer_status == "conflicting_evidence"
                for query in dataset.queries
            ))

    def test_conflicting_status_requires_competing_evidence(self):
        with tempfile.TemporaryDirectory(prefix="certus-eval-conflict-") as temp_dir:
            copied_dataset = Path(temp_dir) / "certus_seed_v1"
            shutil.copytree(DATASET_DIRECTORY, copied_dataset)
            manifest_path = copied_dataset / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            conflict = next(
                query
                for query in manifest["queries"]
                if query["expected_answer_status"] == "conflicting_evidence"
            )
            conflict["evidence"] = conflict["evidence"][:1]
            manifest_path.write_text(
                json.dumps(manifest, indent=2) + "\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                EvaluationDataError,
                "conflicting queries require at least two evidence spans",
            ):
                load_dataset(manifest_path)

    def test_tampered_source_fails_before_evaluation(self):
        with tempfile.TemporaryDirectory(prefix="certus-eval-contract-") as temp_dir:
            copied_dataset = Path(temp_dir) / "certus_seed_v1"
            shutil.copytree(DATASET_DIRECTORY, copied_dataset)
            source = copied_dataset / "corpus/borealis_2023.md"
            source.write_text(
                source.read_text(encoding="utf-8") + "\nTampered.\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(EvaluationDataError, "Checksum mismatch"):
                load_dataset(copied_dataset / "manifest.json")


if __name__ == "__main__":
    unittest.main()
