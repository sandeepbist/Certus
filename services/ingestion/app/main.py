import asyncio
import io
import os
import sys
import json
import logging
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from secrets import compare_digest
from threading import Event, Thread
import psycopg2
from psycopg2.extras import RealDictCursor, execute_values
from fastapi import Depends, FastAPI, UploadFile, File, Form, Header, HTTPException, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, StreamingResponse
from dotenv import load_dotenv
from pydantic import BaseModel, Field
from typing import AsyncIterator, List, Optional, Dict, Any, Literal
import uvicorn
from starlette.datastructures import Headers

MODULE_PATH = Path(__file__).resolve()
REPOSITORY_ROOT = next(
    (parent for parent in MODULE_PATH.parents if (parent / ".env.example").exists()),
    MODULE_PATH.parents[1],
)
sys.path.insert(0, str(REPOSITORY_ROOT))
load_dotenv(REPOSITORY_ROOT / ".env")

from app.parsers.factory import ParserFactory, ParserException
from app.chunking.chunker import ChunkerFactory, trim_text_span
from app.extractors.entity_extractor import EntityExtractor, close_entity_graph_driver
from app.evidence import EXACT_TEXT_LOCATOR_PROFILE, EvidenceIntegrityError, build_evidence_envelope
from app.layout_evidence import LayoutEvidenceIntegrityError, resolve_pdf_visual_target
from app.processing import (
    chunker_profile,
    embedding_job_ranges,
    parse_source_time,
    parser_profile,
    sha256_text,
)
from app.pdf_layout import LAYOUT_CONTENT_ENCODING, LAYOUT_MIME_TYPE
from app.reconciler_runtime import UploadReconcilerConfig, upload_reconciler_is_ready

