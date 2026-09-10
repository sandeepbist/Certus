import unittest
import gzip
import hashlib
import json
from unittest.mock import MagicMock, Mock, patch
from datetime import datetime, timezone

from services.ingestion.app.chunking.chunker import (
    ChunkerFactory,
    RecursiveChunker,
    SentenceWindowChunker,
    TokenChunker,
)
from services.ingestion.app.extractors.entity_extractor import (
    MAX_EXTRACTED_ENTITIES,
    NEO4J_ACQUISITION_TIMEOUT_SECONDS,
    NEO4J_CONNECT_TIMEOUT_SECONDS,
    NEO4J_POOL_SIZE,
    NEO4J_QUERY_TIMEOUT_SECONDS,
    EntityExtractor,
    close_entity_graph_driver,
    entity_graph_driver,
)
from services.ingestion.app.extractors import entity_extractor as entity_extractor_module
from services.ingestion.app.evidence import EvidenceIntegrityError, build_evidence_envelope
from services.ingestion.app.parsers.factory import (
    DocxParser,
    MarkdownParser,
    PDFParser,
    ParserException,
    ParserFactory,
    TextParser,
)
from services.ingestion.app.processing import (
    chunker_profile,
    embedding_job_ranges,
    parse_source_time,
    parser_profile,
    sha256_text,
)
from services.ingestion.app.originals import (
    UploadIdempotencyConflict,
    canonical_idempotency_key,
    original_attachment_header,
    original_content_disposition,
)


class ParserSafetyTests(unittest.TestCase):
    def test_rejects_binary_disguised_as_text(self):
        with self.assertRaises(ParserException):
            TextParser().parse(b"MZ\x00\x01binary", "malware.txt")

    def test_rejects_invalid_pdf_and_docx_signatures(self):
        with self.assertRaises(ParserException):
            PDFParser().parse(b"not a pdf", "fake.pdf")
        with self.assertRaises(ParserException):
            DocxParser().parse(b"not a docx", "fake.docx")

    def test_rejects_unsupported_extension_even_with_text_mime(self):
        with self.assertRaises(ParserException):
            ParserFactory.get_parser("text/plain", "payload.exe")

    def test_text_and_markdown_sections_are_exact_parsed_artifact_spans(self):
        text_document = TextParser().parse(b"  Alpha\nBeta  \n", "proof.txt")
        markdown_document = MarkdownParser().parse(
            b"  Preamble proof.\n# First\n\n  Alpha\nBeta  \n\n## Second\nGamma\n",
            "proof.md",
        )

        self.assertEqual(text_document.raw_text, "  Alpha\nBeta  \n")
        for document in (text_document, markdown_document):
            for section in document.sections:
                self.assertEqual(
                    document.raw_text[section.start_char:section.end_char],
                    section.content,
                )
        self.assertEqual(markdown_document.sections[0].title, "Preamble")
        self.assertEqual(markdown_document.sections[0].content, "Preamble proof.")

    def test_pdf_preserves_physical_pages_with_a_versioned_separator(self):
        import pymupdf

        source = pymupdf.open()
        first = source.new_page()
        first.insert_text((72, 72), "First page proof")
        source.new_page()
        third = source.new_page()
        third.insert_text((72, 72), "Third page proof")
        content = source.tobytes()
        source.close()

        document = PDFParser().parse(content, "proof.pdf")

        self.assertEqual(document.metadata["page_count"], 3)
        self.assertEqual(document.metadata["pages_with_text"], 2)
        self.assertEqual(
            document.metadata["page_join_contract"],
            "physical_pages_form_feed:v1",
        )
        self.assertEqual(document.raw_text.count("\f"), 2)
        self.assertEqual(len(document.sections), 3)
        for section in document.sections:
            self.assertEqual(
                document.raw_text[section.start_char:section.end_char],
                section.content,
            )
        self.assertIsNotNone(document.pdf_layout)
        layout = document.pdf_layout
        assert layout is not None
        self.assertEqual(len(layout.pages), 3)
        self.assertEqual(layout.pages[1].extraction_status, "empty")
        self.assertEqual(layout.pages[1].parsed_start, layout.pages[1].parsed_end)
        self.assertEqual(gzip.decompress(layout.compressed_bytes), layout.canonical_bytes)
        self.assertEqual(
            hashlib.sha256(layout.canonical_bytes).hexdigest(),
            layout.canonical_content_sha256,
        )
        payload = json.loads(layout.canonical_bytes)
        self.assertEqual(payload["parsed_text"]["content_sha256"], sha256_text(document.raw_text))
        for run in layout.text_runs:
            self.assertEqual(
                hashlib.sha256(
                    document.raw_text[run.parsed_start:run.parsed_end].encode("utf-8")
                ).hexdigest(),
                run.text_sha256,
            )

    def test_pdf_spatial_artifact_is_deterministic_and_uses_unrotated_coordinates(self):
        import pymupdf

        source = pymupdf.open()
        page = source.new_page(width=300, height=500)
        page.insert_text((72, 72), "Rotated proof")
        page.set_rotation(90)
        content = source.tobytes()
        source.close()

        first = PDFParser().parse(content, "rotated.pdf")
        second = PDFParser().parse(content, "rotated.pdf")
        assert first.pdf_layout is not None and second.pdf_layout is not None

        self.assertEqual(first.raw_text, "Rotated proof\n")
        self.assertEqual(first.pdf_layout.canonical_bytes, second.pdf_layout.canonical_bytes)
        self.assertEqual(first.pdf_layout.compressed_bytes, second.pdf_layout.compressed_bytes)
        page_layout = first.pdf_layout.pages[0]
        self.assertEqual(page_layout.rotation_degrees, 90)
        self.assertEqual((page_layout.width_points, page_layout.height_points), (300.0, 500.0))
        self.assertTrue(first.pdf_layout.text_runs)
        run = first.pdf_layout.text_runs[0]
        self.assertGreaterEqual(run.bbox[0], 0.0)
        self.assertLessEqual(run.bbox[2], page_layout.width_points)


