import unittest

from services.orchestration.app.retrieval.graph_view import project_workspace_graph


ROWS = [
    {
        "document_id": "doc-1",
        "document_title": "Architecture",
        "document_created_at": "2026-08-24T00:00:00Z",
        "normalized_name": "postgresql",
        "entity_name": "PostgreSQL",
        "entity_type": "TECHNOLOGY",
        "frequency": 3,
    },
    {
        "document_id": "doc-1",
        "document_title": "Architecture",
        "document_created_at": "2026-08-24T00:00:00Z",
        "normalized_name": "pgvector",
        "entity_name": "pgvector",
        "entity_type": "TECHNOLOGY",
        "frequency": 2,
    },
    {
        "document_id": "doc-2",
        "document_title": "Unrelated",
        "document_created_at": "2026-08-23T00:00:00Z",
        "normalized_name": "pgvector",
        "entity_name": "pgvector",
        "entity_type": "TECHNOLOGY",
        "frequency": 1,
    },
    {
        "document_id": "doc-2",
        "document_title": "Unrelated",
        "document_created_at": "2026-08-23T00:00:00Z",
        "normalized_name": "temporal",
        "entity_name": "Temporal",
        "entity_type": "TECHNOLOGY",
        "frequency": 1,
    },
]


class WorkspaceGraphProjectionTests(unittest.TestCase):
    def test_query_builds_a_bounded_tenant_graph_around_matching_nodes(self):
        result = project_workspace_graph(ROWS, query="PostgreSQL", depth=1, node_limit=20)
        names = {node["name"] for node in result["nodes"]}

        self.assertEqual(names, {"PostgreSQL", "pgvector", "Architecture"})
        self.assertEqual(len(result["links"]), 2)
        self.assertEqual(result["stats"]["documents"], 1)

    def test_empty_query_returns_global_graph_with_aggregated_mentions(self):
        result = project_workspace_graph(ROWS, node_limit=20)
        postgres = next(node for node in result["nodes"] if node["name"] == "PostgreSQL")

        self.assertEqual(postgres["mention_count"], 3)
        self.assertEqual(postgres["document_count"], 1)
        self.assertEqual(result["stats"], {"nodes": 5, "links": 4, "entities": 3, "documents": 2})

    def test_depth_expands_across_shared_entities_without_crossing_unrelated_data(self):
        result = project_workspace_graph(ROWS, query="PostgreSQL", depth=2, node_limit=20)
        names = {node["name"] for node in result["nodes"]}

        self.assertEqual(names, {"PostgreSQL", "pgvector", "Temporal", "Architecture", "Unrelated"})

    def test_node_limit_marks_the_projection_as_truncated(self):
        result = project_workspace_graph(ROWS, node_limit=2)

        self.assertEqual(len(result["nodes"]), 2)
        self.assertTrue(result["truncated"])


if __name__ == "__main__":
    unittest.main()
