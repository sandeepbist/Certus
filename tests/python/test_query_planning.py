import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
for module_name in list(sys.modules):
    if module_name == "app" or module_name.startswith("app."):
        del sys.modules[module_name]
services_root = REPOSITORY_ROOT / "services"
original_sys_path = list(sys.path)
sys.path = [
    str(services_root / "orchestration"),
    *[
        entry
        for entry in original_sys_path
        if Path(entry or ".").resolve().parent != services_root
    ],
]

from app.query_planning import (
    QUERY_PLAN_PROFILE,
    QueryPlan,
    build_query_plan,
    validate_query_plan,
)
from app.conversation import build_conversation_context
from app.retrieval import hybrid as hybrid_module
from app.retrieval import memory as memory_module

sys.path = original_sys_path
for module_name in list(sys.modules):
    if module_name == "app" or module_name.startswith("app."):
        del sys.modules[module_name]


def make_plan(query: str, tool_calls=()):
    return build_query_plan(
        query,
        tool_calls,
        selected_model="local-extractive",
        is_complex=False,
    )


class QueryPlanningTests(unittest.TestCase):
    def test_short_follow_up_uses_only_the_latest_user_question_for_retrieval(self):
        context = build_conversation_context([
            {
                "id": "older-run",
                "input_query": "Explain the unrelated hiring plan",
                "output_response": "Ignore this assistant output.",
                "answer_status": "answered",
            },
            {
                "id": "latest-run",
                "input_query": "What was the Atlas revenue in 2023?",
                "output_response": "The assistant claimed a value that must not be reused.",
                "answer_status": "answered",
            },
        ])
        plan = build_query_plan(
            "What about 2024?",
            [],
            selected_model="local-extractive",
            is_complex=False,
            conversation_context=context,
        )

        self.assertTrue(plan.original_query_preserved)
        self.assertTrue(plan.conversation_resolution.applied)
        self.assertEqual(
            plan.conversation_resolution.strategy,
            "contiguous_user_question_chain",
        )
        self.assertEqual(
            plan.conversation_resolution.source_run_ids,
            ["latest-run"],
        )
        self.assertEqual(len(plan.conversation_resolution.source_question_sha256s), 1)
        self.assertIn("Atlas revenue", plan.retrieval_query)
        self.assertIn("2024", plan.retrieval_query)
        self.assertNotIn("2023", plan.retrieval_query)
        self.assertNotIn("What about", plan.retrieval_query)
        self.assertNotIn("assistant claimed", plan.retrieval_query)
        self.assertNotIn("hiring plan", plan.retrieval_query)
        self.assertEqual(plan.detected_constraints.years, ["2024"])
        self.assertIn(
            "resolve_latest_user_reference:v1",
            plan.deterministic_transformations,
        )
        self.assertEqual(plan.synthetic_rewrites, [])

    def test_follow_up_inherits_only_explicit_narrowing_scope(self):
        document_id = "11111111-1111-4111-8111-111111111111"
        context = build_conversation_context([{
            "id": "prior-run",
            "input_query": (
                f'Find margin in document ID {document_id} and '
                'document titled "Atlas Ledger" from 2023'
            ),
            "output_response": "Prior answer",
            "answer_status": "answered",
        }])
        plan = build_query_plan(
            "And what changed in 2024?",
            [],
            selected_model="local-extractive",
            is_complex=False,
            conversation_context=context,
        )

        self.assertEqual(plan.detected_constraints.document_ids, [document_id])
        self.assertEqual(
            plan.detected_constraints.conversation_inherited_document_ids,
            [document_id],
        )
        self.assertEqual(plan.detected_constraints.titles, ["Atlas Ledger"])
        self.assertEqual(
            plan.detected_constraints.conversation_inherited_titles,
            ["Atlas Ledger"],
        )
        self.assertEqual(
            plan.detected_constraints.document_scope_source,
            "conversation_reference",
        )
        self.assertEqual(
            plan.conversation_resolution.inherited_constraints,
            ["document_ids", "titles"],
        )
        self.assertFalse(plan.conversation_resolution.permission_scope_broadened)

    def test_product_selection_overrides_conversation_scope_and_standalone_turns_do_not_rewrite(self):
        inherited_id = "11111111-1111-4111-8111-111111111111"
        selected_id = "22222222-2222-4222-8222-222222222222"
        context = build_conversation_context([{
            "id": "prior-run",
            "input_query": f"Find margin in document ID {inherited_id}",
            "output_response": "Prior answer",
            "answer_status": "answered",
        }])
        selected = build_query_plan(
            "What about its margin?",
            [],
            selected_model="local-extractive",
            is_complex=False,
            selected_document_ids=[selected_id],
            conversation_context=context,
        )
        standalone = build_query_plan(
            "Find the 2024 Atlas margin in the annual report",
            [],
            selected_model="local-extractive",
            is_complex=False,
            conversation_context=context,
        )

        self.assertEqual(selected.detected_constraints.document_ids, [selected_id])
        self.assertEqual(
            selected.detected_constraints.conversation_inherited_document_ids,
            [],
        )
        self.assertEqual(
            selected.detected_constraints.document_scope_source,
            "product_selection",
        )
        self.assertFalse(standalone.conversation_resolution.applied)
        self.assertEqual(
            standalone.retrieval_query,
            "Find the 2024 Atlas margin in the annual report",
        )

    def test_explicit_current_document_scope_does_not_mix_with_inherited_scope(self):
        inherited_id = "11111111-1111-4111-8111-111111111111"
        current_id = "22222222-2222-4222-8222-222222222222"
        context = build_conversation_context([{
            "id": "prior-run",
            "input_query": (
                f'Find margin in document ID {inherited_id} and '
                'document titled "Atlas Ledger"'
            ),
            "output_response": "Prior answer",
            "answer_status": "answered",
        }])

        plan = build_query_plan(
            f"And what about document ID {current_id}?",
            [],
            selected_model="local-extractive",
            is_complex=False,
            conversation_context=context,
        )

        self.assertEqual(plan.detected_constraints.document_ids, [current_id])
        self.assertEqual(
            plan.detected_constraints.conversation_inherited_document_ids,
            [],
        )
        self.assertEqual(plan.detected_constraints.titles, [])
        self.assertEqual(
            plan.detected_constraints.conversation_inherited_titles,
            [],
        )
        self.assertEqual(
            plan.detected_constraints.document_scope_source,
            "query_label",
        )
        self.assertEqual(plan.conversation_resolution.inherited_constraints, [])

    def test_common_relative_and_expletive_words_do_not_trigger_a_rewrite(self):
        context = build_conversation_context([{
            "id": "prior-run",
            "input_query": "Explain the unrelated hiring plan",
            "output_response": "Prior answer",
            "answer_status": "answered",
        }])

        for query in (
            "Show documents that discuss Atlas revenue",
            "Are there Atlas revenue reports?",
        ):
            with self.subTest(query=query):
                plan = build_query_plan(
                    query,
                    [],
                    selected_model="local-extractive",
                    is_complex=False,
                    conversation_context=context,
                )
                self.assertFalse(plan.conversation_resolution.applied)
                self.assertEqual(plan.retrieval_query, query)

    def test_anchored_anaphoric_question_resolves_without_a_prefix(self):
        context = build_conversation_context([{
            "id": "prior-run",
            "input_query": "Find the Atlas operating margin in 2023",
            "output_response": "Prior answer",
            "answer_status": "answered",
        }])

        plan = build_query_plan(
            "How did it change in 2024?",
            [],
            selected_model="local-extractive",
            is_complex=False,
            conversation_context=context,
        )

        self.assertTrue(plan.conversation_resolution.applied)
        self.assertEqual(
            plan.conversation_resolution.reference_signals,
            ["anaphoric_reference"],
        )
        self.assertIn("Atlas operating margin", plan.retrieval_query)
        self.assertIn("2024", plan.retrieval_query)
        self.assertNotIn("2023", plan.retrieval_query)

    def test_follow_up_chain_reaches_the_latest_self_contained_user_question(self):
        context = build_conversation_context([
            {
                "id": "base-run",
                "input_query": "Find the Atlas revenue in 2022",
                "output_response": "Prior answer",
                "answer_status": "answered",
            },
            {
                "id": "follow-up-run",
                "input_query": "What about operating margin in 2023?",
                "output_response": "Another prior answer",
                "answer_status": "answered",
            },
        ])

        plan = build_query_plan(
            "And how did it change in 2024?",
            [],
            selected_model="local-extractive",
            is_complex=False,
            conversation_context=context,
        )

        self.assertTrue(plan.conversation_resolution.applied)
        self.assertEqual(
            plan.conversation_resolution.source_run_ids,
            ["base-run", "follow-up-run"],
        )
        self.assertIn("Atlas revenue", plan.retrieval_query)
        self.assertIn("operating margin", plan.retrieval_query)
        self.assertIn("2024", plan.retrieval_query)
        self.assertNotIn("2022", plan.retrieval_query)
        self.assertNotIn("2023", plan.retrieval_query)
        self.assertNotIn("What about", plan.retrieval_query)

    def test_product_selection_is_authoritative_over_query_labeled_ids(self):
        selected_id = "22222222-2222-4222-8222-222222222222"
        mentioned_id = "11111111-1111-4111-8111-111111111111"
        plan = build_query_plan(
            f"Compare document ID {mentioned_id} with the archive",
            [],
            selected_model="local-extractive",
            is_complex=False,
            selected_document_ids=[selected_id, selected_id.upper()],
        )

        self.assertEqual(plan.detected_constraints.document_ids, [selected_id])
        self.assertEqual(
            plan.detected_constraints.query_document_ids,
            [mentioned_id],
        )
        self.assertEqual(
            plan.detected_constraints.product_selected_document_ids,
            [selected_id],
        )
        self.assertEqual(
            plan.detected_constraints.document_scope_source,
            "product_selection",
        )
        self.assertTrue(plan.enforcement.requested_document_id_filter_applied)
        self.assertTrue(
            plan.enforcement.product_selection_overrode_query_document_ids
        )

    def test_product_selection_is_bounded_and_requires_valid_uuids(self):
        with self.assertRaises(ValueError):
            build_query_plan(
                "Find evidence",
                [],
                selected_model="local-extractive",
                is_complex=False,
                selected_document_ids=["not-a-uuid"],
            )
        with self.assertRaises(ValueError):
            build_query_plan(
                "Find evidence",
                [],
                selected_model="local-extractive",
                is_complex=False,
                selected_document_ids=[
                    f"00000000-0000-4000-8000-{index:012d}"
                    for index in range(11)
                ],
            )

    def test_product_version_scope_is_authoritative_except_for_as_of_cutoffs(self):
        historical = build_query_plan(
            "Show the current value",
            [],
            selected_model="local-extractive",
            is_complex=False,
            selected_version_scope="all_history",
        )
        current = build_query_plan(
            "Compare values across the archive",
            [],
            selected_model="local-extractive",
            is_complex=False,
            selected_version_scope="current_only",
        )
        as_of = build_query_plan(
            "What was true as of 2024-06-01?",
            [],
            selected_model="local-extractive",
            is_complex=False,
            selected_version_scope="current_only",
        )

        self.assertEqual(
            historical.detected_constraints.query_requested_version_scope,
            "current_requested",
        )
        self.assertEqual(
            historical.detected_constraints.requested_version_scope,
            "all_history",
        )
        self.assertEqual(
            historical.detected_constraints.version_scope_source,
            "product_selection",
        )
        self.assertTrue(
            historical.enforcement.product_version_scope_overrode_query_scope
        )
        self.assertEqual(
            historical.enforcement.retrieval_version_scope,
            "all_retained_versions",
        )

        self.assertEqual(
            current.detected_constraints.requested_version_scope,
            "current_requested",
        )
        self.assertEqual(
            current.enforcement.retrieval_version_scope,
            "current_version_only",
        )
        self.assertTrue(current.enforcement.product_version_scope_overrode_query_scope)

        self.assertEqual(
            as_of.detected_constraints.requested_version_scope,
            "as_of_requested",
        )
        self.assertEqual(as_of.detected_constraints.version_scope_source, "query")
        self.assertEqual(as_of.enforcement.retrieval_version_scope, "as_of_version")

        automatic_comparison = make_plan(
            "Compare the current value with its historical value"
        )
        self.assertEqual(
            automatic_comparison.detected_constraints.requested_version_scope,
            "all_history",
        )
        self.assertEqual(
            automatic_comparison.detected_constraints.version_scope_source,
            "query",
        )
        self.assertEqual(
            automatic_comparison.enforcement.retrieval_version_scope,
            "all_retained_versions",
        )

    def test_product_version_scope_rejects_unknown_values(self):
        with self.assertRaises(ValueError):
            build_query_plan(
                "Find evidence",
                [],
                selected_model="local-extractive",
                is_complex=False,
                selected_version_scope="latest",  # type: ignore[arg-type]
            )

    def test_self_contained_action_skips_retrieval_and_embedding(self):
        plan = make_plan(
            "Create a task to review the archive tomorrow",
            [{"tool_name": "create_task", "arguments": {}}],
        )

        self.assertIsInstance(plan, QueryPlan)
        self.assertEqual(plan.profile, QUERY_PLAN_PROFILE)
        self.assertEqual(plan.intent, "action")
        self.assertEqual(
            plan.branches.model_dump(),
            {
                "documents": False,
                "memories": False,
                "graph": False,
                "tools": True,
                "query_embedding": False,
            },
        )

    def test_exact_historical_document_lookup_uses_only_documents(self):
        document_id = "11111111-1111-4111-8111-111111111111"
        plan = make_plan(
            f'What was the value "$42.50" in 2024 for document ID {document_id}?'
        )

        self.assertEqual(plan.intent, "temporal_lookup")
        self.assertTrue(plan.branches.documents)
        self.assertFalse(plan.branches.memories)
        self.assertFalse(plan.branches.graph)
        self.assertTrue(plan.branches.query_embedding)
        self.assertEqual(plan.detected_constraints.document_ids, [document_id])
        self.assertEqual(plan.detected_constraints.years, ["2024"])
        self.assertEqual(plan.detected_constraints.temporal_authority, "effective")
        self.assertIn("$42.50", plan.detected_constraints.quoted_phrases)
        self.assertEqual(
            plan.detected_constraints.requested_version_scope,
            "all_history",
        )
        self.assertEqual(
            plan.enforcement.retrieval_version_scope,
            "all_retained_versions",
        )
        self.assertEqual(plan.enforcement.temporal_filter_mode, "exact_years")
        self.assertTrue(plan.enforcement.requested_temporal_filter_applied)
        self.assertTrue(plan.enforcement.requested_document_id_filter_applied)
        self.assertNotIn(document_id, plan.retrieval_query)
        self.assertNotIn(" ?", plan.retrieval_query)
        self.assertEqual(
            plan.deterministic_transformations,
            ["remove_explicit_document_id_clause:v1"],
        )
        self.assertEqual(plan.synthetic_rewrites, [])

    def test_unlabeled_uuid_is_not_enforced_as_a_document_filter(self):
        identifier = "11111111-1111-4111-8111-111111111111"
        plan = make_plan(f"Explain run {identifier}")

        self.assertEqual(plan.intent, "exact_lookup")
        self.assertEqual(plan.detected_constraints.document_ids, [])
        self.assertFalse(plan.enforcement.requested_document_id_filter_applied)

    def test_explicit_document_id_is_canonicalized_without_rewriting_query(self):
        uppercase_id = "AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA"
        plan = make_plan(f"Search document ID {uppercase_id}")

        self.assertEqual(
            plan.detected_constraints.document_ids,
            [uppercase_id.casefold()],
        )
        self.assertTrue(plan.original_query_preserved)
        self.assertNotIn(uppercase_id, plan.retrieval_query)

    def test_exact_value_anchors_exclude_dates_years_and_identifier_suffixes(self):
        cases = (
            ("What happened on 2024-06-01?", []),
            ("What was the budget in 2024?", []),
            ("Find identifier ZETA-19", []),
            ("Find value $42.50 in the report", ["$42.50"]),
            (
                "Does any file contain 18,500 events per second and 15 percent?",
                ["18,500", "15 percent"],
            ),
            ("Compare version 2 with version 3", ["2", "3"]),
            (
                "Was the variance +12.5% or -2 percentage points?",
                ["+12.5%", "-2 percentage points"],
            ),
        )
        for query, expected in cases:
            with self.subTest(query=query):
                self.assertEqual(
                    make_plan(query).detected_constraints.exact_values,
                    expected,
                )

    def test_invalid_or_older_plan_fails_to_conservative_legacy_behavior(self):
        current = make_plan("Find the uploaded report").model_dump(mode="json")
        invalid = {**current, "branches": {**current["branches"], "graph": "false"}}
        older = {**current, "profile": "certus_deterministic_query_plan:v8"}

        self.assertIsNotNone(validate_query_plan(current))
        self.assertIsNone(validate_query_plan(invalid))
        self.assertIsNone(validate_query_plan(older))

    def test_hybrid_search_applies_document_filter_to_both_rankers(self):
        document_id = "11111111-1111-4111-8111-111111111111"
        embedding = MagicMock()
        embedding.vector = [0.1, 0.2]
        embedding.profile.identifier = "embedding-space:v1:test"
        cursor = MagicMock()
        cursor.fetchall.side_effect = [[], [], []]
        connection = MagicMock()
        connection.cursor.return_value.__enter__.return_value = cursor

        with patch.object(hybrid_module, "get_db_connection") as get_connection:
            get_connection.return_value.__enter__.return_value = connection
            results = hybrid_module.HybridSearchEngine.search(
                "find proof",
                "tenant",
                "user",
                top_k=50,
                query_embedding=embedding,
                allow_embedding_generation=False,
                document_ids=[document_id, document_id.upper()],
                titles=["Quarterly Report", "quarterly report"],
                years=["2024", 2024],
            )

        retrieval_calls = [
            call
            for call in cursor.execute.call_args_list
            if "AS chunk_id" in call.args[0]
        ]
        self.assertEqual(results, [])
        self.assertEqual(len(retrieval_calls), 3)
        for call in retrieval_calls:
            self.assertIn("c.document_id = ANY(%s::uuid[])", call.args[0])
            self.assertIn([document_id], call.args[1])
            self.assertIn("LOWER(BTRIM(version.title)) = ANY(%s::text[])", call.args[0])
            self.assertIn(["quarterly report"], call.args[1])
            self.assertIn(
                "EXTRACT(YEAR FROM COALESCE(version.source_time, version.recorded_at))",
                call.args[0],
            )
            self.assertIn([2024], call.args[1])
            candidate_limit = (
                call.args[1][-2]
                if "WITH nearest AS MATERIALIZED" in call.args[0]
                else call.args[1][-1]
            )
            self.assertEqual(candidate_limit, 50)

        semantic_sql = retrieval_calls[0].args[0]
        self.assertIn("WITH nearest AS MATERIALIZED", semantic_sql)
        self.assertIn("WHERE vector_distance <= %s", semantic_sql)
        self.assertIn("ORDER BY vector_distance + 0 ASC", semantic_sql)
        self.assertNotIn("(1.0 - (c.embedding <=>", semantic_sql)

        full_text_sql = retrieval_calls[1].args[0]
        phrase_sql = retrieval_calls[2].args[0]
        self.assertIn("search_vector @@", full_text_sql)
        self.assertNotIn(" ILIKE ", full_text_sql)
        self.assertIn(" ILIKE %s ESCAPE", phrase_sql)
        self.assertNotIn(" OR ", phrase_sql)

    def test_literal_phrase_escapes_user_wildcards(self):
        queries = hybrid_module.build_document_lexical_queries(
            query=r"100%_complete\path",
            tenant_id="tenant",
            user_id="user",
        )

        self.assertEqual(
            queries.literal_phrase.params[-2],
            r"%100\%\_complete\\path%",
        )

    def test_hybrid_search_rejects_invalid_or_unbounded_document_filters(self):
        self.assertEqual(hybrid_module.normalize_result_limit(1), 1)
        self.assertEqual(hybrid_module.normalize_result_limit(100), 100)
        self.assertEqual(hybrid_module.normalize_min_vector_similarity("0.3"), 0.3)
        for value in (0, 101, True, 1.5, "5"):
            with self.subTest(top_k=value), self.assertRaises(ValueError):
                hybrid_module.normalize_result_limit(value)
        for value in ("nan", "inf", "invalid", -1.01, 1.01, None):
            with self.subTest(similarity=value), self.assertRaises(ValueError):
                hybrid_module.normalize_min_vector_similarity(value)
        with self.assertRaises(ValueError):
            hybrid_module.normalize_document_ids(["not-a-uuid"])
        with self.assertRaises(ValueError):
            hybrid_module.normalize_document_ids(
                [f"00000000-0000-4000-8000-{index:012d}" for index in range(11)]
            )
        with self.assertRaises(ValueError):
            hybrid_module.normalize_temporal_years([999])
        with self.assertRaises(ValueError):
            hybrid_module.normalize_temporal_years(range(2000, 2011))
        with self.assertRaises(ValueError):
            hybrid_module.normalize_temporal_authority("both")
        with self.assertRaises(ValueError):
            hybrid_module.normalize_version_scope("latest-ish")
        with self.assertRaises(ValueError):
            hybrid_module.normalize_titles("one title")
        with self.assertRaises(ValueError):
            hybrid_module.normalize_titles(["valid", ""])
        with self.assertRaises(ValueError):
            hybrid_module.normalize_temporal_year_bound("twenty", "year_start")
        with self.assertRaises(ValueError):
            hybrid_module.normalize_temporal_time_bound(
                "2024-06-01T00:00:00",
                "time_start",
            )
        with patch.object(hybrid_module, "get_db_connection") as get_connection:
            with self.assertRaises(ValueError):
                hybrid_module.HybridSearchEngine.search(
                    "historical value",
                    "tenant",
                    "user",
                    allow_embedding_generation=False,
                    version_scope="as_of",
                )
            with self.assertRaises(ValueError):
                hybrid_module.HybridSearchEngine.search(
                    "historical value",
                    "tenant",
                    "user",
                    allow_embedding_generation=False,
                    years=[2024],
                    year_start=2024,
                )
            with self.assertRaises(ValueError):
                hybrid_module.HybridSearchEngine.search(
                    "historical value",
                    "tenant",
                    "user",
                    allow_embedding_generation=False,
                    year_start=2025,
                    year_end=2024,
                )
            with self.assertRaises(ValueError):
                hybrid_module.HybridSearchEngine.search(
                    "historical value",
                    "tenant",
                    "user",
                    allow_embedding_generation=False,
                    time_start="2024-06-02T00:00:00+00:00",
                    time_end="2024-06-01T00:00:00+00:00",
                )
        get_connection.assert_not_called()

    def test_memory_retrieval_limits_and_similarity_fail_closed(self):
        self.assertEqual(memory_module.normalize_memory_limit(1), 1)
        self.assertEqual(memory_module.normalize_memory_limit(100), 100)
        self.assertEqual(memory_module.normalize_memory_similarity("-1"), -1.0)
        self.assertEqual(memory_module.normalize_memory_similarity("0.3"), 0.3)
        self.assertEqual(memory_module.normalize_memory_similarity("1"), 1.0)
        for value in (0, 101, True, 1.5, "5"):
            with self.subTest(limit=value), self.assertRaises(ValueError):
                memory_module.normalize_memory_limit(value)
        for value in ("nan", "inf", "invalid", -1.01, 1.01, None):
            with self.subTest(similarity=value), self.assertRaises(ValueError):
                memory_module.normalize_memory_similarity(value)
        with (
            patch.object(memory_module, "embed_query") as embed_query,
            patch.object(memory_module, "get_db_connection") as get_connection,
            self.assertRaises(ValueError),
        ):
            memory_module.retrieve_relevant_memories(
                "query",
                "tenant",
                "user",
                limit=0,
            )
        embed_query.assert_not_called()
        get_connection.assert_not_called()

    def test_labeled_title_is_enforced_and_removed_from_retrieval_text(self):
        plan = make_plan(
            'Find budget proof in document titled "Quarterly Fiscal Report"'
        )

        self.assertEqual(
            plan.detected_constraints.titles,
            ["Quarterly Fiscal Report"],
        )
        self.assertEqual(plan.detected_constraints.quoted_phrases, [])
        self.assertTrue(plan.enforcement.requested_title_filter_applied)
        self.assertNotIn("Quarterly Fiscal Report", plan.retrieval_query)
        self.assertEqual(
            plan.deterministic_transformations,
            ["remove_explicit_title_clause:v1"],
        )

    def test_unlabeled_quote_and_title_overflow_do_not_narrow_retrieval(self):
        quoted = make_plan('Find the phrase "Quarterly Fiscal Report"')
        overflow_query = " compare ".join(
            f'document titled "Report {index}"' for index in range(6)
        )
        overflow = make_plan(overflow_query)

        self.assertEqual(quoted.detected_constraints.titles, [])
        self.assertFalse(quoted.enforcement.requested_title_filter_applied)
        self.assertIn("Quarterly Fiscal Report", quoted.detected_constraints.quoted_phrases)
        self.assertEqual(overflow.detected_constraints.titles, [])
        self.assertFalse(overflow.enforcement.requested_title_filter_applied)
        self.assertEqual(overflow.retrieval_query, overflow_query)

    def test_before_after_and_range_years_become_bounded_windows(self):
        cases = (
            ("What was true before 2024?", None, "2023", "source"),
            ("What was known after 2024?", "2025", None, "recorded"),
            ("What was valid between 2020 and 2025?", "2020", "2025", "source"),
            ("What was recorded from 2021 through 2023?", "2021", "2023", "recorded"),
        )
        for query, start, end, authority in cases:
            with self.subTest(query=query):
                plan = make_plan(query)
                self.assertEqual(plan.detected_constraints.year_start, start)
                self.assertEqual(plan.detected_constraints.year_end, end)
                self.assertEqual(
                    plan.detected_constraints.temporal_authority,
                    authority,
                )
                self.assertEqual(plan.enforcement.temporal_filter_mode, "year_bounds")
                self.assertTrue(plan.enforcement.requested_temporal_filter_applied)

    def test_invalid_or_ambiguous_year_bounds_are_detected_but_not_applied(self):
        for query in (
            "What changed between 2025 and 2020?",
            "What changed after 2999?",
            "What changed before 1000?",
        ):
            with self.subTest(query=query):
                plan = make_plan(query)
                self.assertIsNone(plan.detected_constraints.year_start)
                self.assertIsNone(plan.detected_constraints.year_end)
                self.assertEqual(plan.enforcement.temporal_filter_mode, "none")
                self.assertFalse(plan.enforcement.requested_temporal_filter_applied)

    def test_iso_dates_are_calendar_windows_not_whole_year_filters(self):
        cases = (
            (
                "What happened on 2024-06-01?",
                "2024-06-01T00:00:00+00:00",
                "2024-06-02T00:00:00+00:00",
                "source",
            ),
            (
                "What was true before 2024-06-01?",
                None,
                "2024-06-01T00:00:00+00:00",
                "source",
            ),
            (
                "What was known after 2024-06-01?",
                "2024-06-02T00:00:00+00:00",
                None,
                "recorded",
            ),
            (
                "What was valid between 2024-06-01 and 2025-06-01?",
                "2024-06-01T00:00:00+00:00",
                "2025-06-02T00:00:00+00:00",
                "source",
            ),
        )
        for query, start, end, authority in cases:
            with self.subTest(query=query):
                plan = make_plan(query)
                self.assertEqual(plan.detected_constraints.years, [])
                self.assertEqual(plan.detected_constraints.time_start, start)
                self.assertEqual(plan.detected_constraints.time_end_exclusive, end)
                self.assertEqual(plan.detected_constraints.temporal_authority, authority)
                self.assertEqual(plan.enforcement.temporal_filter_mode, "time_window")

    def test_iso_date_as_of_selects_a_single_date_cutoff(self):
        plan = make_plan("What was true as of 2024-06-01 in the report?")

        self.assertEqual(plan.detected_constraints.years, [])
        self.assertIsNone(plan.detected_constraints.time_start)
        self.assertEqual(
            plan.detected_constraints.time_end_exclusive,
            "2024-06-02T00:00:00+00:00",
        )
        self.assertEqual(plan.enforcement.temporal_filter_mode, "as_of")
        self.assertEqual(plan.enforcement.retrieval_version_scope, "as_of_version")

    def test_timezone_aware_instants_are_normalized_to_microsecond_windows(self):
        cases = (
            (
                "What happened at 2024-05-31T20:00:00-04:00?",
                "2024-06-01T00:00:00+00:00",
                "2024-06-01T00:00:00.000001+00:00",
                "time_window",
            ),
            (
                "What happened after 2024-06-01T00:00:00Z?",
                "2024-06-01T00:00:00.000001+00:00",
                None,
                "time_window",
            ),
            (
                "What was true as of 2024-06-01T00:00:00Z?",
                None,
                "2024-06-01T00:00:00.000001+00:00",
                "as_of",
            ),
            (
                "What changed from 2024-06-01T00:00:00Z through 2025-06-01T00:00:00Z?",
                "2024-06-01T00:00:00+00:00",
                "2025-06-01T00:00:00.000001+00:00",
                "time_window",
            ),
        )
        for query, start, end, mode in cases:
            with self.subTest(query=query):
                plan = make_plan(query)
                self.assertEqual(plan.detected_constraints.years, [])
                self.assertEqual(plan.detected_constraints.time_start, start)
                self.assertEqual(plan.detected_constraints.time_end_exclusive, end)
                self.assertEqual(plan.enforcement.temporal_filter_mode, mode)
                self.assertTrue(plan.enforcement.requested_temporal_filter_applied)

    def test_invalid_or_timezone_ambiguous_instant_is_not_degraded_to_year(self):
        for query in (
            "What happened on 2024-02-30?",
            "What happened at 2024-06-01T12:30:00?",
            "What happened at 2024-06-01T25:30:00Z?",
            "What happened at 2024-06-01T00:00:00-00:00?",
            "What happened at 1000-01-01T00:00:00+14:00?",
            "What happened at 2999-12-31T23:59:59-14:00?",
        ):
            with self.subTest(query=query):
                plan = make_plan(query)
                self.assertEqual(plan.detected_constraints.years, [])
                self.assertIsNone(plan.detected_constraints.time_start)
                self.assertIsNone(plan.detected_constraints.time_end_exclusive)
                self.assertEqual(plan.enforcement.temporal_filter_mode, "none")
                self.assertFalse(plan.enforcement.requested_temporal_filter_applied)

    def test_temporal_authority_distinguishes_source_from_recorded_time(self):
        source = make_plan("What was true in 2024?")
        recorded = make_plan("What was known to Certus in 2026?")

        self.assertEqual(source.detected_constraints.temporal_authority, "source")
        self.assertEqual(recorded.detected_constraints.temporal_authority, "recorded")
        self.assertTrue(source.enforcement.requested_temporal_filter_applied)
        self.assertTrue(recorded.enforcement.requested_temporal_filter_applied)

    def test_current_request_enforces_only_the_logical_current_version(self):
        plan = make_plan("What is the current value in the report?")

        self.assertEqual(
            plan.detected_constraints.requested_version_scope,
            "current_requested",
        )
        self.assertEqual(
            plan.enforcement.retrieval_version_scope,
            "current_version_only",
        )
        self.assertEqual(plan.enforcement.temporal_filter_mode, "none")
        self.assertFalse(plan.enforcement.requested_temporal_filter_applied)
        self.assertTrue(plan.enforcement.requested_version_filter_applied)

    def test_as_of_request_enforces_latest_version_by_requested_authority(self):
        source = make_plan("What was true as of 2024 in the report?")
        recorded = make_plan("What was known to Certus as of 2026?")

        for plan, authority in ((source, "source"), (recorded, "recorded")):
            self.assertEqual(
                plan.detected_constraints.requested_version_scope,
                "as_of_requested",
            )
            self.assertEqual(plan.detected_constraints.temporal_authority, authority)
            self.assertEqual(plan.enforcement.retrieval_version_scope, "as_of_version")
            self.assertEqual(plan.enforcement.temporal_filter_mode, "as_of")
            self.assertTrue(plan.enforcement.requested_temporal_filter_applied)
            self.assertTrue(plan.enforcement.requested_version_filter_applied)

    def test_ambiguous_multi_year_as_of_request_is_detected_but_not_applied(self):
        plan = make_plan("Compare what was known as of 2024 and 2025")

        self.assertEqual(
            plan.detected_constraints.requested_version_scope,
            "as_of_requested",
        )
        self.assertEqual(plan.enforcement.retrieval_version_scope, "all_retained_versions")
        self.assertEqual(plan.enforcement.temporal_filter_mode, "none")
        self.assertFalse(plan.enforcement.requested_temporal_filter_applied)
        self.assertFalse(plan.enforcement.requested_version_filter_applied)

    def test_relationship_query_adds_graph_without_personal_memory(self):
        plan = make_plan(
            "How is the uploaded report related to the cited policy document?"
        )

        self.assertEqual(plan.intent, "relationship")
        self.assertTrue(plan.branches.documents)
        self.assertTrue(plan.branches.graph)
        self.assertFalse(plan.branches.memories)

    def test_personal_memory_query_adds_memory_without_graph(self):
        plan = make_plan("What preference did I tell you to remember?")

        self.assertTrue(plan.branches.documents)
        self.assertTrue(plan.branches.memories)
        self.assertFalse(plan.branches.graph)

    def test_signal_matching_uses_term_boundaries_and_temporal_between_is_not_graph(self):
        profile_plan = make_plan("What is my profile preference?")
        range_plan = make_plan("What changed between 2020 and 2021?")

        self.assertFalse(profile_plan.routing_signals.document_focused)
        self.assertTrue(profile_plan.routing_signals.memory_focused)
        self.assertTrue(range_plan.routing_signals.temporal)
        self.assertFalse(range_plan.routing_signals.relationship_focused)
        self.assertFalse(range_plan.branches.graph)


if __name__ == "__main__":
    unittest.main()