from services.shared.admission import AdmissionCapacityExceeded, AsyncAdmissionController
from services.shared.embeddings import configured_embedding_profile
from services.shared.embedding_registry import register_embedding_profile
from services.shared.worker_runtime import bounded_int_env, connect_database
from services.shared.object_storage import (
    ObjectConflictError,
    ObjectDigest,
    ObjectIntegrityError,
    ObjectNotFoundError,
    ObjectStorageConfigurationError,
    ObjectStorageError,
    ObjectTooLargeError,
    StoredObject,
    hash_stream,
    pdf_layout_object_key,
    read_bounded_bytes,
)
from app.originals import (
    DuplicateDocument,
    ReplacementDocumentNotFound,
    UploadIdempotencyConflict,
    UploadIntentAbandoned,
    UploadIntent,
    WorkspaceQuotaExceeded,
    close_original_storage,
    lease_upload_for_recovery,
    original_content_disposition,
    ready_original_storage,
    record_stored_object,
    record_upload_error,
    reserve_upload_intent,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("ingestion_service")
UPLOAD_RECONCILER_CONFIG = UploadReconcilerConfig.from_environment()
_upload_reconciler_worker: Optional[Thread] = None


@asynccontextmanager
async def application_lifespan(_application: FastAPI):
    global _upload_reconciler_worker
    stop_event: Optional[Event] = None
    worker: Optional[Thread] = None
    if UPLOAD_RECONCILER_CONFIG.enabled:
        stop_event = Event()
        worker = Thread(
            target=_run_upload_reconciler,
            args=(stop_event,),
            name="certus-upload-reconciler",
            daemon=True,
        )
        _upload_reconciler_worker = worker
        worker.start()
    try:
        yield
    finally:
        worker_stopped = True
        if stop_event is not None and worker is not None:
            stop_event.set()
            shutdown_seconds = UPLOAD_RECONCILER_CONFIG.shutdown_seconds
            await asyncio.to_thread(worker.join, shutdown_seconds)
            worker_stopped = not worker.is_alive()
            if not worker_stopped:
                logger.warning(
                    "Upload reconciler did not stop within %s seconds; durable upload intent remains recoverable",
                    shutdown_seconds,
                )
        if worker_stopped:
            _upload_reconciler_worker = None
            for resource_name, close_resource in (
                ("Neo4j", close_entity_graph_driver),
                ("original storage", close_original_storage),
            ):
                try:
                    await asyncio.to_thread(close_resource)
                except Exception:
                    logger.exception("Could not close ingestion %s resources", resource_name)


app = FastAPI(
    title="Certus Ingestion Service",
    description="Document parsing, MIME detection, entity extraction, and chunking pipeline",
    version="1.0.0",
    lifespan=application_lifespan,
)

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://nexus:nexus_dev_password@localhost:5432/nexus")
DATABASE_CONNECT_TIMEOUT_SECONDS = bounded_int_env(
    "INGESTION_DB_CONNECT_TIMEOUT_SECONDS", 3, 1, 30
)
DATABASE_STATEMENT_TIMEOUT_MS = bounded_int_env(
    "INGESTION_DB_STATEMENT_TIMEOUT_MS", 30_000, 100, 300_000
)
DATABASE_LOCK_TIMEOUT_MS = bounded_int_env(
    "INGESTION_DB_LOCK_TIMEOUT_MS", 5_000, 100, 60_000
)
PROCESSING_ADMISSION = AsyncAdmissionController(
    capacity=bounded_int_env("INGESTION_PROCESSING_CONCURRENCY", 4, 1, 32),
    queue_timeout_ms=bounded_int_env(
        "INGESTION_PROCESSING_QUEUE_TIMEOUT_MS", 500, 10, 10_000
    ),
)
MAX_FILE_SIZE_BYTES = 50 * 1024 * 1024  # 50MB
INTERNAL_SERVICE_TOKEN = os.getenv("INTERNAL_SERVICE_TOKEN", "")
ACTIVE_EMBEDDING_PROFILE = configured_embedding_profile(
    os.getenv("OPENAI_API_KEY", ""),
    os.getenv("OPENAI_EMBEDDING_MODEL", "text-embedding-3-small"),
)

@app.middleware("http")
async def require_internal_service_token(request: Request, call_next):
    if request.url.path in {"/health", "/health/ready"}:
        return await call_next(request)
    provided_token = request.headers.get("x-internal-service-token", "")
    if not INTERNAL_SERVICE_TOKEN:
        return JSONResponse(status_code=503, content={"detail": "Service authentication is not configured"})
    if not compare_digest(provided_token, INTERNAL_SERVICE_TOKEN):
        return JSONResponse(status_code=401, content={"detail": "Unauthorized"})
    return await call_next(request)

def get_db():
    return connect_database(
        DATABASE_URL,
        application_name="certus-ingestion",
        connect_timeout_seconds=DATABASE_CONNECT_TIMEOUT_SECONDS,
        statement_timeout_ms=DATABASE_STATEMENT_TIMEOUT_MS,
        lock_timeout_ms=DATABASE_LOCK_TIMEOUT_MS,
    )


async def acquire_processing_capacity() -> AsyncIterator[None]:
    try:
        async with PROCESSING_ADMISSION.acquire():
            yield
    except AdmissionCapacityExceeded as error:
        raise HTTPException(
            status_code=503,
            detail="Document processing capacity is temporarily full.",
            headers={"Retry-After": "1"},
        ) from error


class IngestResponse(BaseModel):
    document_id: str
    document_version_id: str
    version_number: int
    derivation_id: str
    status: str
    chunk_count: int
    entity_count: int
    content_hash: str
    message: str
    entities: List[Dict[str, Any]] = Field(default_factory=list)


class RechunkRequest(BaseModel):
    strategy: Literal["token", "sentence", "recursive"] = "token"


def stage_document_processing(
    cursor,
    document_id: str,
    document_version_id: str,
    derivation_id: str,
    user_id: str,
    tenant_id: str,
    processing_generation: str,
    chunks,
) -> None:
    chunk_rows = [
        (
            document_id,
            document_version_id,
            derivation_id,
            user_id,
            tenant_id,
            chunk.content,
            chunk.contextualized_content,
            chunk.chunk_index,
            chunk.token_count,
            chunk.section_title,
            chunk.page_number,
            chunk.start_char,
            chunk.end_char,
            "exact",
            EXACT_TEXT_LOCATOR_PROFILE,
            processing_generation,
            ACTIVE_EMBEDDING_PROFILE.identifier,
        )
        for chunk in chunks
    ]
    execute_values(
        cursor,
        """
        INSERT INTO chunks (
            document_id, document_version_id, derivation_id,
            user_id, tenant_id, content, contextualized_content,
            chunk_index, token_count, section_title, page_number,
            start_char, end_char, text_locator_status, text_locator_profile,
            processing_generation, embedding_profile,
            embedded_at
        ) VALUES %s
        """,
        chunk_rows,
        template="(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NULL)",
    )

    job_rows = [
        (
            document_id,
            document_version_id,
            derivation_id,
            tenant_id,
            user_id,
            processing_generation,
            batch_start,
            batch_end,
            len(chunks),
            ACTIVE_EMBEDDING_PROFILE.identifier,
        )
        for batch_start, batch_end in embedding_job_ranges(len(chunks))
    ]
    execute_values(
        cursor,
        """
        INSERT INTO document_embedding_jobs (
            document_id, document_version_id, derivation_id,
            tenant_id, user_id, processing_generation,
            batch_start, batch_end, total_chunks, embedding_profile
        ) VALUES %s
        """,
        job_rows,
    )

@app.get("/health")
def health_check():
    return {
        "status": "ok",
        "service": "ingestion",
        "version": "1.0.0"
    }


def _database_is_ready() -> bool:
    try:
        with get_db() as connection:
            with connection.cursor() as cursor:
                cursor.execute("SET LOCAL statement_timeout = '2000ms'")
                cursor.execute("SELECT 1")
                return cursor.fetchone() == (1,)
    except psycopg2.Error:
        return False


def _original_storage_is_ready() -> bool:
    try:
        ready_original_storage().check_readiness()
        return True
    except ObjectStorageError:
        return False


@app.get("/health/ready")
async def readiness_check():
    database_ready, storage_ready = await asyncio.gather(
        asyncio.to_thread(_database_is_ready),
        asyncio.to_thread(_original_storage_is_ready),
    )
    dependencies = {
        "postgresql": "ready" if database_ready else "not_ready",
        "original_storage": "ready" if storage_ready else "not_ready",
        "upload_reconciler": (
            "ready"
            if upload_reconciler_is_ready(
                UPLOAD_RECONCILER_CONFIG,
                _upload_reconciler_worker,
            )
            else "not_ready"
        ),
    }
    ready = all(status == "ready" for status in dependencies.values())
    return JSONResponse(
        status_code=200 if ready else 503,
        content={
            "status": "ready" if ready else "not_ready",
            "service": "ingestion",
            "dependencies": dependencies,
        },
        headers={"Cache-Control": "no-store"},
    )

@app.post("/ingest", response_model=IngestResponse)
def ingest_document(
    file: UploadFile = File(...),
    tenant_id: str = Header(..., alias="X-Certus-Tenant-Id"),
    user_id: str = Header(..., alias="X-Certus-User-Id"),
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
    chunk_strategy: str = Form("token"),
    tags: Optional[str] = Form(""),
    replace_document_id: Optional[str] = Form(None),
    source_time: Optional[str] = Form(None),
    _capacity: None = Depends(acquire_processing_capacity),
):
    upload_intent: Optional[UploadIntent] = None
    try:
        filename = Path(file.filename or "document.txt").name
        mime_type = file.content_type or "text/plain"
        if not filename or len(filename) > 500 or any(ord(character) < 32 for character in filename):
            raise HTTPException(status_code=422, detail="Filename must be 1-500 printable characters.")
        if len(mime_type) > 100 or any(ord(character) < 32 for character in mime_type):
            raise HTTPException(status_code=415, detail="The claimed MIME type is invalid.")
        if tags and len(tags) > 2048:
            raise HTTPException(status_code=422, detail="Document tags exceed the 2 KB request limit.")
        if len(chunk_strategy) > 32:
            raise HTTPException(status_code=422, detail="Chunk strategy is invalid.")
        if source_time and len(source_time) > 128:
            raise HTTPException(status_code=422, detail="Source time is invalid.")
        try:
            digest = hash_stream(file.file, MAX_FILE_SIZE_BYTES)
        except ObjectTooLargeError as error:
            raise HTTPException(
                status_code=413,
                detail="File exceeds the maximum allowed size of 50 MB.",
            ) from error
        file_size = digest.byte_length
        content_hash = digest.sha256_hex
        content_bytes = read_bounded_bytes(file.file, file_size)

        # 2. Parse file via ParserFactory
        parser = ParserFactory.get_parser(mime_type, filename)
        parsed_doc = parser.parse(content_bytes, filename)
        try:
            parsed_source_time, source_time_origin = parse_source_time(source_time)
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

        # 3. Chunk sections with Contextual Retrieval
        try:
            normalized_chunk_strategy = ChunkerFactory.normalize_strategy(chunk_strategy)
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        chunker = ChunkerFactory.get_chunker(normalized_chunk_strategy)
        parser_provenance = parser_profile(
            parser,
            parsed_doc.metadata.get("format", "text"),
        )
        chunker_provenance = chunker_profile(normalized_chunk_strategy, chunker)
        input_text_hash = sha256_text(parsed_doc.raw_text)
        all_chunks = []
        for section in parsed_doc.sections:
            sec_chunks = chunker.chunk(
                text=section.content,
                document_title=filename,
                section_title=section.title,
                page_number=section.page_number
            )
            for chunk in sec_chunks:
                chunk.start_char += section.start_char
                chunk.end_char += section.start_char
                if parsed_doc.raw_text[chunk.start_char:chunk.end_char] != chunk.content:
                    raise RuntimeError("Chunk text does not match its parsed-artifact span")
                chunk.chunk_index = len(all_chunks)
                all_chunks.append(chunk)

        if not all_chunks:
            from app.chunking.chunker import Chunk, estimate_tokens
            fallback_start, fallback_end = trim_text_span(parsed_doc.raw_text)
            fallback_content = parsed_doc.raw_text[fallback_start:fallback_end]
            all_chunks.append(
                Chunk(
                    content=fallback_content,
                    contextualized_content=f"{filename}: {fallback_content}",
                    chunk_index=0,
                    token_count=estimate_tokens(fallback_content),
                    section_title="Full Content",
                    page_number=1,
                    start_char=fallback_start,
                    end_char=fallback_end,
                )
            )

        # 4. Extract entities for the document and later graph synchronization.
        entities = EntityExtractor.extract_from_text(parsed_doc.raw_text)

        # 5. Reserve a durable, idempotent object identity before crossing the
        # PostgreSQL/object-store boundary. Parser validation has already
        # rejected unsupported or malformed uploads; publication has not begun.
        tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else []
        if len(tag_list) > 20 or any(
            len(tag) > 64 or any(ord(character) < 32 for character in tag)
            for tag in tag_list
        ):
            raise HTTPException(
                status_code=422,
                detail="Use at most 20 printable tags of 64 characters each.",
            )
        explicit_replacement = bool(replace_document_id)
        if replace_document_id:
            try:
                replace_document_id = str(uuid.UUID(replace_document_id))
            except ValueError as error:
                raise HTTPException(
                    status_code=422,
                    detail="replace_document_id must be a valid UUID",
                ) from error

        request_metadata = {
            "schema_version": 1,
            "chunk_strategy": normalized_chunk_strategy,
            "tags": tag_list,
            "source_time": parsed_source_time.isoformat() if parsed_source_time else None,
            "source_time_origin": source_time_origin,
        }
        storage = ready_original_storage()
        upload_intent = reserve_upload_intent(
            get_db,
            idempotency_key=idempotency_key,
            tenant_id=tenant_id,
            user_id=user_id,
            original_filename=filename,
            claimed_mime_type=mime_type,
            digest=digest,
            request_metadata=request_metadata,
            replace_document_id=replace_document_id,
            bucket=storage.settings.bucket,
        )
        if upload_intent.status == "completed" and upload_intent.response_payload:
            return IngestResponse.model_validate(upload_intent.response_payload)

        stored = upload_intent.stored_object()
        if stored:
            stored = storage.verify_exact(
                stored.object_key,
                stored.object_version_id,
                ObjectDigest(
                    stored.byte_length,
                    stored.sha256_hex,
                    stored.checksum_sha256_base64,
                ),
            )
        else:
            stored = storage.put_create_once(
                upload_intent.object_key,
                file.file,
                digest,
                mime_type,
                {
                    "certus-schema": "original-object-v1",
                    "certus-source-object": upload_intent.source_object_id,
                    "certus-document-version": upload_intent.document_version_id,
                    "certus-sha256": digest.sha256_hex,
                },
            )
            upload_intent = record_stored_object(get_db, upload_intent.id, stored)

        stored_layout: Optional[StoredObject] = None
        layout_artifact_id: Optional[str] = None
        if parsed_doc.pdf_layout is not None:
            layout = parsed_doc.pdf_layout
            if layout.raw_text != parsed_doc.raw_text:
                raise RuntimeError("PDF layout text differs from its parsed artifact")
            if layout.producer_profile.get("coordinate_system") != (
                "pymupdf_unrotated_cropbox_top_left_points:v1"
            ):
                raise RuntimeError("PDF layout coordinate profile is unsupported")
            layout_digest = ObjectDigest(
                byte_length=layout.byte_length,
                sha256_hex=layout.content_sha256,
                checksum_sha256_base64=layout.checksum_sha256_base64,
            )
            layout_key = pdf_layout_object_key(
                tenant_id,
                upload_intent.document_id,
                upload_intent.document_version_id,
                content_hash,
                layout.canonical_content_sha256,
            )
            stored_layout = storage.put_create_once(
                layout_key,
                io.BytesIO(layout.compressed_bytes),
                layout_digest,
                LAYOUT_MIME_TYPE,
                {
                    "certus-schema": "pdf-layout-v1",
                    "certus-source-object": upload_intent.source_object_id,
                    "certus-document-version": upload_intent.document_version_id,
                    "certus-source-sha256": content_hash,
                    "certus-layout-sha256": layout.canonical_content_sha256,
                    "certus-content-encoding": LAYOUT_CONTENT_ENCODING,
                },
            )
            layout_artifact_id = str(
                uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    (
                        "certus:pdf-layout:"
                        f"{upload_intent.document_version_id}:"
                        f"{layout.canonical_content_sha256}"
                    ),
                )
            )

        processing_generation = str(uuid.uuid4())
        automation_event_id = str(uuid.uuid4())
        automation_event = {
            "event_id": automation_event_id,
            "type": "on_document_uploaded",
            "tenant_id": tenant_id,
            "user_id": user_id,
            "title": filename,
            "mime_type": mime_type,
            "tags": tag_list,
        }
        
        with get_db() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cursor:
                register_embedding_profile(cursor, ACTIVE_EMBEDDING_PROFILE)
                cursor.execute(
                    """
                    SELECT status, response_payload
                    FROM document_upload_intents
                    WHERE id = %s
                    FOR UPDATE
                    """,
                    (upload_intent.id,),
                )
                locked_intent = cursor.fetchone()
                if not locked_intent:
                    raise RuntimeError("Reserved upload intent disappeared")
                if locked_intent["status"] == "completed" and locked_intent["response_payload"]:
                    return IngestResponse.model_validate(locked_intent["response_payload"])
                if locked_intent["status"] not in {"object_stored", "finalizing"}:
                    raise RuntimeError("Upload intent is not ready for database finalization")
                cursor.execute(
                    """
                    UPDATE document_upload_intents
                    SET status = 'finalizing', updated_at = NOW()
                    WHERE id = %s
                    """,
                    (upload_intent.id,),
                )
                if not replace_document_id:
                    cursor.execute(
                        """
                        SELECT document.id, document.current_version_id,
                               matched_version.id AS matched_version_id,
                               matched_version.version_number,
                               matched_version.status AS version_status
                        FROM document_versions AS matched_version
                        JOIN documents AS document
                          ON document.id = matched_version.document_id
                        WHERE matched_version.tenant_id = %s
                          AND matched_version.user_id = %s
                          AND matched_version.content_hash = %s
                          AND document.deleted_at IS NULL
                        ORDER BY
                            (matched_version.id = document.current_version_id) DESC,
                            matched_version.version_number DESC
                        LIMIT 1
                        """,
                        (tenant_id, user_id, content_hash),
                    )
                    existing = cursor.fetchone()
                    if existing:
                        logger.info("Concurrent duplicate document detected: %s", existing["id"])
                        raise HTTPException(
                            status_code=409,
                            detail={
                                "code": "duplicate_document",
                                "message": "This document has already been ingested in the workspace.",
                                "document_id": str(existing["id"]),
                                "document_version_id": str(existing["matched_version_id"]),
                                "version_number": int(existing["version_number"]),
                            },
                        )

                derivation_id = str(uuid.uuid4())
                parsed_artifact_id = str(uuid.uuid4())
                version_id: str
                version_number: int
                source_recorded_at: Optional[datetime] = None
                source_title = filename
                source_type = parsed_doc.metadata.get("format", "text")
                source_mime_type = mime_type
                source_file_size = file_size
                source_hash = content_hash
                source_raw_text = parsed_doc.raw_text
                source_tags = tag_list
                source_timestamp = parsed_source_time
                source_timestamp_origin = source_time_origin
                source_parser_profile = parser_provenance
                source_metadata = parsed_doc.metadata

                if replace_document_id:
                    cursor.execute(
                        """
                        SELECT id
                        FROM document_embedding_jobs
                        WHERE document_id = %s
                          AND tenant_id = %s AND user_id = %s
                          AND status IN ('pending', 'publishing', 'published', 'processing')
                        ORDER BY id
                        FOR UPDATE
                        """,
                        (replace_document_id, tenant_id, user_id),
                    )
                    cursor.fetchall()
                    cursor.execute(
                        """
                        SELECT document.id, document.status, document.current_version_id,
                               version.version_number, version.title, version.source_type,
                               version.mime_type, version.file_size_bytes,
                               version.content_hash, version.tags,
                               version.source_time, version.source_time_origin,
                               version.recorded_at, version.parser_profile,
                               version.source_metadata, version.current_derivation_id
                        FROM documents AS document
                        JOIN document_versions AS version
                          ON version.id = document.current_version_id
                         AND version.document_id = document.id
                        WHERE document.id = %s
                          AND document.tenant_id = %s
                          AND document.user_id = %s
                          AND document.deleted_at IS NULL
                        FOR UPDATE OF document, version
                        """,
                        (replace_document_id, tenant_id, user_id),
                    )
                    current = cursor.fetchone()
                    if not current:
                        raise HTTPException(status_code=404, detail="Document to replace was not found")
                    doc_id = str(current["id"])
                    same_source_bytes = current["content_hash"] == content_hash
                    same_source_record = (
                        same_source_bytes
                        and current["title"] == filename
                        and current["source_type"] == source_type
                        and current["mime_type"] == mime_type
                        and list(current["tags"] or []) == tag_list
                        and current["source_time"] == parsed_source_time
                    )
                    if same_source_record and explicit_replacement:
                        raise HTTPException(
                            status_code=409,
                            detail={
                                "code": "duplicate_document_version",
                                "message": "The current document version already has these exact source bytes.",
                                "document_id": doc_id,
                                "document_version_id": str(current["current_version_id"]),
                            },
                        )
                    cursor.execute(
                        """
                        UPDATE document_derivations
                        SET status = 'superseded', superseded_at = COALESCE(superseded_at, NOW()),
                            updated_at = NOW()
                        WHERE id = %s AND status IN ('processing', 'error')
                        """,
                        (str(current["current_derivation_id"]),),
                    )
                    cursor.execute(
                        """
                        UPDATE document_versions
                        SET status = 'error',
                            error_message = COALESCE(
                                error_message,
                                'Processing was superseded by a newer source version.'
                            ),
                            updated_at = NOW()
                        WHERE id = %s AND status = 'processing'
                        """,
                        (str(current["current_version_id"]),),
                    )
                    cursor.execute(
                        """
                        SELECT COALESCE(MAX(version_number), 0) + 1 AS next_version
                        FROM document_versions
                        WHERE document_id = %s
                        """,
                        (doc_id,),
                    )
                    version_number = int(cursor.fetchone()["next_version"])
                    version_id = upload_intent.document_version_id
                    cursor.execute(
                        """
                        INSERT INTO document_versions (
                            id, document_id, tenant_id, user_id, version_number,
                            title, source_type, mime_type, file_size_bytes,
                            content_hash, tags, source_time,
                            source_time_origin, status,
                            processing_generation, current_derivation_id,
                            parser_profile, source_metadata
                        ) VALUES (
                            %s, %s, %s, %s, %s,
                            %s, %s, %s,
                            %s, %s, %s, %s,
                            %s, 'processing',
                            %s, %s, %s::jsonb, %s::jsonb
                        )
                        RETURNING recorded_at
                        """,
                        (
                            version_id, doc_id, tenant_id, user_id, version_number,
                            source_title, source_type, source_mime_type,
                            source_file_size, source_hash, source_tags,
                            source_timestamp, source_timestamp_origin,
                            processing_generation, derivation_id,
                            json.dumps(source_parser_profile), json.dumps(source_metadata),
                        ),
                    )
                    source_recorded_at = cursor.fetchone()["recorded_at"]
                    cursor.execute(
                        """
                        UPDATE document_embedding_jobs
                        SET status = 'obsolete', locked_at = NULL,
                            processing_owner = NULL,
                            processed_at = COALESCE(processed_at, NOW()),
                            updated_at = NOW()
                        WHERE document_id = %s
                          AND status IN ('pending', 'publishing', 'published', 'processing')
                        """,
                        (doc_id,),
                    )
                else:
                    doc_id = upload_intent.document_id
                    version_id = upload_intent.document_version_id
                    version_number = 1
                    cursor.execute(
                        """
                        INSERT INTO documents (
                            id, current_version_id, user_id, tenant_id,
                            title, source_type, mime_type,
                            file_size_bytes, content_hash, chunk_count,
                            processing_total_chunks, entity_count, tags, status,
                            processing_generation, metadata
                        ) VALUES (
                            %s, %s, %s, %s,
                            %s, %s, %s,
                            %s, %s, 0, %s,
                            %s, %s, 'processing', %s, %s::jsonb
                        )
                        """,
                        (
                            doc_id, version_id, user_id, tenant_id, source_title,
                            source_type, source_mime_type, source_file_size,
                            source_hash, len(all_chunks),
                            len(entities), source_tags, processing_generation,
                            json.dumps({
                                "automation_event_id": automation_event_id,
                                "chunk_strategy": normalized_chunk_strategy,
                            }),
                        ),
                    )
                    cursor.execute(
                        """
                        INSERT INTO document_versions (
                            id, document_id, tenant_id, user_id, version_number,
                            title, source_type, mime_type, file_size_bytes,
                            content_hash, tags, source_time,
                            source_time_origin, status,
                            processing_generation, current_derivation_id,
                            parser_profile, source_metadata
                        ) VALUES (
                            %s, %s, %s, %s, %s,
                            %s, %s, %s,
                            %s, %s, %s, %s,
                            %s, 'processing',
                            %s, %s, %s::jsonb, %s::jsonb
                        )
                        RETURNING recorded_at
                        """,
                        (
                            version_id, doc_id, tenant_id, user_id, version_number,
                            source_title, source_type, source_mime_type,
                            source_file_size, source_hash, source_tags,
                            source_timestamp, source_timestamp_origin,
                            processing_generation, derivation_id,
                            json.dumps(source_parser_profile), json.dumps(source_metadata),
                        ),
                    )
                    source_recorded_at = cursor.fetchone()["recorded_at"]

                cursor.execute(
                    """
                    INSERT INTO document_source_objects (
                        id, document_version_id, document_id, tenant_id, user_id,
                        storage_backend, bucket, object_key, object_version_id,
                        original_filename, claimed_mime_type, detected_mime_type,
                        byte_length, content_sha256, checksum_sha256_base64,
                        etag, storage_class, server_side_encryption, kms_key_id,
                        bucket_key_enabled, status, stored_at, last_verified_at
                    ) VALUES (
                        %s, %s, %s, %s, %s,
                        's3', %s, %s, %s,
                        %s, %s, NULL,
                        %s, %s, %s,
                        %s, %s, %s, %s,
                        %s, 'available', %s, NOW()
                    )
                    """,
                    (
                        upload_intent.source_object_id,
                        version_id,
                        doc_id,
                        tenant_id,
                        user_id,
                        stored.bucket,
                        stored.object_key,
                        stored.object_version_id,
                        source_title,
                        source_mime_type,
                        stored.byte_length,
                        stored.sha256_hex,
                        stored.checksum_sha256_base64,
                        stored.etag,
                        stored.storage_class,
                        stored.server_side_encryption,
                        stored.kms_key_id,
                        stored.bucket_key_enabled,
                        upload_intent.object_stored_at,
                    ),
                )
                parsed_text_bytes = source_raw_text.encode("utf-8")
                cursor.execute(
                    """
                    INSERT INTO document_parsed_artifacts (
                        id, source_object_id, document_version_id, document_id,
                        tenant_id, user_id, content_text, content_sha256,
                        byte_length, producer_profile, status
                    ) VALUES (
                        %s, %s, %s, %s,
                        %s, %s, %s, %s,
                        %s, %s::jsonb, 'ready'
                    )
                    """,
                    (
                        parsed_artifact_id,
                        upload_intent.source_object_id,
                        version_id,
                        doc_id,
                        tenant_id,
                        user_id,
                        source_raw_text,
                        input_text_hash,
                        len(parsed_text_bytes),
                        json.dumps(source_parser_profile),
                    ),
                )

                if parsed_doc.pdf_layout is not None:
                    if stored_layout is None or layout_artifact_id is None:
                        raise RuntimeError("PDF layout object was not durably stored")
                    layout = parsed_doc.pdf_layout
                    cursor.execute(
                        """
                        INSERT INTO document_layout_artifacts (
                            id, source_object_id, parsed_artifact_id,
                            document_version_id, document_id, tenant_id, user_id,
                            bucket, object_key, object_version_id,
                            mime_type, content_encoding, byte_length,
                            uncompressed_byte_length, content_sha256,
                            canonical_content_sha256, checksum_sha256_base64,
                            etag, storage_class, server_side_encryption,
                            kms_key_id, bucket_key_enabled,
                            layout_schema_version, producer_profile,
                            coordinate_system, page_join_contract,
                            page_count, text_run_count, status, stored_at,
                            last_verified_at
                        ) VALUES (
                            %s, %s, %s,
                            %s, %s, %s, %s,
                            %s, %s, %s,
                            %s, %s, %s,
                            %s, %s,
                            %s, %s,
                            %s, %s, %s,
                            %s, %s,
                            1, %s::jsonb,
                            %s, %s,
                            %s, %s, 'ready', NOW(), NOW()
                        )
                        """,
                        (
                            layout_artifact_id,
                            upload_intent.source_object_id,
                            parsed_artifact_id,
                            version_id,
                            doc_id,
                            tenant_id,
                            user_id,
                            stored_layout.bucket,
                            stored_layout.object_key,
                            stored_layout.object_version_id,
                            LAYOUT_MIME_TYPE,
                            LAYOUT_CONTENT_ENCODING,
                            stored_layout.byte_length,
                            layout.uncompressed_byte_length,
                            stored_layout.sha256_hex,
                            layout.canonical_content_sha256,
                            stored_layout.checksum_sha256_base64,
                            stored_layout.etag,
                            stored_layout.storage_class,
                            stored_layout.server_side_encryption,
                            stored_layout.kms_key_id,
                            stored_layout.bucket_key_enabled,
                            json.dumps(layout.producer_profile),
                            layout.producer_profile["coordinate_system"],
                            layout.producer_profile["page_join_contract"],
                            len(layout.pages),
                            len(layout.text_runs),
                        ),
                    )

                    layout_uuid = uuid.UUID(layout_artifact_id)
                    page_ids = {
                        page.page_index: str(
                            uuid.uuid5(layout_uuid, f"page:{page.page_index}")
                        )
                        for page in layout.pages
                    }
                    execute_values(
                        cursor,
                        """
                        INSERT INTO document_layout_pages (
                            id, layout_artifact_id, parsed_artifact_id,
                            document_version_id, document_id, tenant_id, user_id,
                            page_index, page_label, width_points, height_points,
                            rotation_degrees,
                            media_x0, media_y0, media_x1, media_y1,
                            crop_x0, crop_y0, crop_x1, crop_y1,
                            parsed_start, parsed_end, extraction_status
                        ) VALUES %s
                        """,
                        [
                            (
                                page_ids[page.page_index],
                                layout_artifact_id,
                                parsed_artifact_id,
                                version_id,
                                doc_id,
                                tenant_id,
                                user_id,
                                page.page_index,
                                page.page_label,
                                page.width_points,
                                page.height_points,
                                page.rotation_degrees,
                                *page.media_box,
                                *page.crop_box,
                                page.parsed_start,
                                page.parsed_end,
                                page.extraction_status,
                            )
                            for page in layout.pages
                        ],
                    )
                    if layout.text_runs:
                        execute_values(
                            cursor,
                            """
                            INSERT INTO document_text_runs (
                                id, layout_artifact_id, layout_page_id, page_index,
                                parsed_start, parsed_end, reading_order,
                                source_block_index, line_index, span_index,
                                text_sha256,
                                bbox_x0, bbox_y0, bbox_x1, bbox_y1,
                                quad_ul_x, quad_ul_y, quad_ur_x, quad_ur_y,
                                quad_ll_x, quad_ll_y, quad_lr_x, quad_lr_y,
                                direction_x, direction_y, writing_mode
                            ) VALUES %s
                            """,
                            [
                                (
                                    str(
                                        uuid.uuid5(
                                            layout_uuid,
                                            (
                                                f"run:{run.page_index}:"
                                                f"{run.reading_order}"
                                            ),
                                        )
                                    ),
                                    layout_artifact_id,
                                    page_ids[run.page_index],
                                    run.page_index,
                                    run.parsed_start,
                                    run.parsed_end,
                                    run.reading_order,
                                    run.source_block_index,
                                    run.line_index,
                                    run.span_index,
                                    run.text_sha256,
                                    *run.bbox,
                                    *run.quad,
                                    *run.direction,
                                    run.writing_mode,
                                )
                                for run in layout.text_runs
                            ],
                        )

                cursor.execute(
                    """
                    INSERT INTO document_derivations (
                        id, document_version_id, document_id, tenant_id, user_id,
                        processing_generation, input_text_hash,
                        input_parsed_artifact_id, parser_profile,
                        chunker_profile, embedding_profile, status,
                        chunk_count, processing_total_chunks
                    ) VALUES (
                        %s, %s, %s, %s, %s,
                        %s, %s, %s, %s::jsonb,
                        %s::jsonb, %s, 'processing', 0, %s
                    )
                    """,
                    (
                        derivation_id, version_id, doc_id, tenant_id, user_id,
                        processing_generation, input_text_hash, parsed_artifact_id,
                        json.dumps(parser_provenance),
                        json.dumps(chunker_provenance),
                        ACTIVE_EMBEDDING_PROFILE.identifier,
                        len(all_chunks),
                    ),
                )

                cursor.execute(
                    """
                    UPDATE documents SET
                        current_version_id = %s,
                        title = %s, source_type = %s, mime_type = %s,
                        file_size_bytes = %s, content_hash = %s,
                        chunk_count = 0, processing_total_chunks = %s,
                        entity_count = %s, tags = %s,
                        status = 'processing', error_message = NULL,
                        processing_generation = %s,
                        metadata = metadata || %s::jsonb,
                        updated_at = NOW()
                    WHERE id = %s AND tenant_id = %s AND user_id = %s
                      AND deleted_at IS NULL
                    """,
                    (
                        version_id, source_title, source_type, source_mime_type,
                        source_file_size, source_hash,
                        len(all_chunks), len(entities), source_tags,
                        processing_generation,
                        json.dumps({
                            "automation_event_id": automation_event_id,
                            "chunk_strategy": normalized_chunk_strategy,
                        }),
                        doc_id, tenant_id, user_id,
                    ),
                )

                stage_document_processing(
                    cursor,
                    doc_id,
                    version_id,
                    derivation_id,
                    user_id,
                    tenant_id,
                    processing_generation,
                    all_chunks,
                )
                assert source_recorded_at is not None
                automation_event.update({
                    "document_id": doc_id,
                    "document_version_id": version_id,
                    "version_number": version_number,
                    "derivation_id": derivation_id,
                    "content_hash": source_hash,
                    "source_time": (
                        source_timestamp.isoformat() if source_timestamp else None
                    ),
                    "recorded_at": source_recorded_at.isoformat(),
                    "title": source_title,
                    "mime_type": source_mime_type,
                    "tags": source_tags,
                })
                cursor.execute(
                    """
                    INSERT INTO automation_events (
                        id, document_id, tenant_id, user_id, event_type, payload
                    ) VALUES (%s, %s, %s, %s, %s, %s::jsonb)
                    """,
                    (
                        automation_event_id,
                        doc_id,
                        tenant_id,
                        user_id,
                        automation_event["type"],
                        json.dumps(automation_event),
                    ),
                )
                response_payload = IngestResponse(
                    document_id=doc_id,
                    document_version_id=version_id,
                    version_number=version_number,
                    derivation_id=derivation_id,
                    status="processing",
                    chunk_count=len(all_chunks),
                    entity_count=len(entities),
                    content_hash=source_hash,
                    message=(
                        f"Document '{source_title}' was preserved, parsed, and durably "
                        "queued for embedding."
                    ),
                    entities=[entity.to_dict() for entity in entities],
                ).model_dump(mode="json")
                cursor.execute(
                    "SELECT complete_workspace_upload(%s, %s::jsonb)",
                    (upload_intent.id, json.dumps(response_payload)),
                )

        # 6. Sync the knowledge graph only after chunks and both outbox records
        # commit. A graph outage degrades traversal without losing retrieval work.
        EntityExtractor.sync_to_neo4j(doc_id, source_title, user_id, tenant_id, entities)

        logger.info("Ingested document %s: %s chunks staged durably.", doc_id, len(all_chunks))

        return IngestResponse.model_validate(response_payload)

    except ParserException as pe:
        raise HTTPException(status_code=pe.status_code, detail=str(pe))
    except (DuplicateDocument, UploadIdempotencyConflict) as error:
        detail = error.detail if isinstance(error, DuplicateDocument) else str(error)
        raise HTTPException(status_code=409, detail=detail) from error
    except ReplacementDocumentNotFound as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except WorkspaceQuotaExceeded as error:
        raise HTTPException(status_code=409, detail=error.detail) from error
    except UploadIntentAbandoned as error:
        raise HTTPException(
            status_code=409,
            detail={"code": "upload_intent_abandoned", "message": str(error)},
        ) from error
    except ObjectStorageConfigurationError as error:
        logger.error("Original storage configuration failed: %s", error)
        raise HTTPException(
            status_code=503,
            detail="Original document storage is not ready.",
        ) from error
    except ObjectConflictError as error:
        if upload_intent:
            record_upload_error(get_db, upload_intent.id, error)
        logger.error("Original storage key conflict for upload intent %s", upload_intent.id if upload_intent else "unreserved")
        raise HTTPException(status_code=409, detail="The reserved original-object key is in conflict.") from error
    except ObjectIntegrityError as error:
        if upload_intent:
            record_upload_error(get_db, upload_intent.id, error)
        logger.error("Original storage integrity failure", exc_info=True)
        raise HTTPException(status_code=500, detail="Original document integrity verification failed.") from error
    except ObjectStorageError as error:
        if upload_intent:
            record_upload_error(get_db, upload_intent.id, error)
        logger.warning("Original storage is temporarily unavailable: %s", error)
        raise HTTPException(status_code=503, detail="Original document storage is temporarily unavailable.") from error
    except HTTPException as error:
        if upload_intent:
            record_upload_error(get_db, upload_intent.id, error)
        raise
    except psycopg2.errors.UniqueViolation as error:
        if upload_intent:
            record_upload_error(get_db, upload_intent.id, error)
        logger.info("Concurrent duplicate document upload rejected")
        raise HTTPException(status_code=409, detail="This document already exists in the workspace.") from error
    except Exception as e:
        if upload_intent:
            try:
                record_upload_error(get_db, upload_intent.id, e)
            except Exception:
                logger.error("Failed to record upload intent error", exc_info=True)
        logger.error(f"Ingestion error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Document ingestion failed.") from e