class ChunkingBoundaryTests(unittest.TestCase):
    def test_sentence_windows_are_named_truthfully_and_legacy_alias_is_normalized(self):
        self.assertEqual(ChunkerFactory.normalize_strategy("semantic"), "sentence")
        self.assertIsInstance(ChunkerFactory.get_chunker("sentence"), SentenceWindowChunker)
        with self.assertRaises(ValueError):
            ChunkerFactory.get_chunker("unknown")

    def test_embedding_job_ranges_are_complete_and_non_overlapping(self):
        self.assertEqual(
            list(embedding_job_ranges(45, batch_size=20)),
            [(0, 20), (20, 40), (40, 45)],
        )
        with self.assertRaises(ValueError):
            list(embedding_job_ranges(0))

    def test_single_oversized_sentence_is_bounded(self):
        content = " ".join(f"word{index}" for index in range(1200))
        chunks = TokenChunker(target_chunk_tokens=400, overlap_tokens=50).chunk(
            content,
            "large.txt",
        )

        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(chunk.token_count <= 400 for chunk in chunks))

    def test_token_chunks_preserve_exact_source_spans_with_overlap_and_whitespace(self):
        content = "  One.\n\nTwo.   Three four five.\nSix seven eight.  "
        chunks = TokenChunker(target_chunk_tokens=4, overlap_tokens=2).chunk(
            content,
            "proof.txt",
        )

        self.assertGreater(len(chunks), 2)
        self.assertTrue(any(chunks[index + 1].start_char < chunk.end_char for index, chunk in enumerate(chunks[:-1])))
        for chunk in chunks:
            self.assertEqual(content[chunk.start_char:chunk.end_char], chunk.content)

    def test_recursive_chunks_preserve_exact_unicode_source_spans(self):
        content = "  Delta Δ keeps spacing 😀.\n\nSecond paragraph has cafe\u0301 and proof.  "
        chunks = RecursiveChunker(max_tokens=5, overlap_tokens=1).chunk(
            content,
            "proof.txt",
        )

        self.assertGreaterEqual(len(chunks), 2)
        for chunk in chunks:
            self.assertEqual(content[chunk.start_char:chunk.end_char], chunk.content)


class ProcessingProvenanceTests(unittest.TestCase):
    def test_source_time_requires_an_explicit_timezone_and_normalizes_to_utc(self):
        parsed, origin = parse_source_time("2026-08-28T10:15:00+05:30")

        self.assertEqual(parsed, datetime(2026, 8, 28, 4, 45, tzinfo=timezone.utc))
        self.assertEqual(origin, "user_provided")
        self.assertEqual(parse_source_time(None), (None, "unspecified"))
        with self.assertRaises(ValueError):
            parse_source_time("2026-08-28T10:15:00")

    def test_text_hash_and_producer_profiles_are_deterministic(self):
        self.assertEqual(
            sha256_text("Certus"),
            "8ddf71771bc2b530a40b7bc215c4e3404dd71822752c03da8610806ee0c1821a",
        )


