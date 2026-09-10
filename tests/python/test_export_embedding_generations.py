import sys
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
ORCHESTRATION_ROOT = REPOSITORY_ROOT / "services" / "orchestration"
sys.path.insert(0, str(ORCHESTRATION_ROOT))

from app.api import export as export_api
from app.core.identity import RequestIdentity

sys.path.remove(str(ORCHESTRATION_ROOT))
for module_name in list(sys.modules):
    if module_name == "app" or module_name.startswith("app."):
        del sys.modules[module_name]


IDENTITY = RequestIdentity(tenant_id="tenant-proof", user_id="user-proof")


class ExportCursor:
    def __init__(self):
        self.statements: list[tuple[str, tuple]] = []
        self.current = ""

    def execute(self, statement, params):
        self.current = statement
        self.statements.append((statement, params))

    def fetchone(self):
        return None

    def fetchall(self):
        if "FROM workspace_embedding_generations" in self.current:
            return [{
                "id": "generation",
                "embedding_profile": "profile",
                "status": "active",
            }]
        if "FROM chunk_embedding_vectors" in self.current:
            return [{
                "generation_id": "generation",
                "chunk_id": "chunk",
                "content_sha256": "a" * 64,
                "embedding_payload_included": False,
            }]
        return []


class ExportEmbeddingGenerationTests(unittest.TestCase):
    def test_schema_v10_exports_rebuild_provenance_without_shadow_vectors(self):
        cursor = ExportCursor()

        data = export_api._collect_export_data(cursor, IDENTITY)

        self.assertEqual(export_api.EXPORT_SCHEMA_VERSION, 10)
        self.assertEqual(data["embedding_generations"][0]["status"], "active")
        manifest = data["chunk_embedding_vector_manifests"][0]
        self.assertFalse(manifest["embedding_payload_included"])
        generation_sql = next(
            statement
            for statement, _ in cursor.statements
            if "FROM chunk_embedding_vectors" in statement
        )
        selected_columns = generation_sql.split("FROM chunk_embedding_vectors", 1)[0]
        self.assertNotIn("embedding::text", selected_columns)
        self.assertNotIn(" lease_owner", selected_columns)

    def test_profile_export_includes_generation_only_profiles(self):
        cursor = ExportCursor()

        export_api._collect_export_data(cursor, IDENTITY)

        profile_sql, params = next(
            (statement, params)
            for statement, params in cursor.statements
            if "FROM embedding_profiles AS profile" in statement
        )
        self.assertIn("FROM workspace_embedding_generations AS generation", profile_sql)
        self.assertEqual(params, ("tenant-proof", "user-proof") * 5)

    def test_manifest_explicitly_requires_rebuild_and_reevaluation(self):
        source = Path("services/orchestration/app/api/export.py").read_text(
            encoding="utf-8"
        )
        self.assertIn('"chunk_embedding_vectors.embedding"', source)
        self.assertIn("Rebuild from exact chunk membership", source)
        self.assertIn("then re-evaluate before activation", source)


if __name__ == "__main__":
    unittest.main()