def _reconcile_upload_intent(intent: UploadIntent) -> None:
    storage = ready_original_storage()
    digest = ObjectDigest(
        intent.byte_length,
        intent.content_sha256,
        intent.checksum_sha256_base64,
    )
    stored = intent.stored_object()
    if not stored:
        stored = storage.recover_existing(intent.object_key, digest)
        intent = record_stored_object(get_db, intent.id, stored)

    spool = storage.download_verified_to_spool(stored)
    upload = UploadFile(
        file=spool,
        size=stored.byte_length,
        filename=intent.original_filename,
        headers=Headers({"content-type": intent.claimed_mime_type}),
    )
    try:
        metadata = intent.request_metadata
        response = ingest_document(
            file=upload,
            tenant_id=intent.tenant_id,
            user_id=intent.user_id,
            idempotency_key=intent.idempotency_key,
            chunk_strategy=str(metadata.get("chunk_strategy", "token")),
            tags=",".join(str(tag) for tag in metadata.get("tags", [])),
            replace_document_id=intent.replace_document_id,
            source_time=metadata.get("source_time"),
        )
        logger.info(
            "Recovered upload intent %s into document %s version %s",
            intent.id,
            response.document_id,
            response.document_version_id,
        )
    finally:
        spool.close()


def _run_upload_reconciler(stop_event: Event) -> None:
    poll_seconds = UPLOAD_RECONCILER_CONFIG.poll_seconds
    lease_seconds = UPLOAD_RECONCILER_CONFIG.lease_seconds
    max_attempts = UPLOAD_RECONCILER_CONFIG.max_attempts
    worker_id = str(uuid.uuid4())

    while not stop_event.is_set():
        try:
            intent = lease_upload_for_recovery(
                get_db,
                worker_id=worker_id,
                lease_seconds=lease_seconds,
                max_attempts=max_attempts,
            )
        except Exception:
            logger.error("Upload reconciler could not lease work", exc_info=True)
            stop_event.wait(poll_seconds)
            continue

        if not intent:
            stop_event.wait(poll_seconds)
            continue

        try:
            _reconcile_upload_intent(intent)
        except Exception as error:
            try:
                record_upload_error(get_db, intent.id, error)
            except Exception:
                logger.error(
                    "Upload reconciler could not persist failure for intent %s",
                    intent.id,
                    exc_info=True,
                )
            logger.warning(
                "Upload reconciliation failed for intent %s: %s",
                intent.id,
                error,
            )


