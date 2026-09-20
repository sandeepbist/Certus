import unittest
from pathlib import Path
from unittest.mock import patch

from services.orchestration.app.retrieval import graphrag
from services.orchestration.app.retrieval.graphrag import GraphRAGEngine


class OrchestrationDiagnosticBoundaryTests(unittest.TestCase):
    def test_graph_retrieval_drops_exception_instance_data(self):
        secret = "neo4j://user:private-password@graph.internal"

        with (
            patch.object(
                graphrag,
                "get_graph_driver",
                side_effect=RuntimeError(secret),
            ),
            self.assertLogs("graphrag", level="WARNING") as captured,
        ):
            result = GraphRAGEngine.traverse_graph(
                ["certus"],
                "user",
                "tenant",
            )

        diagnostics = "\n".join(captured.output)
        self.assertEqual(
            result,
            {
                "graph_triples": [],
                "connected_entities": [],
                "relationships_count": 0,
            },
        )
        self.assertIn("GraphRAG traversal failed (RuntimeError)", diagnostics)
        self.assertNotIn(secret, diagnostics)
        self.assertNotIn("private-password", diagnostics)

    def test_runtime_has_no_raw_infrastructure_diagnostic_sinks(self):
        source_paths = (
            "services/orchestration/app/agents/graph.py",
            "services/orchestration/app/api/chat.py",
            "services/orchestration/app/api/tasks.py",
            "services/orchestration/app/core/runtime.py",
            "services/orchestration/app/main.py",
            "services/orchestration/app/retrieval/graphrag.py",
            "services/orchestration/app/retrieval/hybrid.py",
            "services/orchestration/app/retrieval/memory.py",
            "services/orchestration/app/retrieval/webhook_delivery.py",
            "services/shared/webhooks.py",
        )
        forbidden_sinks = (
            "logger.exception",
            "exc_info=True",
            'error_message = str(error)',
            'logger.warning("Shared query embedding unavailable: %s", error)',
            'logger.warning("%s retrieval branch degraded: %s", name, error)',
            'logger.warning("Long-term memory retrieval degraded: %s", error)',
            'logger.warning(f"GraphRAG traversal error: {e}")',
        )

        for source_path in source_paths:
            source = Path(source_path).read_text(encoding="utf-8")
            with self.subTest(source_path=source_path):
                for forbidden_sink in forbidden_sinks:
                    self.assertNotIn(forbidden_sink, source)


if __name__ == "__main__":
    unittest.main()
