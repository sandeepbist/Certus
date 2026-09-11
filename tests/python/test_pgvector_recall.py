import unittest
from datetime import datetime, timezone

from services.shared.document_retrieval import (
    build_active_embedding_generation_query,
    build_document_lexical_queries,
    build_document_semantic_query,
)
from services.shared.embeddings import LOCAL_EMBEDDING_PROFILE
from evals.pgvector_recall import (
    check_pgvector_report,
    recall_at_k,
    result_is_complete,
)
from services.shared.pgvector_policy import (
    HNSW_EF_SEARCH,
    HNSW_MAX_SCAN_TUPLES,
    HNSW_SCAN_MEM_MULTIPLIER,
    configure_filtered_hnsw,
)


class PgvectorRecallMetricTests(unittest.TestCase):
    def test_production_query_keeps_authorization_inside_and_distance_outside(self):
        query = build_document_semantic_query(
            vector_literal="[1,0]",
            tenant_id="tenant",
            user_id="user",
            embedding_profile=LOCAL_EMBEDDING_PROFILE.identifier,
            minimum_similarity=0.3,
            document_ids=("11111111-1111-4111-8111-111111111111",),
            years=(2024,),
        )

        cte, outer = query.sql.split(")\n        SELECT", maxsplit=1)
        self.assertIn("c.tenant_id = %s", cte)
        self.assertIn("c.document_id = ANY(%s::uuid[])", cte)
        self.assertIn("EXTRACT(YEAR FROM COALESCE", cte)
        self.assertIn(
            "embedding_profile = "
            "'embedding-space:v1:local:local-lexical-v2:1536'",
            cte,
        )
        self.assertNotIn(LOCAL_EMBEDDING_PROFILE.identifier, query.params)
        self.assertIn([2024], query.params)
        self.assertNotIn("vector_distance <=", cte)
        self.assertIn("WHERE vector_distance <= %s", outer)
        self.assertEqual(query.params[-2:], (20, 0.7))

        with self.assertRaises(ValueError):
            build_document_semantic_query(
                vector_literal="[1,0]",
                tenant_id="tenant",
                user_id="user",
                embedding_profile=LOCAL_EMBEDDING_PROFILE.identifier,
                minimum_similarity=float("nan"),
            )
        with self.assertRaises(ValueError):
            build_document_semantic_query(
                vector_literal="[1,0]",
                tenant_id="tenant",
                user_id="user",
                embedding_profile="embedding-space:v1:local:x:1536' OR TRUE --",
                minimum_similarity=0.3,
            )
        recorded = build_document_semantic_query(
            vector_literal="[1,0]", tenant_id="tenant", user_id="user",
            embedding_profile=LOCAL_EMBEDDING_PROFILE.identifier, minimum_similarity=0.3,
            years=(2026,), temporal_authority="recorded",
        )
        self.assertIn("EXTRACT(YEAR FROM version.recorded_at)", recorded.sql)
        self.assertNotIn("COALESCE(version.source_time", recorded.sql)
        current = build_document_semantic_query(
            vector_literal="[1,0]", tenant_id="tenant", user_id="user",
            embedding_profile=LOCAL_EMBEDDING_PROFILE.identifier, minimum_similarity=0.3,
            version_scope="current",
        )
        self.assertIn("d.current_version_id = version.id", current.sql)
        lexical = build_document_lexical_queries(
            query="current value",
            tenant_id="tenant",
            user_id="user",
            version_scope="current",
        )
        self.assertIn("d.current_version_id = version.id", lexical.full_text.sql)
        self.assertIn(
            "d.current_version_id = version.id",
            lexical.literal_phrase.sql,
        )
        as_of = build_document_semantic_query(
            vector_literal="[1,0]",
            tenant_id="tenant",
            user_id="user",
            embedding_profile=LOCAL_EMBEDDING_PROFILE.identifier,
            minimum_similarity=0.3,
            years=(2024,),
            temporal_authority="source",
            version_scope="as_of",
        )
        self.assertIn("SELECT candidate.id", as_of.sql)
        self.assertIn("candidate.source_time < %s::timestamptz", as_of.sql)
        self.assertIn("candidate.version_number DESC", as_of.sql)
        self.assertNotIn("EXTRACT(YEAR FROM version.source_time)", as_of.sql)
        with self.assertRaises(ValueError):
            build_document_semantic_query(
                vector_literal="[1,0]",
                tenant_id="tenant",
                user_id="user",
                embedding_profile=LOCAL_EMBEDDING_PROFILE.identifier,
                minimum_similarity=0.3,
                version_scope="as_of",
            )
        bounded = build_document_semantic_query(
            vector_literal="[1,0]",
            tenant_id="tenant",
            user_id="user",
            embedding_profile=LOCAL_EMBEDDING_PROFILE.identifier,
            minimum_similarity=0.3,
            year_start=2024,
            year_end=2025,
            temporal_authority="recorded",
        )
        self.assertIn("version.recorded_at >= %s::timestamptz", bounded.sql)
        self.assertIn("version.recorded_at < %s::timestamptz", bounded.sql)
        self.assertNotIn("EXTRACT(YEAR FROM version.recorded_at)", bounded.sql)
        cutoffs = [value for value in bounded.params if hasattr(value, "year")]
        self.assertEqual([value.year for value in cutoffs], [2024, 2026])
        bounded_lexical = build_document_lexical_queries(
            query="historical value",
            tenant_id="tenant",
            user_id="user",
            year_start=2025,
            temporal_authority="source",
        )
        for query in (bounded_lexical.full_text, bounded_lexical.literal_phrase):
            self.assertIn("version.source_time >= %s::timestamptz", query.sql)
        with self.assertRaises(ValueError):
            build_document_semantic_query(
                vector_literal="[1,0]",
                tenant_id="tenant",
                user_id="user",
                embedding_profile=LOCAL_EMBEDDING_PROFILE.identifier,
                minimum_similarity=0.3,
                years=(2024,),
                year_start=2024,
            )
        day_start = datetime(2024, 6, 1, tzinfo=timezone.utc)
        day_end = datetime(2024, 6, 2, tzinfo=timezone.utc)
        dated = build_document_semantic_query(
            vector_literal="[1,0]",
            tenant_id="tenant",
            user_id="user",
            embedding_profile=LOCAL_EMBEDDING_PROFILE.identifier,
            minimum_similarity=0.3,
            time_start=day_start,
            time_end=day_end,
            temporal_authority="source",
        )
        self.assertIn("version.source_time >= %s::timestamptz", dated.sql)
        self.assertIn("version.source_time < %s::timestamptz", dated.sql)
        self.assertIn(day_start, dated.params)
        self.assertIn(day_end, dated.params)
        dated_as_of = build_document_semantic_query(
            vector_literal="[1,0]",
            tenant_id="tenant",
            user_id="user",
            embedding_profile=LOCAL_EMBEDDING_PROFILE.identifier,
            minimum_similarity=0.3,
            time_end=day_end,
            temporal_authority="source",
            version_scope="as_of",
        )
        self.assertIn("candidate.source_time < %s::timestamptz", dated_as_of.sql)
        self.assertIn(day_end, dated_as_of.params)
        self.assertNotIn("EXTRACT(YEAR", dated_as_of.sql)

    def test_active_generation_query_uses_only_serving_shadow_vectors(self):
        generation_id = "11111111-1111-4111-8111-111111111111"
        lookup = build_active_embedding_generation_query(
            tenant_id="tenant",
            user_id="user",
            embedding_profile=LOCAL_EMBEDDING_PROFILE.identifier,
        )
        self.assertIn("status = 'active'", lookup.sql)
        self.assertIn(
            "embedding_profile = "
            "'embedding-space:v1:local:local-lexical-v2:1536'",
            lookup.sql,
        )
        self.assertIn("FOR SHARE", lookup.sql)
        self.assertEqual(lookup.params, ("tenant", "user"))

        query = build_document_semantic_query(
            vector_literal="[1,0]",
            tenant_id="tenant",
            user_id="user",
            embedding_profile=LOCAL_EMBEDDING_PROFILE.identifier,
            embedding_generation_id=generation_id,
            minimum_similarity=0.3,
        )
        cte, outer = query.sql.split(")\n        SELECT", maxsplit=1)
        self.assertIn("FROM chunk_embedding_vectors AS candidate_vector", cte)
        self.assertIn("embedding_generation.status = 'active'", cte)
        self.assertIn("candidate_vector.status = 'embedded'", cte)
        self.assertIn("candidate_vector.is_serving = true", cte)
        self.assertIn(
            "candidate_vector.embedding_profile = "
            "'embedding-space:v1:local:local-lexical-v2:1536'",
            cte,
        )
        self.assertIn("candidate_vector.embedding <=> %s::vector", cte)
        self.assertIn("delta_nearest AS MATERIALIZED", cte)
        self.assertIn("FROM chunks c", cte)
        self.assertIn("snapshot_member.generation_id = %s::uuid", cte)
        self.assertIn("UNION ALL", cte)
        self.assertIn(generation_id, query.params)
        self.assertIn("embedding_generation_id", outer)

        with self.assertRaises(ValueError):
            build_document_semantic_query(
                vector_literal="[1,0]",
                tenant_id="tenant",
                user_id="user",
                embedding_profile=LOCAL_EMBEDDING_PROFILE.identifier,
                embedding_generation_id="not-a-uuid",
                minimum_similarity=0.3,
            )

    def test_runtime_and_evaluator_share_one_bounded_hnsw_policy(self):
        class Cursor:
            def __init__(self):
                self.statements = []

            def execute(self, statement, params=None):
                self.statements.append((statement, params))

        cursor = Cursor()
        configure_filtered_hnsw(cursor)

        self.assertIn("strict_order", cursor.statements[0][0])
        self.assertEqual(cursor.statements[1][1], (str(HNSW_EF_SEARCH),))
        self.assertEqual(cursor.statements[2][1], (str(HNSW_MAX_SCAN_TUPLES),))
        self.assertEqual(cursor.statements[3][1], (str(HNSW_SCAN_MEM_MULTIPLIER),))
    def test_recall_uses_the_exact_top_k_as_oracle(self):
        self.assertEqual(recall_at_k([1, 2, 3], [1, 3, 9], 3), 2 / 3)
        self.assertEqual(recall_at_k([], [], 5), 1.0)
        with self.assertRaises(ValueError):
            recall_at_k([1], [1], 0)

    def test_completeness_accounts_for_small_eligible_sets(self):
        self.assertTrue(result_is_complete([1, 2], eligible_count=2, requested_count=5))
        self.assertFalse(result_is_complete([1], eligible_count=2, requested_count=5))
        with self.assertRaises(ValueError):
            result_is_complete([], eligible_count=-1, requested_count=5)

    def test_report_gate_names_recall_and_completeness_failures(self):
        report = {
            "cases": [
                {
                    "case_id": "weak", "recall_at_20": 0.95,
                    "complete": False, "eligible_count": 20,
                    "ann_plan": {"uses_hnsw": True},
                },
                {
                    "case_id": "strong", "recall_at_20": 1.0,
                    "complete": True, "eligible_count": 20,
                    "ann_plan": {"uses_hnsw": True},
                },
            ],
            "production_query_cases": [{
                "case_id": "joined-strong", "recall_at_20": 1.0,
                "complete": True, "eligible_count": 20,
                "ann_plan": {"uses_hnsw": True},
            }],
            "lexical_query_cases": [{
                "case_id": "fts-strong", "scope_correct": True,
                "fts_plan": {"uses_gin": True},
            }],
        }

        failures = check_pgvector_report(report)

        self.assertEqual(len(failures), 2)
        self.assertTrue(all("weak" in failure for failure in failures))

    def test_report_gate_rejects_a_non_hnsw_or_empty_case(self):
        failures = check_pgvector_report({
            "cases": [{
                "case_id": "planner-regression",
                "recall_at_20": 1.0,
                "complete": True,
                "eligible_count": 0,
                "ann_plan": {"uses_hnsw": False},
            }],
            "production_query_cases": [{
                "case_id": "joined-strong", "recall_at_20": 1.0,
                "complete": True, "eligible_count": 20,
                "ann_plan": {"uses_hnsw": True},
            }],
            "lexical_query_cases": [{
                "case_id": "fts-strong", "scope_correct": True,
                "fts_plan": {"uses_gin": True},
            }],
        })

        self.assertEqual(len(failures), 2)
        self.assertTrue(any("HNSW" in failure for failure in failures))
        self.assertTrue(any("eligible" in failure for failure in failures))

    def test_report_gate_requires_the_joined_production_query(self):
        failures = check_pgvector_report({
            "cases": [{
                "case_id": "flat-strong", "recall_at_20": 1.0,
                "complete": True, "eligible_count": 20,
                "ann_plan": {"uses_hnsw": True},
            }],
        })

        self.assertEqual(
            failures,
            [
                "No joined production-query cases were executed",
                "No joined lexical-query cases were executed",
            ],
        )

    def test_schema_v3_report_requires_and_gates_production_rrf_corpus(self):
        report = {
            "schema_version": 3,
            "cases": [{
                "case_id": "flat", "recall_at_20": 1.0, "complete": True,
                "eligible_count": 20, "ann_plan": {"uses_hnsw": True},
            }],
            "production_query_cases": [{
                "case_id": "joined", "recall_at_20": 1.0, "complete": True,
                "eligible_count": 20, "ann_plan": {"uses_hnsw": True},
            }],
            "lexical_query_cases": [{
                "case_id": "fts", "scope_correct": True,
                "fts_plan": {"uses_gin": True},
            }],
            "seed_corpus": {
                "evidence_recall_at_5": 0.9,
                "mrr_at_20": 0.8,
                "conflict_complete_at_5": 0.0,
            },
        }

        failures = check_pgvector_report(report)

        self.assertEqual(len(failures), 3)
        self.assertTrue(any("Recall@5" in failure for failure in failures))
        self.assertTrue(any("MRR@20" in failure for failure in failures))
        self.assertTrue(any("conflict" in failure for failure in failures))

    def test_schema_v4_report_requires_source_and_recorded_time_cases(self):
        report = {
            "schema_version": 4,
            "cases": [{
                "case_id": "flat", "recall_at_20": 1.0, "complete": True,
                "eligible_count": 20, "ann_plan": {"uses_hnsw": True},
            }],
            "production_query_cases": [{
                "case_id": "joined", "recall_at_20": 1.0, "complete": True,
                "eligible_count": 20, "ann_plan": {"uses_hnsw": True},
            }],
            "lexical_query_cases": [{
                "case_id": "fts", "scope_correct": True,
                "fts_plan": {"uses_gin": True},
            }],
            "seed_corpus": {
                "evidence_recall_at_5": 1.0, "mrr_at_20": 0.95,
                "conflict_complete_at_5": 1.0,
            },
        }

        failures = check_pgvector_report(report)

        self.assertEqual(
            failures,
            ["Joined production queries did not cover both temporal authorities"],
        )

    def test_schema_v5_report_requires_explicit_recorded_authority(self):
        report = {
            "schema_version": 5,
            "cases": [{"case_id": "flat", "recall_at_20": 1.0,
                "complete": True, "eligible_count": 20,
                "ann_plan": {"uses_hnsw": True}}],
            "production_query_cases": [
                {"case_id": case_id, "recall_at_20": 1.0, "complete": True,
                 "eligible_count": 1, "ann_plan": {"uses_hnsw": True}}
                for case_id in (
                    "target-source-year-2024",
                    "target-recorded-year-fallback-2026",
                )
            ],
            "lexical_query_cases": [{"case_id": "fts", "scope_correct": True,
                "fts_plan": {"uses_gin": True}}],
            "seed_corpus": {"evidence_recall_at_5": 1.0, "mrr_at_20": 0.95,
                "conflict_complete_at_5": 1.0},
        }

        self.assertIn(
            "Joined production queries did not prove recorded-time authority",
            check_pgvector_report(report),
        )

    def test_schema_v6_report_requires_current_version_exclusion(self):
        report = {
            "schema_version": 6,
            "cases": [{"case_id": "flat", "recall_at_20": 1.0,
                "complete": True, "eligible_count": 20,
                "ann_plan": {"uses_hnsw": True}}],
            "production_query_cases": [
                {"case_id": case_id, "recall_at_20": 1.0, "complete": True,
                 "eligible_count": 1, "ann_plan": {"uses_hnsw": True},
                 "temporal_authority": authority}
                for case_id, authority in (
                    ("target-source-year-2024", "source"),
                    ("target-recorded-year-fallback-2026", "effective"),
                    ("target-recorded-authority-2026", "recorded"),
                )
            ],
            "lexical_query_cases": [{"case_id": "fts", "scope_correct": True,
                "fts_plan": {"uses_gin": True}}],
            "seed_corpus": {"evidence_recall_at_5": 1.0, "mrr_at_20": 0.95,
                "conflict_complete_at_5": 1.0},
        }

        self.assertIn(
            "Joined production queries did not prove current-version scope",
            check_pgvector_report(report),
        )

    def test_schema_v7_report_requires_both_as_of_authorities(self):
        report = {
            "schema_version": 7,
            "cases": [{"case_id": "flat", "recall_at_20": 1.0,
                "complete": True, "eligible_count": 20,
                "ann_plan": {"uses_hnsw": True}}],
            "production_query_cases": [
                {"case_id": case_id, "recall_at_20": 1.0, "complete": True,
                 "eligible_count": 1, "ann_plan": {"uses_hnsw": True},
                 "temporal_authority": authority}
                for case_id, authority in (
                    ("target-source-year-2024", "source"),
                    ("target-recorded-year-fallback-2026", "effective"),
                    ("target-recorded-authority-2026", "recorded"),
                    ("target-current-version-only", "effective"),
                )
            ],
            "lexical_query_cases": [{"case_id": "fts", "scope_correct": True,
                "fts_plan": {"uses_gin": True}}],
            "seed_corpus": {"evidence_recall_at_5": 1.0, "mrr_at_20": 0.95,
                "conflict_complete_at_5": 1.0},
        }

        failures = check_pgvector_report(report)
        self.assertIn(
            "Joined production queries did not prove source-time as-of scope",
            failures,
        )
        self.assertIn(
            "Joined production queries did not prove recorded-time as-of scope",
            failures,
        )

    def test_schema_v8_report_requires_all_bounded_year_operators(self):
        report = {
            "schema_version": 8,
            "cases": [{"case_id": "flat", "recall_at_20": 1.0,
                "complete": True, "eligible_count": 20,
                "ann_plan": {"uses_hnsw": True}}],
            "production_query_cases": [
                {"case_id": case_id, "recall_at_20": 1.0, "complete": True,
                 "eligible_count": 1, "ann_plan": {"uses_hnsw": True},
                 "temporal_authority": authority}
                for case_id, authority in (
                    ("target-source-year-2024", "source"),
                    ("target-recorded-year-fallback-2026", "effective"),
                    ("target-recorded-authority-2026", "recorded"),
                    ("target-current-version-only", "effective"),
                )
            ],
            "lexical_query_cases": [{"case_id": "fts", "scope_correct": True,
                "fts_plan": {"uses_gin": True}}],
            "seed_corpus": {"evidence_recall_at_5": 1.0, "mrr_at_20": 0.95,
                "conflict_complete_at_5": 1.0},
        }

        failures = check_pgvector_report(report)
        for case_id in (
            "target-source-before-2025",
            "target-source-after-2024",
            "target-source-between-2024-2025",
        ):
            self.assertIn(
                f"Joined production queries did not prove bounded year scope: {case_id}",
                failures,
            )

    def test_schema_v9_report_requires_all_iso_date_operators(self):
        report = {
            "schema_version": 9,
            "cases": [{"case_id": "flat", "recall_at_20": 1.0,
                "complete": True, "eligible_count": 20,
                "ann_plan": {"uses_hnsw": True}}],
            "production_query_cases": [
                {"case_id": case_id, "recall_at_20": 1.0, "complete": True,
                 "eligible_count": 1, "ann_plan": {"uses_hnsw": True},
                 "temporal_authority": authority}
                for case_id, authority in (
                    ("target-source-year-2024", "source"),
                    ("target-recorded-year-fallback-2026", "effective"),
                    ("target-recorded-authority-2026", "recorded"),
                    ("target-current-version-only", "effective"),
                )
            ],
            "lexical_query_cases": [{"case_id": "fts", "scope_correct": True,
                "fts_plan": {"uses_gin": True}}],
            "seed_corpus": {"evidence_recall_at_5": 1.0, "mrr_at_20": 0.95,
                "conflict_complete_at_5": 1.0},
        }

        failures = check_pgvector_report(report)
        for case_id in (
            "target-source-on-2024-06-01",
            "target-source-after-2024-06-01",
            "target-source-between-dates",
            "target-source-as-of-2024-06-01",
        ):
            self.assertIn(
                f"Joined production queries did not prove ISO-date scope: {case_id}",
                failures,
            )

    def test_schema_v10_report_requires_exact_title_scope(self):
        report = {
            "schema_version": 10,
            "cases": [{"case_id": "flat", "recall_at_20": 1.0,
                "complete": True, "eligible_count": 20,
                "ann_plan": {"uses_hnsw": True}}],
            "production_query_cases": [
                {"case_id": case_id, "recall_at_20": 1.0, "complete": True,
                 "eligible_count": 1, "ann_plan": {"uses_hnsw": True},
                 "temporal_authority": authority}
                for case_id, authority in (
                    ("target-source-year-2024", "source"),
                    ("target-recorded-year-fallback-2026", "effective"),
                    ("target-recorded-authority-2026", "recorded"),
                    ("target-current-version-only", "effective"),
                )
            ],
            "lexical_query_cases": [{"case_id": "fts", "scope_correct": True,
                "fts_plan": {"uses_gin": True}}],
            "seed_corpus": {"evidence_recall_at_5": 1.0, "mrr_at_20": 0.95,
                "conflict_complete_at_5": 1.0},
        }

        self.assertIn(
            "Joined production queries did not prove exact title scope",
            check_pgvector_report(report),
        )

    def test_schema_v11_report_requires_all_exact_instant_operators(self):
        report = {
            "schema_version": 11,
            "cases": [{"case_id": "flat", "recall_at_20": 1.0,
                "complete": True, "eligible_count": 20,
                "ann_plan": {"uses_hnsw": True}}],
            "production_query_cases": [
                {"case_id": case_id, "recall_at_20": 1.0, "complete": True,
                 "eligible_count": 1, "ann_plan": {"uses_hnsw": True},
                 "temporal_authority": authority}
                for case_id, authority in (
                    ("target-source-year-2024", "source"),
                    ("target-recorded-year-fallback-2026", "effective"),
                    ("target-recorded-authority-2026", "recorded"),
                    ("target-current-version-only", "effective"),
                )
            ],
            "lexical_query_cases": [{"case_id": "fts", "scope_correct": True,
                "fts_plan": {"uses_gin": True}}],
            "seed_corpus": {"evidence_recall_at_5": 1.0, "mrr_at_20": 0.95,
                "conflict_complete_at_5": 1.0},
        }

        failures = check_pgvector_report(report)
        for case_id in (
            "target-source-at-offset-instant",
            "target-source-after-instant",
            "target-source-between-instants",
            "target-source-as-of-instant",
        ):
            self.assertIn(
                f"Joined production queries did not prove exact instant scope: {case_id}",
                failures,
            )

    def test_schema_v13_report_requires_generation_bound_serving(self):
        report = {
            "schema_version": 13,
            "run": {
                "hnsw": {
                    "profile_isolation": "partial_hnsw_per_supported_profile",
                    "generation_serving": "inactive_vectors_included",
                }
            },
            "cases": [{
                "case_id": "flat",
                "recall_at_20": 1.0,
                "complete": True,
                "eligible_count": 20,
                "ann_plan": {"uses_hnsw": True},
            }],
            "production_query_cases": [{
                "case_id": "ordinary",
                "recall_at_20": 1.0,
                "complete": True,
                "eligible_count": 20,
                "ann_plan": {"uses_hnsw": True},
            }],
            "generation_query_cases": [{
                "case_id": "target-active-embedding-generation",
                "recall_at_20": 1.0,
                "complete": True,
                "eligible_count": 20,
                "ann_plan": {"uses_hnsw": False, "bounded_exact": False},
                "serving_source": "active_embedding_generation",
                "generation_bound": False,
            }],
            "lexical_query_cases": [{
                "case_id": "fts",
                "scope_correct": True,
                "fts_plan": {"uses_gin": True},
            }],
            "seed_corpus": {
                "evidence_recall_at_5": 1.0,
                "mrr_at_20": 0.95,
                "conflict_complete_at_5": 1.0,
            },
        }

        failures = check_pgvector_report(report)
        self.assertIn(
            "Generation query did not prove active-baseline plus delta serving",
            failures,
        )
        self.assertIn(
            "Active-generation retrieval used neither HNSW nor bounded exact search",
            failures,
        )
        self.assertIn(
            "Production HNSW generation serving was not declared",
            failures,
        )


if __name__ == "__main__":
    unittest.main()