@app.get("/documents")
def list_documents(
    tenant_id: str = Header(..., alias="X-Certus-Tenant-Id"),
    user_id: str = Header(..., alias="X-Certus-User-Id"),
    limit: int = Query(50, ge=1, le=100),
    page_cursor: Optional[uuid.UUID] = Query(None, alias="cursor"),
    search: str = Query("", max_length=500),
    status: Optional[Literal["processing", "ready", "error"]] = Query(None),
    tag: str = Query("", max_length=64),
    source_type: str = Query("", max_length=50),
):
    filters = [
        "document.tenant_id = %s",
        "document.user_id = %s",
        "document.deleted_at IS NULL",
    ]
    values: List[Any] = [tenant_id, user_id]
    clean_search = search.strip()
    clean_tag = tag.strip()
    clean_source_type = source_type.strip().lower()
    if clean_search:
        filters.append(
            "(document.title_search_vector @@ websearch_to_tsquery('english', %s) "
            "OR lower(document.title) LIKE '%%' || lower(%s) || '%%' "
            "OR document.tags @> ARRAY[%s]::TEXT[])"
        )
        values.extend([clean_search, clean_search, clean_search])
    if status:
        filters.append("document.status = %s")
        values.append(status)
    if clean_tag:
        filters.append("document.tags @> ARRAY[%s]::TEXT[]")
        values.append(clean_tag)
    if clean_source_type:
        filters.append("document.source_type = %s")
        values.append(clean_source_type)
    where_clause = " AND ".join(filters)

    with get_db() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cursor:
            if page_cursor is not None:
                cursor.execute(
                    f"""
                    SELECT document.created_at, document.id
                    FROM documents AS document
                    WHERE document.id = %s AND {where_clause}
                    """,  # noqa: S608 -- where_clause contains only server-owned SQL fragments.
                    (str(page_cursor), *values),
                )
                anchor = cursor.fetchone()
                if not anchor:
                    raise HTTPException(
                        status_code=422,
                        detail="The document page cursor is invalid.",
                    )
            else:
                anchor = None

            page_filter = ""
            page_values = list(values)
            if anchor:
                page_filter = "AND (document.created_at, document.id) < (%s, %s)"
                page_values.extend([anchor["created_at"], anchor["id"]])
            page_values.append(limit + 1)
            cursor.execute(
                f"""
                SELECT document.id, document.title, document.source_type,
                       document.mime_type, document.file_size_bytes,
                       document.chunk_count, document.processing_total_chunks,
                       document.entity_count, document.tags, document.status,
                       document.created_at, document.current_version_id,
                       current_version.version_number,
                       current_version.source_time,
                       current_version.recorded_at,
                       (
                           SELECT COUNT(*)::INT
                           FROM document_versions AS history
                           WHERE history.document_id = document.id
                       ) AS version_count
                FROM documents AS document
                JOIN document_versions AS current_version
                  ON current_version.id = document.current_version_id
                 AND current_version.document_id = document.id
                 AND current_version.tenant_id = document.tenant_id
                 AND current_version.user_id = document.user_id
                WHERE {where_clause}
                  {page_filter}
                ORDER BY document.created_at DESC, document.id DESC
                LIMIT %s
                """,  # noqa: S608 -- where_clause contains only server-owned SQL fragments.
                page_values,
            )
            rows = cursor.fetchall()
            has_more = len(rows) > limit
            docs = [dict(document) for document in rows[:limit]]
    return {
        "documents": docs,
        "pagination": {
            "limit": limit,
            "next_cursor": str(docs[-1]["id"]) if has_more else None,
        },
    }