class EntityProjectionTruthTests(unittest.TestCase):
    def test_entity_projection_distinguishes_empty_from_unavailable(self):
        graph_driver = MagicMock()
        session = graph_driver.session.return_value.__enter__.return_value
        session.run.return_value = []
        with patch.object(
            entity_extractor_module,
            "entity_graph_driver",
            return_value=graph_driver,
        ):
            entities, status = EntityExtractor.list_document_entities(
                "document",
                "user",
                "tenant",
            )
        self.assertEqual(entities, [])
        self.assertEqual(status, "ready")

        with patch.object(
            entity_extractor_module,
            "entity_graph_driver",
            side_effect=RuntimeError("graph unavailable"),
        ):
            entities, status = EntityExtractor.list_document_entities(
                "document",
                "user",
                "tenant",
            )
        self.assertEqual(entities, [])
        self.assertEqual(status, "degraded")


class OriginalDeliveryContractTests(unittest.TestCase):
    def test_idempotency_keys_are_canonical_uuids(self):
        self.assertEqual(
            canonical_idempotency_key("11111111-1111-4111-8111-111111111111"),
            "11111111-1111-4111-8111-111111111111",
        )
        with self.assertRaises(UploadIdempotencyConflict):
            canonical_idempotency_key("not-a-uuid")

    def test_attachment_header_has_ascii_fallback_and_encoded_utf8_name(self):
        header = original_attachment_header('proof "Δ".pdf')

        self.assertIn('filename="proof ___.pdf"', header)
        self.assertIn("filename*=UTF-8''proof%20%22%CE%94%22.pdf", header)
        self.assertNotIn("\r", header)
        self.assertNotIn("\n", header)
        self.assertTrue(
            original_content_disposition("proof.pdf", "inline").startswith("inline;")
        )
        with self.assertRaises(ValueError):
            original_content_disposition("proof.pdf", "unsafe")
        self.assertEqual(
            parser_profile(TextParser(), "text"),
            {
                "schema_version": 2,
                "pipeline_version": 2,
                "implementation": "certus.ingestion.parsers.TextParser",
                "source_format": "text",
                "text_offset_unit": "unicode_code_point",
                "text_range_semantics": "zero_based_half_open",
                "library_versions": {},
                "build_revision": "unversioned-development",
            },
        )
        self.assertEqual(
            chunker_profile(
                "token",
                TokenChunker(target_chunk_tokens=400, overlap_tokens=50),
            ),
            {
                "schema_version": 2,
                "pipeline_version": 2,
                "strategy": "token",
                "implementation": "certus.ingestion.chunkers.TokenChunker",
                "token_estimator": "whitespace_words_x_1.3_floor:v1",
                "sentence_splitter": "source_preserving_punctuation_spans:v2",
                "text_offset_unit": "unicode_code_point",
                "text_range_semantics": "zero_based_half_open",
                "exact_source_spans": True,
                "parameters": {
                    "target_chunk_tokens": 400,
                    "overlap_tokens": 50,
                },
                "build_revision": "unversioned-development",
            },
        )


