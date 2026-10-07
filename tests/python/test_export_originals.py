import hashlib
import json
import sys
import unittest
from contextlib import contextmanager
from io import BytesIO
from pathlib import Path
from unittest.mock import patch
from zipfile import ZipFile

import httpx
from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "services" / "orchestration"))
from app.api import export as export_api
from app.core.identity import RequestIdentity
from app.retrieval import export_originals
sys.path.pop(0)
for name in list(sys.modules):
    if name == "app" or name.startswith("app."):
        del sys.modules[name]

IDENTITY = RequestIdentity(tenant_id="tenant-proof", user_id="user-proof")
DOCUMENT = "00000000-0000-4000-8000-000000000001"
CONTENT = b"exact source bytes\x00\xff"


def fixture():
    versions = []
    sources = []
    for number in range(1, 4):
        version_id = f"00000000-0000-4000-8000-{number + 10:012d}"
        versions.append({"id": version_id, "document_id": DOCUMENT, "version_number": number})
        sources.append({
            "id": f"00000000-0000-4000-8000-{number + 20:012d}",
            "document_version_id": version_id, "document_id": DOCUMENT,
            "tenant_id": IDENTITY.tenant_id, "user_id": IDENTITY.user_id,
            "original_filename": "../../unsafe-filename.pdf", "byte_length": len(CONTENT),
            "content_sha256": hashlib.sha256(CONTENT).hexdigest(), "storage_backend": "s3",
            "status": "unavailable" if number == 1 else "available",
        })
    return {"document_versions": versions, "document_source_objects": sources}


class Cursor:
    def __init__(self):
        self.statements = []

    def execute(self, query, params=None):
        self.statements.append((query, params))

    def fetchone(self):
        return {"acquired": True}


class ExportOriginalTests(unittest.TestCase):
    def generate(self, data, *, include=True, content=CONTENT, status=200):
        self.cursor = Cursor()
        self.requests = []

        @contextmanager
        def database():
            yield self.cursor

        def respond(request):
            self.requests.append(request)
            return httpx.Response(status, content=content)

        client = httpx.Client(transport=httpx.MockTransport(respond), trust_env=False)
        self.client = client

        def create_client(**kwargs):
            client.headers.update(kwargs["headers"])
            return client

        try:
            with patch.object(export_api, "get_db_cursor", database), \
                 patch.object(export_api, "_collect_export_data", return_value=data), \
                 patch.object(export_originals.settings, "INTERNAL_SERVICE_TOKEN", "test-internal-token-at-least-32-characters"), \
                 patch.object(export_originals.httpx, "Client", side_effect=create_client) as factory:
                result = export_api.generate_data_export(IDENTITY, include_originals=include)
                if not include:
                    factory.assert_not_called()
            return result
        finally:
            self.closed_by_export = client.is_closed
            client.close()

    def saved_archives(self):
        return [params[5] for query, params in self.cursor.statements if "INSERT INTO data_exports" in query]

    def test_archive_preserves_versions_and_labels_legacy_exclusion(self):
        result = self.generate(fixture())
        self.assertEqual(result["originals"]["included_count"], 2)
        self.assertEqual(result["originals"]["excluded_count"], 1)
        self.assertFalse(result["originals"]["complete"])
        with ZipFile(BytesIO(self.saved_archives()[0])) as archive:
            manifest = json.loads(archive.read("certus-export.json"))["manifest"]
            self.assertEqual(manifest["schema_version"], 13)
            files = manifest["originals"]["files"]
            self.assertEqual(files[0]["reason"], "unavailable")
            self.assertIsNone(files[0]["path"])
            for entry in files[1:]:
                self.assertEqual(archive.read(entry["path"]), CONTENT)
                self.assertNotIn("..", entry["path"])
            self.assertIsNone(archive.testzip())
        self.assertEqual([request.url.params["version"] for request in self.requests], ["2", "3"])
        for request in self.requests:
            self.assertEqual(request.url.params["include_archived"], "true")
            self.assertEqual(request.headers["X-Certus-Tenant-Id"], IDENTITY.tenant_id)
            self.assertEqual(request.headers["X-Certus-User-Id"], IDENTITY.user_id)
            self.assertEqual(request.headers["X-Internal-Service-Token"], "test-internal-token-at-least-32-characters")
        self.assertTrue(self.closed_by_export)

    def test_download_failure_or_corruption_never_saves_partial_archive(self):
        for content, status, expected in ((b"bad", 200, 502), (CONTENT, 404, 503)):
            with self.subTest(status=status), self.assertRaises(HTTPException) as raised:
                self.generate(fixture(), content=content, status=status)
            self.assertEqual(raised.exception.status_code, expected)
            self.assertEqual(self.saved_archives(), [])
            self.assertTrue(self.closed_by_export)

    def test_scope_and_capacity_fail_before_download_or_persistence(self):
        data = fixture()
        data["document_source_objects"][1]["tenant_id"] = "other-tenant"
        with self.assertRaises(HTTPException) as raised:
            self.generate(data)
        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(self.requests, [])
        with patch.object(export_api.settings, "MAX_EXPORT_ORIGINAL_BYTES", len(CONTENT)):
            with self.assertRaises(HTTPException) as raised:
                self.generate(fixture())
        self.assertEqual(raised.exception.status_code, 413)
        self.assertNotIn("Use the asynchronous", raised.exception.detail)
        self.assertEqual(self.requests, [])
        self.assertEqual(self.saved_archives(), [])

    def test_metadata_only_export_is_explicit_and_needs_no_original_service(self):
        result = self.generate(fixture(), include=False)
        self.assertFalse(result["originals"]["requested"])
        self.assertEqual(result["originals"]["included_count"], 0)
        self.assertEqual(result["originals"]["excluded_count"], 3)
        with ZipFile(BytesIO(self.saved_archives()[0])) as archive:
            self.assertEqual(archive.namelist(), ["certus-export.json"])

    def test_unexpected_catalog_failure_and_build_deadline_abort(self):
        data = fixture()
        data["document_source_objects"][1]["status"] = "missing"
        with self.assertRaises(HTTPException) as raised:
            self.generate(data)
        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(self.requests, [])
        with patch.object(export_api, "monotonic", side_effect=[0, 1000]):
            with self.assertRaises(HTTPException) as raised:
                self.generate(fixture())
        self.assertEqual(raised.exception.status_code, 504)
        self.assertEqual(self.saved_archives(), [])


if __name__ == "__main__":
    unittest.main()