@app.get("/documents/{document_id}")
def get_document_details(
    document_id: str,
    tenant_id: str = Header(..., alias="X-Certus-Tenant-Id"),
    user_id: str = Header(..., alias="X-Certus-User-Id"),
    version: Optional[int] = Query(None, ge=1),
    chunk_limit: int = Query(100, ge=1, le=200),
    chunk_offset: int = Query(0, ge=0),
):
    with get_db() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute(
                """
                SELECT document.id, selected_version.title,
                       selected_version.source_type, selected_version.mime_type,
                       selected_version.file_size_bytes,
                       derivation.chunk_count,
                       derivation.processing_total_chunks,
                       document.entity_count, selected_version.content_hash,
                       selected_version.tags, selected_version.status,
                       selected_version.error_message,
                       document.metadata, document.created_at, document.updated_at,
                       document.current_version_id,
                       selected_version.id AS document_version_id,
                       selected_version.version_number,
                       selected_version.source_time,
                       selected_version.source_time_origin,
                       selected_version.recorded_at,
                       selected_version.parser_profile,
                       selected_version.source_metadata,
                       selected_version.current_derivation_id,
                       selected_version.pending_derivation_id,
                       source_object.status AS original_status,
                       source_object.original_filename,
                       source_object.last_verified_at AS original_last_verified_at,
                       derivation.chunker_profile,
                       derivation.embedding_profile,
                       derivation.input_parsed_artifact_id AS parsed_artifact_id,
                       derivation.created_at AS derivation_created_at,
                       (selected_version.id = document.current_version_id) AS is_current_version
                FROM documents AS document
                JOIN document_versions AS selected_version
                  ON selected_version.document_id = document.id
                 AND selected_version.tenant_id = document.tenant_id
                 AND selected_version.user_id = document.user_id
                 AND (
                      (%s IS NULL AND selected_version.id = document.current_version_id)
                      OR selected_version.version_number = %s
                 )
                JOIN document_derivations AS derivation
                  ON derivation.id = selected_version.current_derivation_id
                 AND derivation.document_version_id = selected_version.id
                 AND derivation.document_id = document.id
                 AND derivation.tenant_id = document.tenant_id
                 AND derivation.user_id = document.user_id
                JOIN document_source_objects AS source_object
                  ON source_object.document_version_id = selected_version.id
                 AND source_object.document_id = document.id
                 AND source_object.tenant_id = document.tenant_id
                 AND source_object.user_id = document.user_id
                WHERE document.id = %s
                  AND document.tenant_id = %s
                  AND document.user_id = %s
                  AND document.deleted_at IS NULL
                """,
                (version, version, document_id, tenant_id, user_id),
            )
            doc = cursor.fetchone()
            if not doc:
                raise HTTPException(status_code=404, detail="Document not found")

            cursor.execute(
                """
                SELECT id, document_version_id, derivation_id, chunk_index,
                       token_count, section_title, page_number, start_char,
                       end_char, content, contextualized_content,
                       text_locator_status, text_locator_profile,
                       text_locator_unavailable_reason,
                       embedding_profile, embedded_at, created_at
                FROM chunks
                WHERE document_id = %s
                  AND document_version_id = %s
                  AND derivation_id = %s
                  AND tenant_id = %s AND user_id = %s
                ORDER BY chunk_index ASC
                LIMIT %s OFFSET %s
                """,
                (
                    document_id,
                    str(doc["document_version_id"]),
                    str(doc["current_derivation_id"]),
                    tenant_id,
                    user_id,
                    chunk_limit,
                    chunk_offset,
                ),
            )
            chunks = cursor.fetchall()
            cursor.execute(
                """
                SELECT version.id AS document_version_id,
                       version.version_number, version.title,
                       version.content_hash, version.status,
                       version.source_time, version.source_time_origin,
                       version.recorded_at, version.error_message,
                       derivation.id AS derivation_id,
                       version.pending_derivation_id,
                       derivation.chunk_count,
                       derivation.processing_total_chunks,
                       derivation.embedding_profile,
                       source_object.status AS original_status,
                       (version.id = document.current_version_id) AS is_current_version,
                       COUNT(*) OVER() AS total_versions
                FROM document_versions AS version
                JOIN documents AS document ON document.id = version.document_id
                JOIN document_derivations AS derivation
                  ON derivation.id = version.current_derivation_id
                 AND derivation.document_version_id = version.id
                JOIN document_source_objects AS source_object
                  ON source_object.document_version_id = version.id
                 AND source_object.document_id = document.id
                 AND source_object.tenant_id = version.tenant_id
                 AND source_object.user_id = version.user_id
                WHERE version.document_id = %s
                  AND version.tenant_id = %s
                  AND version.user_id = %s
                  AND document.deleted_at IS NULL
                ORDER BY version.version_number DESC
                LIMIT 100
                """,
                (document_id, tenant_id, user_id),
            )
            versions = cursor.fetchall()

    entities, entities_status = EntityExtractor.list_document_entities(
        document_id,
        user_id,
        tenant_id,
    )
    return {
        "document": dict(doc),
        "chunks": [dict(c) for c in chunks],
        "chunk_page": {
            "limit": chunk_limit,
            "offset": chunk_offset,
            "total_count": int(doc["processing_total_chunks"]),
        },
        "versions": [dict(item) for item in versions],
        "version_count": int(versions[0]["total_versions"]) if versions else 0,
        "history_truncated": bool(versions and versions[0]["total_versions"] > len(versions)),
        "entities": entities,
        "entities_status": entities_status,
        "entities_scope": "current_graph_projection",
    }


