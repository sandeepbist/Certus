import io
import hashlib
import unittest

import pymupdf

from services.ingestion.app.layout_evidence import (
    LayoutEvidenceIntegrityError,
    resolve_pdf_visual_target,
)
from services.ingestion.app.parsers.factory import PDFParser


class MemoryLayoutStorage:
    def __init__(self, content: bytes):
        self.content = content

    def download_verified_to_spool(self, _stored):
        return io.BytesIO(self.content)


def layout_row(document, **overrides):
    layout = document.pdf_layout
    assert layout is not None
    row = {
        "layout_status": "ready",
        "layout_bucket": "private",
        "layout_object_key": "layout",
        "layout_object_version_id": "version-1",
        "layout_byte_length": layout.byte_length,
        "layout_uncompressed_byte_length": layout.uncompressed_byte_length,
        "layout_content_sha256": layout.content_sha256,
        "layout_canonical_content_sha256": layout.canonical_content_sha256,
        "layout_checksum_sha256_base64": layout.checksum_sha256_base64,
        "layout_etag": None,
        "layout_storage_class": None,
        "layout_server_side_encryption": None,
        "layout_kms_key_id": None,
        "layout_bucket_key_enabled": None,
        "layout_producer_profile": layout.producer_profile,
        "layout_page_count": len(layout.pages),
        "layout_artifact_id": "00000000-0000-4000-8000-000000000001",
        "start_char": 0,
        "end_char": len(document.raw_text.rstrip("\n")),
        "parsed_content_text": document.raw_text,
        "parsed_content_sha256": hashlib.sha256(
            document.raw_text.encode("utf-8")
        ).hexdigest(),
        "original_content_sha256": hashlib.sha256(
            SOURCE_PDF
        ).hexdigest(),
    }
    row.update(overrides)
    return row


source = pymupdf.open()
page = source.new_page(width=300, height=500)
page.insert_text((72, 72), "Rotated proof")
page.set_rotation(90)
SOURCE_PDF = source.tobytes()
source.close()


class PdfLayoutEvidenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.document = PDFParser().parse(SOURCE_PDF, "proof.pdf")
        assert cls.document.pdf_layout is not None

    def test_resolves_verified_native_glyph_quads(self):
        layout = self.document.pdf_layout
        assert layout is not None

        visual = resolve_pdf_visual_target(
            layout_row(self.document),
            MemoryLayoutStorage(layout.compressed_bytes),
        )

        self.assertEqual(visual["status"], "verified")
        self.assertEqual(visual["selector"]["granularity"], "nativeGlyphQuad")
        self.assertEqual(visual["selector"]["pages"][0]["rotation_degrees"], 90)
        glyphs = visual["selector"]["pages"][0]["runs"][0]["glyphs"]
        self.assertEqual(len(glyphs), len("Rotated proof"))
        self.assertTrue(all(len(glyph["quad"]) == 8 for glyph in glyphs))

    def test_fails_closed_on_canonical_digest_mismatch(self):
        layout = self.document.pdf_layout
        assert layout is not None

        with self.assertRaises(LayoutEvidenceIntegrityError):
            resolve_pdf_visual_target(
                layout_row(self.document, layout_canonical_content_sha256="0" * 64),
                MemoryLayoutStorage(layout.compressed_bytes),
            )


if __name__ == "__main__":
    unittest.main()