class ExactEvidenceEnvelopeTests(unittest.TestCase):
    @staticmethod
    def evidence_row(**overrides):
        row = {
            "chunk_id": "00000000-0000-4000-8000-000000000001",
            "document_id": "00000000-0000-4000-8000-000000000002",
            "document_version_id": "00000000-0000-4000-8000-000000000003",
            "document_title": "Proof",
            "version_number": 4,
            "version_content_hash": "a" * 64,
            "is_current_version": False,
            "source_time": None,
            "recorded_at": datetime(2026, 8, 29, tzinfo=timezone.utc),
            "derivation_id": "00000000-0000-4000-8000-000000000004",
            "parsed_artifact_id": "00000000-0000-4000-8000-000000000005",
            "parsed_status": "ready",
            "parsed_content_sha256": "b" * 64,
            "source_object_id": "00000000-0000-4000-8000-000000000006",
            "original_status": "available",
            "original_filename": "proof.txt",
            "source_mime_type": "text/plain",
            "original_byte_length": 10,
            "original_content_sha256": "a" * 64,
            "parser_profile": {"pipeline_version": 2},
            "chunker_profile": {"pipeline_version": 2},
            "text_locator_status": "exact",
            "text_locator_profile": "unicode_code_point:zero_based_half_open:v1",
            "text_locator_unavailable_reason": None,
            "start_char": 2,
            "end_char": 9,
            "chunk_content": "😀 cafe\u0301",
            "resolved_quote": "😀 cafe\u0301",
            "resolved_prefix": "A ",
            "resolved_suffix": " Z",
            "page_number": 1,
            "section_title": "Proof",
        }
        row.update(overrides)
        return row

    def test_builds_redundant_position_and_quote_selectors(self):
        envelope = build_evidence_envelope(self.evidence_row())

        self.assertEqual(envelope["resolution_status"], "verified")
        self.assertEqual(
            envelope["text_target"]["selector"][0],
            {
                "type": "TextPositionSelector",
                "start": 2,
                "end": 9,
                "unit": "unicodeCodePoint",
            },
        )
        self.assertEqual(
            envelope["text_target"]["selector"][1]["exact"],
            "😀 cafe\u0301",
        )
        self.assertEqual(envelope["visual_target"]["status"], "not_applicable")

    def test_reports_legacy_locator_unavailable_without_guessing(self):
        envelope = build_evidence_envelope(
            self.evidence_row(
                text_locator_status="unavailable",
                text_locator_profile="legacy_unavailable:v0",
                text_locator_unavailable_reason="Legacy offsets were synthetic.",
                start_char=None,
                end_char=None,
                resolved_quote=None,
                resolved_prefix=None,
                resolved_suffix=None,
            )
        )

        self.assertEqual(envelope["resolution_status"], "unavailable")
        self.assertIsNone(envelope["text_target"])

    def test_fails_closed_when_resolved_quote_disagrees_with_chunk(self):
        with self.assertRaises(EvidenceIntegrityError):
            build_evidence_envelope(self.evidence_row(resolved_quote="wrong"))


class EntityExtractionTests(unittest.TestCase):
    def test_graph_driver_is_bounded_reused_and_closed(self):
        driver = Mock()
        close_entity_graph_driver()
        try:
            with patch.object(
                entity_extractor_module.GraphDatabase,
                "driver",
                return_value=driver,
            ) as create_driver:
                self.assertIs(entity_graph_driver(), driver)
                self.assertIs(entity_graph_driver(), driver)

            create_driver.assert_called_once_with(
                entity_extractor_module.os.getenv("NEO4J_URI", "bolt://localhost:7687"),
                auth=(
                    entity_extractor_module.os.getenv("NEO4J_USER", "neo4j"),
                    entity_extractor_module.os.getenv(
                        "NEO4J_PASSWORD", "nexus_neo4j_dev"
                    ),
                ),
                connection_timeout=NEO4J_CONNECT_TIMEOUT_SECONDS,
                connection_acquisition_timeout=NEO4J_ACQUISITION_TIMEOUT_SECONDS,
                max_connection_pool_size=NEO4J_POOL_SIZE,
                max_transaction_retry_time=NEO4J_QUERY_TIMEOUT_SECONDS,
            )
        finally:
            close_entity_graph_driver()
        driver.close.assert_called_once_with()

    def test_merges_case_insensitive_entities_and_counts_mentions(self):
        entities = EntityExtractor.extract_from_text(
            "PostgreSQL integrates PostgreSQL. Project Borealis launched. Project Borealis scales."
        )
        by_name = {entity.name.casefold(): entity for entity in entities}

        self.assertEqual(by_name["postgresql"].mention_count, 2)
        self.assertEqual(by_name["project borealis"].mention_count, 2)

    def test_caps_large_entity_sets_deterministically(self):
        text = ". ".join(f"Entity{index}" for index in range(MAX_EXTRACTED_ENTITIES + 50))

        first = [entity.name for entity in EntityExtractor.extract_from_text(text)]
        second = [entity.name for entity in EntityExtractor.extract_from_text(text)]

        self.assertEqual(first, second)
        self.assertEqual(len(first), MAX_EXTRACTED_ENTITIES)

    def test_filters_sentence_starters_that_are_not_entities(self):
        entities = EntityExtractor.extract_from_text(
            "See the protocol guide. For production, use PostgreSQL. Therefore, keep MCP enabled."
        )
        names = {entity.name.casefold() for entity in entities}

        self.assertNotIn("see", names)
        self.assertNotIn("for", names)
        self.assertNotIn("therefore", names)
        self.assertIn("postgresql", names)


if __name__ == "__main__":
    unittest.main()
