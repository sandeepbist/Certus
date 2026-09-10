import sys
import hashlib
import json
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier, Event
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from pydantic import ValidationError


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY_ROOT / "services" / "orchestration"))

from app.agents import graph
from app import replay as replay_module
from app.api import chat as chat_api
from app.api import traces as traces_api
from app.core import db as db_module
from app.core import runtime as runtime_module
from app.retrieval import graphrag as graphrag_module

sys.path.remove(str(REPOSITORY_ROOT / "services" / "orchestration"))
for module_name in list(sys.modules):
    if module_name == "app" or module_name.startswith("app."):
        del sys.modules[module_name]


def agent_event(agent: str):
    return {
        "agent": agent,
        "action": f"{agent} completed",
        "status": "completed",
        "details": {},
        "timestamp": 1.0,
    }


class StreamingOrchestratorTests(unittest.TestCase):
    def test_chat_api_validates_product_version_scope(self):
        request = chat_api.ChatQueryRequest(
            query="question",
            version_scope="current_only",
        )
        self.assertEqual(request.version_scope, "current_only")
        self.assertEqual(
            chat_api.ChatQueryRequest(query="question").version_scope,
            "auto",
        )
        with self.assertRaises(ValidationError):
            chat_api.ChatQueryRequest(
                query="question",
                version_scope="latest",  # type: ignore[arg-type]
            )
        with self.assertRaises(ValidationError):
            chat_api.ChatQueryRequest(
                query="question",
                session_id="session_default",  # type: ignore[arg-type]
            )

    def test_chat_session_claim_is_uuid_scoped_and_ownership_checked(self):
        session_id = "AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA"
        state = graph._initial_agent_state(
            "A" * 300,
            "tenant",
            "user",
            session_id,
            None,
            "run",
        )
        cursor = MagicMock()
        cursor.fetchone.return_value = {"id": session_id.casefold()}

        resolved = graph._ensure_chat_session(cursor, state)

        self.assertEqual(resolved, session_id.casefold())
        self.assertEqual(state["session_id"], session_id.casefold())
        self.assertEqual(cursor.execute.call_args_list[-1].args[1][-1], "A" * 255)
        self.assertIn("ON CONFLICT (id) DO UPDATE", cursor.execute.call_args_list[-1].args[0])
        self.assertIn("organization_id = EXCLUDED.organization_id", cursor.execute.call_args_list[-1].args[0])

        cursor.reset_mock()
        cursor.fetchone.return_value = None
        with self.assertRaises(graph.ChatRunConflict):
            graph._ensure_chat_session(cursor, state)

    def test_session_context_is_scoped_bounded_and_rejects_concurrent_turns(self):
        session_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        state = graph._initial_agent_state(
            "follow up",
            "tenant",
            "user",
            session_id,
            None,
            "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
        )
        cursor = MagicMock()
        cursor.fetchone.return_value = None
        cursor.fetchall.return_value = [
            {
                "id": "newer",
                "input_query": "Second question",
                "output_response": "Second answer",
                "answer_status": "answered",
            },
            {
                "id": "older",
                "input_query": "First question",
                "output_response": "First answer",
                "answer_status": "answered",
            },
        ]

        context = graph._load_conversation_context(cursor, state, "current")

        self.assertEqual(
            [turn["run_id"] for turn in context["turns"]],
            ["older", "newer"],
        )
        context_query = cursor.execute.call_args_list[-1].args[0]
        self.assertIn("tenant_id = %s", context_query)
        self.assertIn("user_id = %s", context_query)
        self.assertIn("session_id = %s", context_query)
        self.assertIn("replay_of_run_id IS NULL", context_query)
        self.assertIn("LIMIT 4", context_query)

        cursor.reset_mock()
        cursor.fetchone.return_value = {"id": "active"}
        with self.assertRaises(graph.ChatRunConflict):
            graph._load_conversation_context(cursor, state, "current")

    def test_request_fingerprint_binds_canonical_selected_document_scope(self):
        selected_id = "AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA"
        first = graph._initial_agent_state(
            "question",
            "tenant",
            "user",
            "session",
            None,
            "run",
            selected_document_ids=[selected_id],
        )
        equivalent = graph._initial_agent_state(
            "question",
            "tenant",
            "user",
            "session",
            None,
            "run",
            selected_document_ids=[selected_id.casefold(), selected_id],
        )
        different_scope = graph._initial_agent_state(
            "question",
            "tenant",
            "user",
            "session",
            None,
            "run",
            selected_document_ids=["BBBBBBBB-BBBB-4BBB-8BBB-BBBBBBBBBBBB"],
        )

        self.assertEqual(first["selected_document_ids"], [selected_id.casefold()])
        self.assertEqual(first["request_fingerprint"], equivalent["request_fingerprint"])
        self.assertNotEqual(
            first["request_fingerprint"],
            different_scope["request_fingerprint"],
        )

    def test_request_fingerprint_binds_product_version_scope(self):
        automatic = graph._initial_agent_state(
            "question", "tenant", "user", "session", None, "run"
        )
        historical = graph._initial_agent_state(
            "question",
            "tenant",
            "user",
            "session",
            None,
            "run",
            selected_version_scope="all_history",
        )
        current = graph._initial_agent_state(
            "question",
            "tenant",
            "user",
            "session",
            None,
            "run",
            selected_version_scope="current_only",
        )

        self.assertEqual(automatic["selected_version_scope"], "auto")
        self.assertEqual(
            automatic["request_fingerprint"],
            hashlib.sha256(json.dumps(
                {
                    "model": "auto",
                    "query": "question",
                    "replay_mode": "original",
                    "replay_of_run_id": None,
                    "selected_document_ids": [],
                    "session_id": "session",
                    "conversation_context": {},
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")).hexdigest(),
        )
        self.assertEqual(historical["selected_version_scope"], "all_history")
        self.assertNotEqual(
            automatic["request_fingerprint"], historical["request_fingerprint"]
        )
        self.assertNotEqual(
            historical["request_fingerprint"], current["request_fingerprint"]
        )
        with self.assertRaises(ValueError):
            graph._initial_agent_state(
                "question",
                "tenant",
                "user",
                "session",
                None,
                "run",
                selected_version_scope="latest",
            )

    def test_existing_run_rejects_a_different_request_fingerprint(self):
        state = graph._initial_agent_state(
            "question",
            "tenant",
            "user",
            "session",
            None,
            "run",
            selected_document_ids=["AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA"],
        )
        existing = {
            "tenant_id": "tenant",
            "user_id": "user",
            "input_query": "question",
            "request_fingerprint": "0" * 64,
            "status": "completed",
        }

        with self.assertRaises(graph.ChatRunConflict):
            graph._resolve_existing_agent_run(
                MagicMock(), MagicMock(), state, "run", existing
            )

    def test_researcher_skips_all_retrieval_for_self_contained_tool_plan(self):
        plan = graph.build_query_plan(
            "Create a task to review the archive",
            [{"tool_name": "create_task", "arguments": {}}],
            selected_model="local-extractive",
            is_complex=True,
        ).model_dump(mode="json")

        with (
            patch.object(graph, "embed_query") as embed,
            patch.object(graph, "get_retrieval_executor") as get_executor,
            patch.object(graph.HybridSearchEngine, "search") as documents,
            patch.object(graph.GraphRAGEngine, "traverse_graph") as graph_branch,
            patch.object(graph, "retrieve_relevant_memories") as memories,
        ):
            result = graph.researcher_node({
                "query": "Create a task to review the archive",
                "tenant_id": "tenant",
                "user_id": "user",
                "plan": plan,
                "agent_events": [],
                "cancel_event": None,
            })

        embed.assert_not_called()
        get_executor.assert_not_called()
        documents.assert_not_called()
        graph_branch.assert_not_called()
        memories.assert_not_called()
        self.assertEqual(result["retrieved_chunks"], [])
        self.assertEqual(result["retrieved_memories"], [])
        self.assertEqual(
            result["agent_events"][-1]["details"]["planned_branches"],
            {"documents": False, "memories": False, "graph": False},
        )

    def test_researcher_runs_only_planned_document_branch(self):
        embedding = MagicMock()
        embedding.profile.identifier = "embedding-space:v1:test"
        embedding.input_tokens = 7
        embedding.estimated_cost_usd = 0.00000014
        embedding.pricing_profile = "openai_standard_text:2026-08-30"
        document_id = "11111111-1111-4111-8111-111111111111"
        plan = graph.build_query_plan(
            f"Find budget proof in document ID {document_id} from 2024",
            [],
            selected_model="local-extractive",
            is_complex=False,
        ).model_dump(mode="json")

        class Chunk:
            def to_dict(self):
                return {"chunk_id": "chunk-1"}

        with (
            ThreadPoolExecutor(max_workers=1) as executor,
            patch.object(graph, "embed_query", return_value=embedding) as embed,
            patch.object(graph, "get_retrieval_executor", return_value=executor),
            patch.object(graph.HybridSearchEngine, "search", return_value=[Chunk()]) as documents,
            patch.object(graph.GraphRAGEngine, "traverse_graph") as graph_branch,
            patch.object(graph, "retrieve_relevant_memories") as memories,
        ):
            result = graph.researcher_node({
                "query": f"Find budget proof in document ID {document_id} from 2024",
                "tenant_id": "tenant",
                "user_id": "user",
                "plan": plan,
                "agent_events": [],
                "cancel_event": None,
            })

        embed.assert_called_once_with("Find budget proof from 2024")
        documents.assert_called_once()
        self.assertEqual(documents.call_args.args[0], "Find budget proof from 2024")
        self.assertEqual(documents.call_args.args[6], [document_id])
        self.assertEqual(documents.call_args.args[7], ["2024"])
        self.assertEqual(documents.call_args.args[8], "effective")
        self.assertEqual(documents.call_args.args[9], "all")
        self.assertEqual(documents.call_args.args[14], [])
        graph_branch.assert_not_called()
        memories.assert_not_called()
        self.assertEqual(result["retrieved_chunks"], [{"chunk_id": "chunk-1"}])
        self.assertEqual(result["embedding_tokens"], 7)
        self.assertEqual(result["embedding_cost_usd"], 0.00000014)
        self.assertEqual(
            result["agent_events"][-1]["details"]["planned_branches"],
            {"documents": True, "memories": False, "graph": False},
        )
        self.assertEqual(
            result["agent_events"][-1]["details"]["applied_document_filter_count"],
            1,
        )
        self.assertEqual(
            result["agent_events"][-1]["details"]["applied_temporal_years"],
            ["2024"],
        )

    def test_researcher_dispatches_single_cutoff_as_as_of_scope(self):
        embedding = MagicMock()
        embedding.profile.identifier = "embedding-space:v1:test"
        embedding.input_tokens = 5
        embedding.estimated_cost_usd = 0.0
        embedding.pricing_profile = None
        plan = graph.build_query_plan(
            "What was true as of 2024 in the report?",
            [],
            selected_model="local-extractive",
            is_complex=False,
        ).model_dump(mode="json")

        with (
            ThreadPoolExecutor(max_workers=1) as executor,
            patch.object(graph, "embed_query", return_value=embedding),
            patch.object(graph, "get_retrieval_executor", return_value=executor),
            patch.object(graph.HybridSearchEngine, "search", return_value=[]) as documents,
        ):
            result = graph.researcher_node({
                "query": "What was true as of 2024 in the report?",
                "tenant_id": "tenant",
                "user_id": "user",
                "plan": plan,
                "agent_events": [],
                "cancel_event": None,
            })

        self.assertEqual(documents.call_args.args[7], ["2024"])
        self.assertEqual(documents.call_args.args[8], "source")
        self.assertEqual(documents.call_args.args[9], "as_of")
        self.assertIsNone(documents.call_args.args[10])
        self.assertIsNone(documents.call_args.args[11])
        self.assertIsNone(documents.call_args.args[12])
        self.assertIsNone(documents.call_args.args[13])
        details = result["agent_events"][-1]["details"]
        self.assertEqual(details["temporal_filter_mode"], "as_of")
        self.assertEqual(details["retrieval_version_scope"], "as_of_version")

    def test_researcher_dispatches_product_selected_all_history_scope(self):
        embedding = MagicMock()
        embedding.profile.identifier = "embedding-space:v1:test"
        embedding.input_tokens = 5
        embedding.estimated_cost_usd = 0.0
        embedding.pricing_profile = None
        plan = graph.build_query_plan(
            "Show the current value in the report",
            [],
            selected_model="local-extractive",
            is_complex=False,
            selected_version_scope="all_history",
        ).model_dump(mode="json")

        with (
            ThreadPoolExecutor(max_workers=1) as executor,
            patch.object(graph, "embed_query", return_value=embedding),
            patch.object(graph, "get_retrieval_executor", return_value=executor),
            patch.object(graph.HybridSearchEngine, "search", return_value=[]) as documents,
        ):
            result = graph.researcher_node({
                "query": "Show the current value in the report",
                "tenant_id": "tenant",
                "user_id": "user",
                "plan": plan,
                "agent_events": [],
                "cancel_event": None,
            })

        self.assertEqual(documents.call_args.args[9], "all")
        details = result["agent_events"][-1]["details"]
        self.assertEqual(details["retrieval_version_scope"], "all_retained_versions")

    def test_researcher_dispatches_year_bounds_without_exact_years(self):
        embedding = MagicMock()
        embedding.profile.identifier = "embedding-space:v1:test"
        embedding.input_tokens = 5
        embedding.estimated_cost_usd = 0.0
        embedding.pricing_profile = None
        plan = graph.build_query_plan(
            "What was true after 2024 in the report?",
            [],
            selected_model="local-extractive",
            is_complex=False,
        ).model_dump(mode="json")

        with (
            ThreadPoolExecutor(max_workers=1) as executor,
            patch.object(graph, "embed_query", return_value=embedding),
            patch.object(graph, "get_retrieval_executor", return_value=executor),
            patch.object(graph.HybridSearchEngine, "search", return_value=[]) as documents,
        ):
            result = graph.researcher_node({
                "query": "What was true after 2024 in the report?",
                "tenant_id": "tenant",
                "user_id": "user",
                "plan": plan,
                "agent_events": [],
                "cancel_event": None,
            })

        self.assertEqual(documents.call_args.args[7], [])
        self.assertEqual(documents.call_args.args[8], "source")
        self.assertEqual(documents.call_args.args[9], "all")
        self.assertEqual(documents.call_args.args[10], "2025")
        self.assertIsNone(documents.call_args.args[11])
        details = result["agent_events"][-1]["details"]
        self.assertEqual(details["temporal_filter_mode"], "year_bounds")
        self.assertEqual(details["applied_temporal_year_start"], "2025")

    def test_researcher_dispatches_iso_date_as_of_cutoff(self):
        embedding = MagicMock()
        embedding.profile.identifier = "embedding-space:v1:test"
        embedding.input_tokens = 5
        embedding.estimated_cost_usd = 0.0
        embedding.pricing_profile = None
        plan = graph.build_query_plan(
            "What was true as of 2024-06-01 in the report?",
            [],
            selected_model="local-extractive",
            is_complex=False,
        ).model_dump(mode="json")

        with (
            ThreadPoolExecutor(max_workers=1) as executor,
            patch.object(graph, "embed_query", return_value=embedding),
            patch.object(graph, "get_retrieval_executor", return_value=executor),
            patch.object(graph.HybridSearchEngine, "search", return_value=[]) as documents,
        ):
            result = graph.researcher_node({
                "query": "What was true as of 2024-06-01 in the report?",
                "tenant_id": "tenant",
                "user_id": "user",
                "plan": plan,
                "agent_events": [],
                "cancel_event": None,
            })

        self.assertEqual(documents.call_args.args[7], [])
        self.assertEqual(documents.call_args.args[8], "source")
        self.assertEqual(documents.call_args.args[9], "as_of")
        self.assertIsNone(documents.call_args.args[12])
        self.assertEqual(
            documents.call_args.args[13],
            "2024-06-02T00:00:00+00:00",
        )
        details = result["agent_events"][-1]["details"]
        self.assertEqual(details["temporal_filter_mode"], "as_of")
        self.assertEqual(
            details["applied_temporal_time_end_exclusive"],
            "2024-06-02T00:00:00+00:00",
        )

    def test_researcher_dispatches_timezone_normalized_exact_instant(self):
        embedding = MagicMock()
        embedding.profile.identifier = "embedding-space:v1:test"
        embedding.input_tokens = 5
        embedding.estimated_cost_usd = 0.0
        embedding.pricing_profile = None
        query = "What happened at 2024-05-31T20:00:00-04:00?"
        plan = graph.build_query_plan(
            query,
            [],
            selected_model="local-extractive",
            is_complex=False,
        ).model_dump(mode="json")

        with (
            ThreadPoolExecutor(max_workers=1) as executor,
            patch.object(graph, "embed_query", return_value=embedding),
            patch.object(graph, "get_retrieval_executor", return_value=executor),
            patch.object(graph.HybridSearchEngine, "search", return_value=[]) as documents,
        ):
            result = graph.researcher_node({
                "query": query,
                "tenant_id": "tenant",
                "user_id": "user",
                "plan": plan,
                "agent_events": [],
                "cancel_event": None,
            })

        self.assertEqual(
            documents.call_args.args[12],
            "2024-06-01T00:00:00+00:00",
        )
        self.assertEqual(
            documents.call_args.args[13],
            "2024-06-01T00:00:00.000001+00:00",
        )
        details = result["agent_events"][-1]["details"]
        self.assertEqual(details["temporal_filter_mode"], "time_window")
        self.assertEqual(
            details["applied_temporal_time_start"],
            "2024-06-01T00:00:00+00:00",
        )
        self.assertEqual(
            details["applied_temporal_time_end_exclusive"],
            "2024-06-01T00:00:00.000001+00:00",
        )

    def test_researcher_dispatches_labeled_title_scope(self):
        embedding = MagicMock()
        embedding.profile.identifier = "embedding-space:v1:test"
        embedding.input_tokens = 5
        embedding.estimated_cost_usd = 0.0
        embedding.pricing_profile = None
        plan = graph.build_query_plan(
            'Find evidence in document titled "Quarterly Fiscal Report"',
            [],
            selected_model="local-extractive",
            is_complex=False,
        ).model_dump(mode="json")

        with (
            ThreadPoolExecutor(max_workers=1) as executor,
            patch.object(graph, "embed_query", return_value=embedding),
            patch.object(graph, "get_retrieval_executor", return_value=executor),
            patch.object(graph.HybridSearchEngine, "search", return_value=[]) as documents,
        ):
            result = graph.researcher_node({
                "query": 'Find evidence in document titled "Quarterly Fiscal Report"',
                "tenant_id": "tenant",
                "user_id": "user",
                "plan": plan,
                "agent_events": [],
                "cancel_event": None,
            })

        self.assertEqual(documents.call_args.args[0], "Find evidence in")
        self.assertEqual(documents.call_args.args[14], ["Quarterly Fiscal Report"])
        self.assertEqual(
            result["agent_events"][-1]["details"]["applied_title_filter_count"],
            1,
        )

    def test_researcher_embeds_once_and_runs_independent_branches_concurrently(self):
        embedding = MagicMock()
        embedding.profile.identifier = "embedding-space:v1:test"
        barrier = Barrier(3)
        branch_embeddings = {}

        class Chunk:
            def to_dict(self):
                return {"chunk_id": "chunk-1"}

        def documents(*args):
            branch_embeddings["documents"] = args[4]
            barrier.wait(timeout=1)
            return [Chunk()]

        def graph_branch(*_args):
            barrier.wait(timeout=1)
            return {"graph_triples": ["A -> B"], "relationships_count": 1}

        def memories(*args):
            branch_embeddings["memories"] = args[4]
            barrier.wait(timeout=1)
            return [{"id": "memory-1", "fact": "proof"}]

        with (
            ThreadPoolExecutor(max_workers=3) as executor,
            patch.object(graph, "embed_query", return_value=embedding) as embed,
            patch.object(graph, "get_retrieval_executor", return_value=executor),
            patch.object(graph.HybridSearchEngine, "search", side_effect=documents),
            patch.object(graph.GraphRAGEngine, "traverse_graph", side_effect=graph_branch),
            patch.object(graph, "retrieve_relevant_memories", side_effect=memories),
        ):
            result = graph.researcher_node({
                "query": "find the proof",
                "tenant_id": "tenant",
                "user_id": "user",
                "agent_events": [],
                "cancel_event": None,
            })

        embed.assert_called_once_with("find the proof")
        self.assertIs(branch_embeddings["documents"], embedding)
        self.assertIs(branch_embeddings["memories"], embedding)
        self.assertEqual(result["retrieved_chunks"], [{"chunk_id": "chunk-1"}])
        self.assertEqual(result["retrieved_memories"][0]["id"], "memory-1")
        details = result["agent_events"][-1]["details"]
        self.assertEqual(details["shared_embedding_profile"], "embedding-space:v1:test")
        self.assertEqual(details["timed_out_branches"], [])

    def test_researcher_deadline_degrades_only_the_slow_branch(self):
        slow_release = Event()

        def slow_documents(*_args):
            slow_release.wait(timeout=0.2)
            return []

        executor = ThreadPoolExecutor(max_workers=3)
        try:
            started_at = time.monotonic()
            with (
                patch.object(graph, "RETRIEVAL_STAGE_TIMEOUT_SECONDS", 0.01),
                patch.object(graph, "embed_query", return_value=None),
                patch.object(graph, "get_retrieval_executor", return_value=executor),
                patch.object(
                    graph.HybridSearchEngine,
                    "search",
                    side_effect=slow_documents,
                ),
                patch.object(
                    graph.GraphRAGEngine,
                    "traverse_graph",
                    return_value={"graph_triples": [], "relationships_count": 0},
                ),
                patch.object(graph, "retrieve_relevant_memories", return_value=[]),
            ):
                result = graph.researcher_node({
                    "query": "bounded retrieval",
                    "tenant_id": "tenant",
                    "user_id": "user",
                    "agent_events": [],
                    "cancel_event": None,
                })
            elapsed = time.monotonic() - started_at
        finally:
            slow_release.set()
            executor.shutdown(wait=True, cancel_futures=True)

        self.assertLess(elapsed, 0.1)
        self.assertEqual(result["retrieved_chunks"], [])
        self.assertEqual(
            result["agent_events"][-1]["details"]["timed_out_branches"],
            ["documents"],
        )

    def test_trace_projection_excludes_frozen_source_snapshots_and_provider_ids(self):
        projected = traces_api._trace_for_client({
            "id": "run-1",
            "evidence_manifest": {
                "profile": "certus_typed_evidence_manifest:v1",
                "source_count": 1,
                "canonical_sha256": "a" * 64,
                "rendered_pack_sha256": "b" * 64,
                "sources": [{
                    "source_kind": "tool",
                    "content_snapshot": "private tool output",
                }],
            },
            "generation_profile": {
                "profile": "certus_grounded_generation:v1",
                "provider": "openai",
                "provider_response_id": "resp_private",
                "provider_attempt": {"failure_type": "example"},
            },
            "conversation_context": {
                "profile": "certus_conversation_context:v1",
                "turn_count": 1,
                "canonical_sha256": "c" * 64,
                "turns": [{
                    "run_id": "prior",
                    "question": "private prior question",
                    "response": "private prior response",
                    "answer_status": "answered",
                }],
            },
        })

        self.assertNotIn("sources", projected["evidence_manifest"])
        self.assertEqual(
            projected["evidence_manifest"]["source_kind_counts"]["tool"],
            1,
        )
        self.assertNotIn("provider_response_id", projected["generation_profile"])
        self.assertNotIn("provider_attempt", projected["generation_profile"])
        self.assertEqual(projected["conversation_context"]["turn_count"], 1)
        self.assertNotIn("turns", projected["conversation_context"])

    def test_fresh_replay_recovers_only_valid_product_selected_scope(self):
        document_id = "AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA"
        documents, version_scope = traces_api._product_scope_from_plan({
            "detected_constraints": {
                "product_selected_document_ids": [
                    document_id,
                    document_id.casefold(),
                    "not-a-uuid",
                ],
                "product_selected_version_scope": "all_history",
            }
        })

        self.assertEqual(documents, [document_id.casefold()])
        self.assertEqual(version_scope, "all_history")
        self.assertEqual(traces_api._product_scope_from_plan(None), ([], "auto"))
        self.assertEqual(
            traces_api._product_scope_from_plan({
                "detected_constraints": {
                    "product_selected_version_scope": "unsupported",
                }
            }),
            ([], "auto"),
        )

    def test_fresh_replay_forwards_original_product_scope(self):
        document_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        cursor = MagicMock()
        cursor.fetchone.return_value = {
            "input_query": "Compare retained values",
            "latency_ms": 12,
            "eval_score": 1.0,
            "answer_status": "answered",
            "grounding_profile": "test:v1",
            "plan": {
                "detected_constraints": {
                    "product_selected_document_ids": [document_id],
                    "product_selected_version_scope": "all_history",
                }
            },
        }
        database_context = MagicMock()
        database_context.__enter__.return_value = cursor
        database_context.__exit__.return_value = False
        replayed = {
            "run_id": "new-run",
            "replay_mode": "fresh_retrieval",
            "provenance": {},
            "latency_ms": 14,
            "eval_score": 1.0,
            "answer_status": "answered",
            "grounding_profile": "test:v1",
            "model_used": "local-extractive",
            "response": "answer",
            "agent_events": [],
        }

        with (
            patch.object(traces_api, "get_db_cursor", return_value=database_context),
            patch.object(traces_api, "replay_models", return_value=["auto"]),
            patch.object(
                traces_api.MultiAgentOrchestrator,
                "execute",
                return_value=replayed,
            ) as execute,
        ):
            result = traces_api.replay_trace(
                "original-run",
                traces_api.TraceReplayRequest(mode="fresh_retrieval"),
                SimpleNamespace(tenant_id="tenant", user_id="user"),
            )

        execute.assert_called_once_with(
            query="Compare retained values",
            tenant_id="tenant",
            user_id="user",
            model_override=None,
            replay_of_run_id="original-run",
            replay_mode="fresh_retrieval",
            document_ids=[document_id],
            version_scope="all_history",
            conversation_context={},
        )
        self.assertEqual(result["replayed_run_id"], "new-run")

    def test_frozen_replay_reconstructs_document_content_without_new_retrieval(self):
        source = {
            "document_id": "11111111-1111-4111-8111-111111111111",
            "document_version_id": "22222222-2222-4222-8222-222222222222",
            "derivation_id": "55555555-5555-4555-8555-555555555555",
            "parsed_artifact_id": "44444444-4444-4444-8444-444444444444",
            "version_number": 1,
            "document_title": "Budget proof",
            "chunk_id": "77777777-7777-4777-8777-777777777777",
            "content": "The approved budget is 42 credits.",
            "score": 1.0,
            "content_hash": "a" * 64,
            "source_time": None,
            "recorded_at": "2026-08-30T00:00:00+00:00",
            "is_current_version": True,
            "page_number": 1,
            "start_char": 0,
            "end_char": 34,
            "text_locator_status": "exact",
            "text_locator_profile": "unicode_code_point:zero_based_half_open:v1",
            "retrieval_method": "test",
        }
        manifest = graph.build_evidence_manifest(graph.build_evidence_pack([source], []))
        cursor = MagicMock()
        cursor.fetchone.return_value = {"content": source["content"]}
        connection = MagicMock()
        connection.cursor.return_value.__enter__.return_value = cursor

        with patch.object(replay_module.psycopg2, "connect", return_value=connection):
            reconstructed = replay_module.reconstruct_frozen_evidence(
                manifest,
                "tenant",
                "user",
                "postgresql://test",
            )

        self.assertEqual(reconstructed["retrieved_chunks"][0]["content"], source["content"])
        self.assertEqual(reconstructed["tool_results"], [])
        connection.close.assert_called_once()

    def test_executor_emits_full_server_owned_retrieval_evidence_witnesses(self):
        quote = "Exact source text 😀 with spacing. " * 12
        chunk = {
            "document_id": "document-1",
            "document_version_id": "version-1",
            "derivation_id": "derivation-1",
            "parsed_artifact_id": "parsed-1",
            "version_number": 7,
            "document_title": "Proof",
            "chunk_id": "chunk-1",
            "content": quote,
            "page_number": 3,
            "content_hash": "a" * 64,
            "start_char": 11,
            "end_char": 11 + len(quote),
            "text_locator_status": "exact",
            "text_locator_profile": "unicode_code_point:zero_based_half_open:v1",
            "source_time": None,
            "recorded_at": "2026-08-29T00:00:00+00:00",
            "is_current_version": False,
        }

        with patch.object(graph, "has_openai_api_key", return_value=False):
            result = graph.executor_node({
                "query": "What is the proof?",
                "model": "local-extractive",
                "agent_events": [],
                "retrieved_chunks": [chunk],
                "retrieved_memories": [],
                "graph_context": {},
                "tool_results": [],
                "cancel_event": None,
            })

        citation = result["citations"][0]
        self.assertEqual(citation["quote"], quote)
        self.assertEqual(
            citation["quote_sha256"],
            hashlib.sha256(quote.encode("utf-8")).hexdigest(),
        )
        self.assertEqual(citation["parsed_artifact_id"], "parsed-1")
        self.assertEqual(citation["start_char"], 11)
        self.assertEqual(citation["text_locator_status"], "exact")
        self.assertEqual(
            citation["support_scope"],
            "atomic_claim_selected",
        )
        self.assertEqual(citation["evidence_id"], "D1")
        self.assertEqual(citation["claim_ids"], ["C1"])
        self.assertEqual(result["answer_status"], "extractive")
        self.assertEqual(result["grounding_profile"], graph.GROUNDING_PROFILE)
        self.assertEqual(result["claim_evidence"][0]["claim_id"], "C1")
        self.assertEqual(
            result["claim_evidence"][0]["semantic_support_status"],
            "not_evaluated",
        )

    def test_critic_records_mechanical_integrity_as_non_semantic(self):
        source = {
            "document_id": "document-1",
            "document_version_id": "version-1",
            "derivation_id": "derivation-1",
            "parsed_artifact_id": "parsed-1",
            "version_number": 1,
            "document_title": "Budget",
            "chunk_id": "chunk-1",
            "content": "The approved budget is 42 credits.",
            "content_hash": "a" * 64,
        }
        grounded = graph.build_extractive_answer(graph.build_evidence_pack([source], []))
        evidence_pack = graph.build_evidence_pack([source], [])
        evidence_manifest = graph.build_evidence_manifest(evidence_pack)
        generation_profile = graph.build_generation_profile(
            execution_mode="local_extractive",
            provider="certus_local",
            requested_model="local-extractive",
            returned_model="local-extractive",
            query="What is the budget?",
            evidence_pack=evidence_pack,
            evidence_manifest=evidence_manifest,
        )
        result = graph.critic_node({
            "query": "What is the budget?",
            "agent_events": [],
            "response": grounded["response"],
            "citations": grounded["citations"],
            "claim_evidence": grounded["claims"],
            "answer_status": grounded["answer_status"],
            "grounding_profile": grounded["grounding_profile"],
            "evidence_manifest": evidence_manifest,
            "generation_profile": generation_profile,
            "retrieved_chunks": [source],
            "retrieved_memories": [],
            "graph_context": {},
            "tool_results": [],
            "iteration_count": 0,
            "cancel_event": None,
        })

        self.assertEqual(result["eval_score"], 1.0)
        self.assertEqual(
            result["eval_details"]["metric"],
            "mechanical_claim_evidence_integrity",
        )
        self.assertFalse(result["eval_details"]["semantic_support_metric"])
        self.assertEqual(
            result["agent_events"][-1]["details"]["metric"],
            "mechanical_claim_evidence_integrity",
        )

    def test_critic_fails_closed_on_a_dangling_selected_citation(self):
        result = graph.critic_node({
            "agent_events": [],
            "response": "Unreleased answer",
            "citations": [{"evidence_id": "D1", "claim_ids": ["C9"]}],
            "claim_evidence": [],
            "answer_status": "answered",
            "grounding_profile": graph.GROUNDING_PROFILE,
            "iteration_count": 0,
            "cancel_event": None,
        })

        self.assertEqual(result["eval_score"], 0.0)
        self.assertEqual(result["answer_status"], "insufficient_evidence")
        self.assertEqual(result["citations"], [])
        self.assertEqual(result["claim_evidence"], [])
        self.assertIn("no answer was released", result["response"])

    def test_hosted_proposal_is_validated_before_any_answer_is_released(self):
        source = {
            "document_id": "document-1",
            "document_version_id": "version-1",
            "derivation_id": "derivation-1",
            "parsed_artifact_id": "parsed-1",
            "version_number": 1,
            "document_title": "Budget",
            "chunk_id": "chunk-1",
            "content": "The approved budget is 42 credits.",
            "content_hash": "a" * 64,
        }
        emitted = []
        with (
            patch.object(graph, "has_openai_api_key", return_value=True),
            patch.object(
                graph,
                "generate_openai_grounded_proposal",
                return_value=(
                    {"status": "answer", "claims": [{
                        "text": "The approved budget is 42 credits.",
                        "source_ids": ["D1"],
                    }]},
                    12,
                    8,
                    {
                        "provider": "openai",
                        "returned_model": "gpt-5.4-mini-2026-03-17",
                        "service_tier": "default",
                        "cached_input_tokens": 0,
                    },
                ),
            ) as generate,
        ):
            conversation_context = graph.build_conversation_context([{
                "id": "prior-run",
                "input_query": "What was the 2024 budget?",
                "output_response": "The evidence reported 40 credits.",
                "answer_status": "answered",
            }])
            result = graph.executor_node({
                "query": "What about the next year?",
                "user_id": "user",
                "model": "gpt-5.4-mini",
                "agent_events": [],
                "retrieved_chunks": [source],
                "retrieved_memories": [],
                "graph_context": {},
                "tool_results": [],
                "event_sink": emitted.append,
                "embedding_tokens": 7,
                "embedding_cost_usd": 0.00000014,
                "embedding_pricing_profile": "openai_standard_text:2026-08-30",
                "conversation_context": conversation_context,
                "cancel_event": None,
            })

        self.assertEqual(emitted, [])
        self.assertEqual(result["answer_status"], "answered")
        self.assertEqual(result["citations"][0]["claim_ids"], ["C1"])
        self.assertEqual(result["prompt_tokens"], 12)
        self.assertEqual(result["estimated_cost_usd"], 0.00004514)
        self.assertEqual(
            result["generation_profile"]["cost_estimate"]["total_amount_usd"],
            0.00004514,
        )
        self.assertIn("[1]", result["response"])
        generation_query = generate.call_args.args[2]
        self.assertIn("untrusted dialogue context only", generation_query)
        self.assertIn("What was the 2024 budget?", generation_query)
        self.assertIn("Current user question:\nWhat about the next year?", generation_query)
        self.assertEqual(result["evidence_manifest"]["source_count"], 1)

    def test_invalid_hosted_proposal_falls_back_to_exact_extractive_evidence(self):
        source = {
            "document_id": "document-1",
            "document_version_id": "version-1",
            "derivation_id": "derivation-1",
            "parsed_artifact_id": "parsed-1",
            "version_number": 1,
            "document_title": "Budget",
            "chunk_id": "chunk-1",
            "content": "The approved budget is 42 credits.",
            "content_hash": "a" * 64,
        }
        with (
            patch.object(graph, "has_openai_api_key", return_value=True),
            patch.object(
                graph,
                "generate_openai_grounded_proposal",
                return_value=(
                    {"status": "answer", "claims": [{
                        "text": "The approved budget is 99 credits.",
                        "source_ids": ["D1"],
                    }]},
                    12,
                    8,
                    {"provider": "openai", "returned_model": "gpt-test"},
                ),
            ),
        ):
            result = graph.executor_node({
                "query": "What is the budget?",
                "user_id": "user",
                "model": "gpt-test",
                "agent_events": [],
                "retrieved_chunks": [source],
                "retrieved_memories": [],
                "graph_context": {},
                "tool_results": [],
                "cancel_event": None,
            })

        self.assertEqual(result["answer_status"], "extractive")
        self.assertEqual(result["model"], "local-extractive-fallback")
        self.assertEqual((result["prompt_tokens"], result["completion_tokens"]), (12, 8))
        self.assertIsNone(result["estimated_cost_usd"])
        self.assertIn("42 credits", result["response"])
        self.assertNotIn("99 credits", result["response"])

    def test_openai_request_uses_strict_structured_output_without_raw_streaming(self):
        fake_response = SimpleNamespace(
            id="resp_test",
            created_at=1_788_048_000,
            model="gpt-test-2026-08-30",
            service_tier="default",
            status="completed",
            output_text=(
                '{"status":"answer","claims":['
                '{"text":"The budget is 42 credits.","source_ids":["D1"]}]}'
            ),
            usage=SimpleNamespace(
                input_tokens=9,
                output_tokens=6,
                input_tokens_details=SimpleNamespace(cached_tokens=2),
            ),
        )
        fake_client = SimpleNamespace(
            responses=SimpleNamespace(create=lambda **kwargs: fake_response)
        )
        captured = {}

        def create(**kwargs):
            captured.update(kwargs)
            return fake_response

        fake_client.responses.create = create
        source = {
            "document_id": "document-1",
            "document_version_id": "version-1",
            "derivation_id": "derivation-1",
            "parsed_artifact_id": "parsed-1",
            "version_number": 1,
            "document_title": "Budget",
            "chunk_id": "chunk-1",
            "content": "The budget is 42 credits.",
            "content_hash": "a" * 64,
        }
        evidence_pack = graph.build_evidence_pack([source], [])

        with patch("openai.OpenAI", return_value=fake_client):
            proposal, input_tokens, output_tokens, provider_metadata = graph.generate_openai_grounded_proposal(
                {
                    "model": "gpt-test",
                    "query": "What is the budget?",
                    "user_id": "user",
                    "cancel_event": None,
                },
                evidence_pack,
                "What is the budget?",
            )

        self.assertEqual(proposal["claims"][0]["source_ids"], ["D1"])
        self.assertEqual((input_tokens, output_tokens), (9, 6))
        self.assertEqual(provider_metadata["response_id"], "resp_test")
        self.assertEqual(provider_metadata["returned_model"], "gpt-test-2026-08-30")
        self.assertEqual(provider_metadata["cached_input_tokens"], 2)
        self.assertNotIn("stream", captured)
        self.assertFalse(captured["store"])
        self.assertTrue(captured["text"]["format"]["strict"])
        self.assertEqual(captured["text"]["format"]["type"], "json_schema")

    def test_token_reservations_scale_down_for_small_daily_budgets(self):
        self.assertEqual(graph.token_reservation_for_budget(100_000), 10_000)
        self.assertEqual(graph.token_reservation_for_budget(5_000), 1_000)
        self.assertEqual(graph.token_reservation_for_budget(500), 500)
        with self.assertRaises(ValueError):
            graph.token_reservation_for_budget(0)

    def test_streams_node_updates_before_the_final_response(self):
        router_event = agent_event("router")
        executor_event = agent_event("executor")
        updates = iter([
            {"router": {"model": "local-extractive", "agent_events": [router_event]}},
            {
                "executor": {
                    "response": "Grounded answer",
                    "citations": [],
                    "prompt_tokens": 2,
                    "completion_tokens": 2,
                    "agent_events": [router_event, executor_event],
                }
            },
        ])
        emitted = []
        persisted_statuses = []

        with (
            patch.object(graph, "_claim_agent_run", return_value=None),
            patch.object(graph.app_graph, "stream", return_value=updates),
            patch.object(
                graph,
                "_persist_agent_run",
                side_effect=lambda _state, _run_id, _latency, status, *_args: persisted_statuses.append(status),
            ),
        ):
            result = graph.MultiAgentOrchestrator.execute_streaming(
                "question",
                "tenant",
                "user",
                "session",
                None,
                "6cb4e175-ed85-45e9-a98e-b47a84094769",
                emitted.append,
                Event(),
            )

        self.assertEqual(
            [event["type"] for event in emitted],
            ["agent_event", "agent_event", "token", "done"],
        )
        self.assertEqual(emitted[0]["agent"], "router")
        self.assertEqual(emitted[-1]["run_id"], "6cb4e175-ed85-45e9-a98e-b47a84094769")
        self.assertEqual(result["response"], "Grounded answer")
        self.assertEqual(persisted_statuses, ["running", "running", "completed"])

    def test_disconnect_stops_the_graph_and_records_interruption(self):
        cancelled = Event()
        first_event = agent_event("router")

        def updates():
            yield {"router": {"agent_events": [first_event]}}
            cancelled.set()
            yield {"planner": {"agent_events": [first_event, agent_event("planner")]}}

        persisted_statuses = []
        with (
            patch.object(graph, "_claim_agent_run", return_value=None),
            patch.object(graph.app_graph, "stream", return_value=updates()),
            patch.object(
                graph,
                "_persist_agent_run",
                side_effect=lambda _state, _run_id, _latency, status, *_args: persisted_statuses.append(status),
            ),
        ):
            with self.assertRaises(graph.ChatRunCancelled):
                graph.MultiAgentOrchestrator.execute_streaming(
                    "question",
                    "tenant",
                    "user",
                    "session",
                    None,
                    "887f1802-16c0-4e6f-971b-a93ccf9c307a",
                    lambda _event: None,
                    cancelled,
                )

        self.assertEqual(persisted_statuses, ["running", "interrupted"])


class RuntimeResourceContractTests(unittest.TestCase):
    def tearDown(self):
        runtime_module.close_runtime_resources()
        graphrag_module.close_graph_driver()

    def test_openai_client_is_reused_and_closed_once_per_immutable_configuration(self):
        client = MagicMock()
        with patch("openai.OpenAI", return_value=client) as constructor:
            first = runtime_module.get_openai_client("embedding", "key", 10, 1)
            second = runtime_module.get_openai_client("embedding", "key", 10, 1)

        self.assertIs(first, second)
        constructor.assert_called_once_with(api_key="key", timeout=10, max_retries=1)
        runtime_module.close_runtime_resources()
        client.close.assert_called_once()

    def test_database_pool_resets_transaction_and_returns_connection(self):
        connection = MagicMock()
        connection.closed = 0
        pool = MagicMock()
        pool.getconn.return_value = connection

        with patch.object(db_module, "_get_pool", return_value=pool):
            with db_module.get_db_connection() as yielded:
                self.assertIs(yielded, connection)

        connection.rollback.assert_called_once()
        connection.commit.assert_called_once()
        pool.putconn.assert_called_once_with(connection, close=False)

    def test_database_pool_rolls_back_before_reuse_after_failure(self):
        connection = MagicMock()
        connection.closed = 0
        pool = MagicMock()
        pool.getconn.return_value = connection

        with patch.object(db_module, "_get_pool", return_value=pool):
            with self.assertRaisesRegex(RuntimeError, "boom"):
                with db_module.get_db_connection():
                    raise RuntimeError("boom")

        self.assertEqual(connection.rollback.call_count, 2)
        connection.commit.assert_not_called()
        pool.putconn.assert_called_once_with(connection, close=False)

    def test_database_pool_discards_a_connection_that_cannot_be_reset(self):
        connection = MagicMock()
        connection.closed = 0
        connection.rollback.side_effect = RuntimeError("broken transaction")
        pool = MagicMock()
        pool.getconn.return_value = connection
        slots = MagicMock()
        slots.acquire.return_value = True

        with (
            patch.object(db_module, "_get_pool", return_value=pool),
            patch.object(db_module, "_pool_slots", slots),
            self.assertRaisesRegex(RuntimeError, "broken transaction"),
        ):
            db_module.acquire_db_connection()

        pool.putconn.assert_called_once_with(connection, close=True)
        slots.release.assert_called_once()

    def test_neo4j_driver_is_reused_and_closed_once_per_process(self):
        driver = MagicMock()
        with patch("neo4j.GraphDatabase.driver", return_value=driver) as constructor:
            first = graphrag_module.get_graph_driver()
            second = graphrag_module.get_graph_driver()

        self.assertIs(first, second)
        constructor.assert_called_once_with(
            graphrag_module.NEO4J_URI,
            auth=(graphrag_module.NEO4J_USER, graphrag_module.NEO4J_PASS),
            connection_timeout=graphrag_module.NEO4J_CONNECTION_TIMEOUT_SECONDS,
            connection_acquisition_timeout=(
                graphrag_module.NEO4J_CONNECTION_ACQUIRE_TIMEOUT_SECONDS
            ),
            max_connection_pool_size=graphrag_module.NEO4J_MAX_CONNECTION_POOL_SIZE,
        )
        graphrag_module.close_graph_driver()
        driver.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
