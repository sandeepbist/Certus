import json
import unittest
from datetime import datetime, timezone
from io import BytesIO
from uuid import UUID
from zipfile import ZipFile

from services.orchestration.app.retrieval.export_archive import (
    ARCHIVE_ENTRY_NAME,
    ExportArchiveLimitExceeded,
    ExportCollectionBudget,
    build_export_archive,
    record_counts,
)


class ExportArchiveTests(unittest.TestCase):
    def test_archive_is_valid_zip_with_json_safe_values(self):
        payload = {
            "manifest": {"exported_at": datetime(2026, 8, 24, tzinfo=timezone.utc)},
            "data": {"documents": [{"id": UUID("00000000-0000-0000-0000-000000000001")}]},
        }

        archive = build_export_archive(payload)

        with ZipFile(BytesIO(archive)) as zip_file:
            self.assertEqual(zip_file.namelist(), [ARCHIVE_ENTRY_NAME])
            decoded = json.loads(zip_file.read(ARCHIVE_ENTRY_NAME))
        self.assertEqual(decoded["data"]["documents"][0]["id"], "00000000-0000-0000-0000-000000000001")
        self.assertEqual(decoded["manifest"]["exported_at"], "2026-08-24T00:00:00+00:00")

    def test_record_counts_handles_singletons_and_collections(self):
        self.assertEqual(
            record_counts({"profile": {"id": "user"}, "documents": [{}, {}], "membership": None}),
            {"profile": 1, "documents": 2, "membership": 0},
        )

    def test_archive_streaming_enforces_uncompressed_limit(self):
        payload = {"data": {"documents": [{"content": "x" * 4_096}]}}

        with self.assertRaises(ExportArchiveLimitExceeded):
            build_export_archive(payload, max_uncompressed_bytes=1_024)

        archive = build_export_archive(payload, max_uncompressed_bytes=8_192)
        with ZipFile(BytesIO(archive)) as zip_file:
            decoded = json.loads(zip_file.read(ARCHIVE_ENTRY_NAME))
        self.assertEqual(decoded, payload)

    def test_collection_budget_commits_only_rows_within_both_limits(self):
        budget = ExportCollectionBudget(max_source_bytes=32, max_records=2)
        budget.add({"id": "one"})
        self.assertEqual((budget.records, budget.source_bytes), (1, 12))

        with self.assertRaises(ExportArchiveLimitExceeded):
            budget.add({"content": "x" * 32})
        self.assertEqual((budget.records, budget.source_bytes), (1, 12))

        budget.add({"id": "two"})
        with self.assertRaises(ExportArchiveLimitExceeded):
            budget.add({"id": "three"})
        self.assertEqual(budget.records, 2)


if __name__ == "__main__":
    unittest.main()