@app.get("/documents/{document_id}/evidence/{chunk_id}")
def resolve_document_evidence(
    document_id: uuid.UUID,
    chunk_id: uuid.UUID,
    tenant_id: str = Header(..., alias="X-Certus-Tenant-Id"),
    user_id: str = Header(..., alias="X-Certus-User-Id"),
):
    with get_db() as connection:
        with connection.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute(
                """
                SELECT chunk.id AS chunk_id, chunk.document_id,
                       chunk.document_version_id, version.title AS document_title,
                       version.version_number,
                       version.content_hash AS version_content_hash,
                       version.source_time, version.recorded_at,
                       (document.current_version_id = version.id) AS is_current_version,
                       chunk.derivation_id, derivation.input_parsed_artifact_id AS parsed_artifact_id,
                       parsed.status AS parsed_status,
                       parsed.content_sha256 AS parsed_content_sha256,
                       parsed.content_text AS parsed_content_text,
                       parsed.producer_profile AS parser_profile,
                       source.id AS source_object_id,
                       source.status AS original_status,
                       source.original_filename,
                       version.mime_type AS source_mime_type,
                       source.byte_length AS original_byte_length,
                       source.content_sha256 AS original_content_sha256,
                       derivation.chunker_profile,
                       chunk.text_locator_status, chunk.text_locator_profile,
                       chunk.text_locator_unavailable_reason,
                       chunk.start_char, chunk.end_char,
                       chunk.content AS chunk_content,
                       CASE WHEN chunk.text_locator_status = 'exact' THEN
                           substring(
                               parsed.content_text
                               FROM chunk.start_char + 1
                               FOR chunk.end_char - chunk.start_char
                           )
                       END AS resolved_quote,
                       CASE WHEN chunk.text_locator_status = 'exact' THEN
                           substring(
                               parsed.content_text
                               FROM GREATEST(chunk.start_char - 32, 0) + 1
                               FOR chunk.start_char - GREATEST(chunk.start_char - 32, 0)
                           )
                       END AS resolved_prefix,
                       CASE WHEN chunk.text_locator_status = 'exact' THEN
                           substring(parsed.content_text FROM chunk.end_char + 1 FOR 32)
                       END AS resolved_suffix,
                       chunk.page_number, chunk.section_title,
                       layout.id AS layout_artifact_id,
                       layout.status AS layout_status,
                       layout.bucket AS layout_bucket,
                       layout.object_key AS layout_object_key,
                       layout.object_version_id AS layout_object_version_id,
                       layout.byte_length AS layout_byte_length,
                       layout.uncompressed_byte_length AS layout_uncompressed_byte_length,
                       layout.content_sha256 AS layout_content_sha256,
                       layout.canonical_content_sha256 AS layout_canonical_content_sha256,
                       layout.checksum_sha256_base64 AS layout_checksum_sha256_base64,
                       layout.etag AS layout_etag,
                       layout.storage_class AS layout_storage_class,
                       layout.server_side_encryption AS layout_server_side_encryption,
                       layout.kms_key_id AS layout_kms_key_id,
                       layout.bucket_key_enabled AS layout_bucket_key_enabled,
                       layout.producer_profile AS layout_producer_profile,
                       layout.page_count AS layout_page_count
                FROM chunks AS chunk
                JOIN documents AS document
                  ON document.id = chunk.document_id
                 AND document.tenant_id = chunk.tenant_id
                 AND document.user_id = chunk.user_id
                JOIN document_versions AS version
                  ON version.id = chunk.document_version_id
                 AND version.document_id = chunk.document_id
                 AND version.tenant_id = chunk.tenant_id
                 AND version.user_id = chunk.user_id
                JOIN document_derivations AS derivation
                  ON derivation.id = chunk.derivation_id
                 AND derivation.document_version_id = chunk.document_version_id
                 AND derivation.document_id = chunk.document_id
                 AND derivation.tenant_id = chunk.tenant_id
                 AND derivation.user_id = chunk.user_id
                JOIN document_parsed_artifacts AS parsed
                  ON parsed.id = derivation.input_parsed_artifact_id
                 AND parsed.document_version_id = derivation.document_version_id
                 AND parsed.document_id = derivation.document_id
                 AND parsed.tenant_id = derivation.tenant_id
                 AND parsed.user_id = derivation.user_id
                JOIN document_source_objects AS source
                  ON source.id = parsed.source_object_id
                 AND source.document_version_id = parsed.document_version_id
                 AND source.document_id = parsed.document_id
                 AND source.tenant_id = parsed.tenant_id
                 AND source.user_id = parsed.user_id
                LEFT JOIN document_layout_artifacts AS layout
                  ON layout.parsed_artifact_id = parsed.id
                 AND layout.document_version_id = parsed.document_version_id
                 AND layout.document_id = parsed.document_id
                 AND layout.tenant_id = parsed.tenant_id
                 AND layout.user_id = parsed.user_id
                WHERE chunk.id = %s
                  AND chunk.document_id = %s
                  AND chunk.tenant_id = %s
                  AND chunk.user_id = %s
                  AND document.deleted_at IS NULL
                """,
                (str(chunk_id), str(document_id), tenant_id, user_id),
            )
            evidence_row = cursor.fetchone()

    if not evidence_row:
        raise HTTPException(status_code=404, detail="Evidence handle not found")
    try:
        visual_target = None
        source_mime_type = str(evidence_row["source_mime_type"]).split(";", 1)[0].lower()
        if (
            source_mime_type == "application/pdf"
            and evidence_row["text_locator_status"] == "exact"
            and evidence_row["layout_artifact_id"] is not None
        ):
            visual_target = resolve_pdf_visual_target(
                evidence_row,
                ready_original_storage(),
            )
        envelope = build_evidence_envelope(evidence_row, visual_target)
    except (EvidenceIntegrityError, LayoutEvidenceIntegrityError) as error:
        logger.error("Evidence handle %s failed closed: %s", chunk_id, error)
        raise HTTPException(
            status_code=409,
            detail={
                "code": "evidence_unverifiable",
                "message": "The stored evidence handle could not be verified exactly.",
            },
        ) from error
    except (ObjectNotFoundError, ObjectIntegrityError) as error:
        logger.error("Evidence layout object %s failed closed: %s", chunk_id, error)
        raise HTTPException(
            status_code=409,
            detail={
                "code": "evidence_visual_unverifiable",
                "message": "The stored PDF layout proof could not be verified exactly.",
            },
        ) from error
    except ObjectStorageError as error:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "evidence_storage_unavailable",
                "message": "Evidence storage is temporarily unavailable.",
            },
        ) from error

    if envelope["visual_target"]["status"] == "verified":
        with get_db() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE document_layout_artifacts
                    SET last_verified_at = NOW(), last_error = NULL, updated_at = NOW()
                    WHERE id = %s
                      AND tenant_id = %s AND user_id = %s
                      AND status = 'ready'
                    """,
                    (str(evidence_row["layout_artifact_id"]), tenant_id, user_id),
                )
                if cursor.rowcount != 1:
                    raise HTTPException(
                        status_code=409,
                        detail={
                            "code": "evidence_visual_state_changed",
                            "message": "The PDF layout proof changed state during verification.",
                        },
                    )

    return JSONResponse(
        content=jsonable_encoder(envelope),
        headers={
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


@app.get("/documents/{document_id}/original")
def download_document_original(
    document_id: str,
    tenant_id: str = Header(..., alias="X-Certus-Tenant-Id"),
    user_id: str = Header(..., alias="X-Certus-User-Id"),
    version: Optional[int] = Query(None, ge=1),
    disposition: Literal["attachment", "inline"] = Query("attachment"),
):
    with get_db() as connection:
        with connection.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute(
                """
                SELECT source.id, source.storage_backend, source.bucket,
                       source.object_key, source.object_version_id,
                       source.original_filename, source.claimed_mime_type,
                       source.byte_length, source.content_sha256,
                       source.checksum_sha256_base64, source.etag,
                       source.storage_class, source.server_side_encryption,
                       source.kms_key_id, source.bucket_key_enabled,
                       source.status, source.unavailable_reason
                FROM documents AS document
                JOIN document_versions AS selected_version
                  ON selected_version.document_id = document.id
                 AND selected_version.tenant_id = document.tenant_id
                 AND selected_version.user_id = document.user_id
                 AND (
                      (%s IS NULL AND selected_version.id = document.current_version_id)
                      OR selected_version.version_number = %s
                 )
                JOIN document_source_objects AS source
                  ON source.document_version_id = selected_version.id
                 AND source.document_id = document.id
                 AND source.tenant_id = document.tenant_id
                 AND source.user_id = document.user_id
                WHERE document.id = %s
                  AND document.tenant_id = %s AND document.user_id = %s
                  AND document.deleted_at IS NULL
                """,
                (version, version, document_id, tenant_id, user_id),
            )
            source = cursor.fetchone()

    if not source:
        raise HTTPException(status_code=404, detail="Document version not found")
    if source["status"] == "unavailable":
        raise HTTPException(
            status_code=409,
            detail={
                "code": "original_unavailable",
                "message": source["unavailable_reason"] or "The exact original was not retained.",
            },
        )
    if source["status"] in {"delete_pending", "deleted"}:
        raise HTTPException(status_code=410, detail="The exact original is no longer available.")
    if source["status"] != "available" or source["storage_backend"] != "s3":
        raise HTTPException(status_code=503, detail="The exact original is temporarily unavailable.")

    stored = StoredObject(
        bucket=source["bucket"],
        object_key=source["object_key"],
        object_version_id=source["object_version_id"],
        byte_length=int(source["byte_length"]),
        sha256_hex=source["content_sha256"],
        checksum_sha256_base64=source["checksum_sha256_base64"],
        etag=source["etag"],
        storage_class=source["storage_class"],
        server_side_encryption=source["server_side_encryption"],
        kms_key_id=source["kms_key_id"],
        bucket_key_enabled=source["bucket_key_enabled"],
    )
    try:
        spool = ready_original_storage().download_verified_to_spool(stored)
    except ObjectNotFoundError as error:
        with get_db() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE document_source_objects
                    SET status = 'missing', last_error = %s, updated_at = NOW()
                    WHERE id = %s AND status = 'available'
                    """,
                    (str(error)[:2000], str(source["id"])),
                )
        raise HTTPException(status_code=503, detail="The exact original is missing from storage.") from error
    except ObjectIntegrityError as error:
        with get_db() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE document_source_objects
                    SET status = 'error', last_error = %s, updated_at = NOW()
                    WHERE id = %s AND status IN ('available', 'missing', 'error')
                    """,
                    (str(error)[:2000], str(source["id"])),
                )
        raise HTTPException(status_code=500, detail="The exact original failed integrity verification.") from error
    except ObjectStorageError as error:
        raise HTTPException(status_code=503, detail="Original storage is temporarily unavailable.") from error

    try:
        with get_db() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE document_source_objects
                    SET last_verified_at = NOW(), last_error = NULL, updated_at = NOW()
                    WHERE id = %s AND status = 'available'
                    """,
                    (str(source["id"]),),
                )
                if cursor.rowcount != 1:
                    raise RuntimeError("Original source state changed during verification")
    except Exception:
        spool.close()
        raise

    def stream_verified_original():
        try:
            while chunk := spool.read(1024 * 1024):
                yield chunk
        finally:
            spool.close()

    return StreamingResponse(
        stream_verified_original(),
        media_type=source["claimed_mime_type"],
        headers={
            "Content-Disposition": original_content_disposition(
                source["original_filename"],
                disposition,
            ),
            "Content-Length": str(stored.byte_length),
            "Content-Digest": f"sha-256=:{stored.checksum_sha256_base64}:",
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


@app.post("/documents/{document_id}/rechunk")
def rechunk_document(
    document_id: str,
    request: RechunkRequest,
    tenant_id: str = Header(..., alias="X-Certus-Tenant-Id"),
    user_id: str = Header(..., alias="X-Certus-User-Id"),
    _capacity: None = Depends(acquire_processing_capacity),
):
    # Read immutable source material without a row lock. CPU chunking happens
    # outside the publication transaction, then the same pointers/state are
    # revalidated under FOR UPDATE immediately before the shadow derivation is
    # committed.
    with get_db() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute(
                """
                SELECT document.id, document.current_version_id,
                       version.version_number, version.title,
                       version.parser_profile, version.current_derivation_id,
                       version.pending_derivation_id, version.status,
                       parsed.id AS parsed_artifact_id,
                       parsed.content_text AS parsed_text
                FROM documents AS document
                JOIN document_versions AS version
                  ON version.id = document.current_version_id
                 AND version.document_id = document.id
                JOIN document_parsed_artifacts AS parsed
                  ON parsed.document_version_id = version.id
                 AND parsed.document_id = document.id
                 AND parsed.tenant_id = document.tenant_id
                 AND parsed.user_id = document.user_id
                WHERE document.id = %s
                  AND document.tenant_id = %s
                  AND document.user_id = %s
                  AND document.deleted_at IS NULL
                """,
                (document_id, tenant_id, user_id),
            )
            document = cursor.fetchone()
            if not document:
                raise HTTPException(status_code=404, detail="Document not found")
            if document["pending_derivation_id"] is not None:
                raise HTTPException(
                    status_code=409,
                    detail="A replacement derivation is already being built for this version.",
                )
            if document["status"] != "ready":
                raise HTTPException(
                    status_code=409,
                    detail="Only a ready document version can be re-chunked.",
                )
            if not (document["parsed_text"] or "").strip():
                raise HTTPException(status_code=422, detail="Document has no extractable text to re-chunk")

    normalized_chunk_strategy = ChunkerFactory.normalize_strategy(request.strategy)
    chunker = ChunkerFactory.get_chunker(normalized_chunk_strategy)
    chunker_provenance = chunker_profile(normalized_chunk_strategy, chunker)
    chunks = chunker.chunk(
        text=document["parsed_text"],
        document_title=document["title"],
        section_title="Re-chunked content",
    )
    if not chunks:
        raise HTTPException(status_code=422, detail="Document has no extractable text to re-chunk")

    processing_generation = str(uuid.uuid4())
    derivation_id = str(uuid.uuid4())
    document_version_id = str(document["current_version_id"])
    parsed_artifact_id = str(document["parsed_artifact_id"])
    with get_db() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute(
                """
                SELECT document.current_version_id,
                       version.pending_derivation_id, version.status,
                       parsed.id AS parsed_artifact_id
                FROM documents AS document
                JOIN document_versions AS version
                  ON version.id = document.current_version_id
                 AND version.document_id = document.id
                JOIN document_parsed_artifacts AS parsed
                  ON parsed.document_version_id = version.id
                 AND parsed.document_id = document.id
                 AND parsed.tenant_id = document.tenant_id
                 AND parsed.user_id = document.user_id
                WHERE document.id = %s
                  AND document.tenant_id = %s
                  AND document.user_id = %s
                  AND document.deleted_at IS NULL
                FOR UPDATE OF document, version
                """,
                (document_id, tenant_id, user_id),
            )
            current = cursor.fetchone()
            if not current:
                raise HTTPException(status_code=404, detail="Document not found")
            if (
                str(current["current_version_id"]) != document_version_id
                or str(current["parsed_artifact_id"]) != parsed_artifact_id
            ):
                raise HTTPException(
                    status_code=409,
                    detail="The document source changed while re-chunking; retry against the current version.",
                )
            if current["pending_derivation_id"] is not None:
                raise HTTPException(
                    status_code=409,
                    detail="A replacement derivation is already being built for this version.",
                )
            if current["status"] != "ready":
                raise HTTPException(
                    status_code=409,
                    detail="Only a ready document version can be re-chunked.",
                )
            register_embedding_profile(cursor, ACTIVE_EMBEDDING_PROFILE)
            cursor.execute(
                """
                INSERT INTO document_derivations (
                    id, document_version_id, document_id, tenant_id, user_id,
                    processing_generation, input_text_hash,
                    input_parsed_artifact_id, parser_profile,
                    chunker_profile, embedding_profile, status,
                    chunk_count, processing_total_chunks
                ) VALUES (
                    %s, %s, %s, %s, %s,
                    %s, %s, %s, %s::jsonb,
                    %s::jsonb, %s, 'processing', 0, %s
                )
                """,
                (
                    derivation_id, document_version_id, document_id,
                    tenant_id, user_id, processing_generation,
                    sha256_text(document["parsed_text"]),
                    parsed_artifact_id,
                    json.dumps(dict(document["parser_profile"] or {})),
                    json.dumps(chunker_provenance),
                    ACTIVE_EMBEDDING_PROFILE.identifier,
                    len(chunks),
                ),
            )
            cursor.execute(
                """
                UPDATE document_versions
                SET pending_derivation_id = %s,
                    updated_at = NOW()
                WHERE id = %s AND document_id = %s
                """,
                (
                    derivation_id,
                    document_version_id,
                    document_id,
                ),
            )
            cursor.execute(
                """
                UPDATE documents
                SET metadata = (metadata - 'last_rechunk_error') || %s::jsonb,
                    updated_at = NOW()
                WHERE id = %s AND tenant_id = %s AND user_id = %s
                  AND deleted_at IS NULL
                """,
                (
                    json.dumps({
                        "pending_chunk_strategy": normalized_chunk_strategy,
                        "rechunk_requested_at": datetime.now(timezone.utc).isoformat(),
                    }),
                    document_id,
                    tenant_id,
                    user_id,
                ),
            )
            stage_document_processing(
                cursor,
                document_id,
                document_version_id,
                derivation_id,
                user_id,
                tenant_id,
                processing_generation,
                chunks,
            )

    return {
        "document_id": document_id,
        "document_version_id": document_version_id,
        "version_number": int(document["version_number"]),
        "derivation_id": derivation_id,
        "status": "processing",
        "chunk_count": len(chunks),
        "chunk_strategy": normalized_chunk_strategy,
        "message": (
            "A replacement derivation was durably queued. "
            "The current indexed evidence remains searchable until the atomic swap."
        ),
    }


@app.delete("/documents/{document_id}")
def delete_document(
    document_id: str,
    tenant_id: str = Header(..., alias="X-Certus-Tenant-Id"),
    user_id: str = Header(..., alias="X-Certus-User-Id"),
):
    deleted_at = datetime.now(timezone.utc)
    with get_db() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute(
                """
                SELECT id
                FROM document_embedding_jobs
                WHERE document_id = %s
                  AND tenant_id = %s AND user_id = %s
                  AND status IN ('pending', 'publishing', 'published', 'processing')
                ORDER BY id
                FOR UPDATE
                """,
                (document_id, tenant_id, user_id),
            )
            cursor.fetchall()
            cursor.execute(
                """
                UPDATE documents
                SET status = 'deleted', deleted_at = %s, updated_at = NOW()
                WHERE id = %s AND tenant_id = %s AND user_id = %s
                  AND deleted_at IS NULL
                RETURNING id
                """,
                (deleted_at, document_id, tenant_id, user_id),
            )
            deleted = cursor.fetchone()
            if not deleted:
                raise HTTPException(status_code=404, detail="Document not found")
            cursor.execute(
                """
                UPDATE document_embedding_jobs
                SET status = 'obsolete', locked_at = NULL, processing_owner = NULL,
                    processed_at = COALESCE(processed_at, NOW()), updated_at = NOW()
                WHERE document_id = %s
                  AND tenant_id = %s AND user_id = %s
                  AND status IN ('pending', 'publishing', 'published', 'processing')
                """,
                (document_id, tenant_id, user_id),
            )

    graph_sync = EntityExtractor.mark_document_deleted(
        document_id,
        user_id,
        tenant_id,
        deleted_at.isoformat(),
    )
    return {
        "document_id": document_id,
        "status": "deleted",
        "deleted_at": deleted_at.isoformat(),
        "graph_sync": graph_sync,
    }

if __name__ == "__main__":
    port = int(os.getenv("PORT", 8001))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=True)
