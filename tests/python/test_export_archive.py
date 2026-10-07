import json
import hashlib
import unittest
from datetime import datetime, timezone
from io import BytesIO
from uuid import UUID
from zipfile import ZipFile

from services.orchestration.app.retrieval.export_archive import (
    ARCHIVE_ENTRY_NAME,
    ExportArchiveLimitExceeded,
    ExportCollectionBudget,
    ExportOriginal,
    ExportOriginalIntegrityError,
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

    def test_originals_are_exact_and_corrupt_or_oversized_streams_fail(self):
        content = b"exact original\x00\xff"
        digest = hashlib.sha256(content).hexdigest()

        def original(chunks, path="originals/version/source.bin"):
            return ExportOriginal(path, len(content), digest, chunks)

        archive = build_export_archive(
            {"manifest": {}}, originals=[original([content[:4], content[4:]])],
            max_original_bytes=len(content), max_archive_bytes=4096,
        )
        with ZipFile(BytesIO(archive)) as zip_file:
            self.assertEqual(zip_file.read("originals/version/source.bin"), content)
            self.assertIsNone(zip_file.testzip())
        for chunks in ([content[:-1]], [content + b"x"], [b"x" * len(content)]):
            with self.subTest(chunks=chunks), self.assertRaises(ExportOriginalIntegrityError):
                build_export_archive({}, originals=[original(chunks)], max_original_bytes=len(content))
        with self.assertRaises(ExportArchiveLimitExceeded):
            build_export_archive({}, originals=[original([content])], max_original_bytes=len(content) - 1)
        with self.assertRaises(ExportArchiveLimitExceeded):
            build_export_archive({}, max_archive_bytes=20)
        for path in ("../original.bin", "/original.bin", "a\\original.bin", ARCHIVE_ENTRY_NAME):
            with self.subTest(path=path), self.assertRaises(ExportOriginalIntegrityError):
                build_export_archive({}, originals=[original([content], path)], max_original_bytes=len(content))


if __name__ == "__main__":
    unittest.main()
