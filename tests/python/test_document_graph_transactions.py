import unittest
from datetime import datetime, timezone
from uuid import uuid4

from services.shared.document_graph import EntitySnapshot, UnavailableSnapshot, _target_from_row, _token


class DocumentGraphSnapshotTests(unittest.TestCase):
    def row(self, entities):
        document_id, version_id = uuid4(), uuid4()
        return {"document_id": document_id, "current_version_id": version_id,
                "tenant_id": "tenant", "user_id": "user", "revision": 1,
                "title": "Proof", "created_at": datetime.now(timezone.utc), "deleted_at": None,
                "entity_count": len(entities) if isinstance(entities, list) else 1,
                "source_payload": {"document_id": str(document_id), "document_version_id": str(version_id),
                                   "entity_count": len(entities) if isinstance(entities, list) else 1,
                                   "entities": entities}}

    def test_empty_is_available_but_missing_malformed_and_wrong_version_are_unavailable(self):
        self.assertEqual(_target_from_row(self.row([])).snapshot, EntitySnapshot(()))
        for entities in (None, {}, [{"name": "Proof", "type": "CONCEPT", "mention_count": True}],
                         [{"name": "Proof", "type": "CONCEPT", "mention_count": 0}],
                         [{"name": "x" * 513, "type": "CONCEPT", "mention_count": 1}]):
            with self.subTest(entities=entities):
                self.assertIsInstance(_target_from_row(self.row(entities)).snapshot, UnavailableSnapshot)
        row = self.row([])
        row["source_payload"]["document_version_id"] = str(uuid4())
        self.assertIsInstance(_target_from_row(row).snapshot, UnavailableSnapshot)
        row = self.row([])
        del row["source_payload"]["entities"]
        self.assertIsInstance(_target_from_row(row).snapshot, UnavailableSnapshot)

    def test_duplicate_normalized_entities_are_rejected_and_snapshots_are_immutable(self):
        entity = {"name": "Straße", "type": "CONCEPT", "mention_count": 2}
        row = self.row([entity, {**entity, "name": "STRASSE"}])
        self.assertIsInstance(_target_from_row(row).snapshot, UnavailableSnapshot)
        row = self.row([entity])
        target = _target_from_row(row)
        entity["name"] = "changed input"
        self.assertEqual(target.snapshot.entities[0].name, "Straße")

    def test_unstamped_or_invalid_projection_rows_have_no_authorization_token(self):
        row = {"document_id": str(uuid4()), "document_version_id": str(uuid4()), "graph_revision": 1}
        self.assertIsNotNone(_token(row))
        for value in (None, True, 0, "1"):
            self.assertIsNone(_token({**row, "graph_revision": value}))
        self.assertIsNone(_token({**row, "document_id": "not-a-uuid"}))


if __name__ == "__main__":
    unittest.main()
