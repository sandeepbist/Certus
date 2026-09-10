import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Response
from psycopg2.extras import Json

from app.core.config import settings
from app.core.db import get_db_cursor
from app.core.identity import RequestIdentity, require_request_identity
from app.retrieval.export_archive import (
    ExportArchiveLimitExceeded,
    ExportCollectionBudget,
    build_export_archive,
    record_counts,
)

router = APIRouter(prefix="", tags=["Data Portability & GDPR Compliance"])

EXPORT_RETENTION_HOURS = 48


class _BudgetedExportCursor:
    """Apply collection limits while retaining the cursor API used below."""

    def __init__(self, cursor, budget: ExportCollectionBudget):
        self._cursor = cursor
        self._budget = budget

    def execute(self, query: str, params: tuple | None = None):
        return self._cursor.execute(query, params)

    def fetchone(self):
        return self._cursor.fetchone()

    def fetchall(self) -> list:
        rows = []
        while batch := self._cursor.fetchmany(settings.EXPORT_FETCH_BATCH_SIZE):
            for row in batch:
                self._budget.add(dict(row))
            rows.extend(batch)
        return rows


def _configure_export_transaction(cursor, identity: RequestIdentity) -> None:
    """Create one consistent, bounded, single-flight workspace snapshot."""
    cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
    cursor.execute(
        "SELECT set_config('statement_timeout', %s, true)",
        (f"{settings.EXPORT_STATEMENT_TIMEOUT_MS}ms",),
    )
    cursor.execute(
        """
        SELECT pg_try_advisory_xact_lock(hashtextextended(%s, 0)) AS acquired
        """,
        (f"certus:data-export:{identity.tenant_id}:{identity.user_id}",),
    )
    lock = cursor.fetchone()
    if not lock or not lock["acquired"]:
        raise HTTPException(
            status_code=409,
            detail="A data export is already being generated for this workspace user.",
        )


def _rows(cursor, query: str, params: tuple) -> list[dict]:
    cursor.execute(query, params)
    return [dict(row) for row in cursor.fetchall()]


def _row(cursor, query: str, params: tuple) -> dict | None:
    cursor.execute(query, params)
    value = cursor.fetchone()
    return dict(value) if value else None


def _collect_export_data(cursor, identity: RequestIdentity) -> dict:
    tenant_user = (identity.tenant_id, identity.user_id)
    user_tenant = (identity.user_id, identity.tenant_id)

    profile = _row(
        cursor,
        """
        SELECT id, name, email, "emailVerified" AS email_verified, image,
               "twoFactorEnabled" AS two_factor_enabled,
               "createdAt" AS created_at, "updatedAt" AS updated_at
        FROM "user"
        WHERE id = %s
        """,
        (identity.user_id,),
    )
    organization = _row(
        cursor,
        """
        SELECT id, name, slug, logo, metadata, "createdAt" AS created_at
        FROM organization
        WHERE id = %s
        """,
        (identity.tenant_id,),
    )
    membership = _row(
        cursor,
        """
        SELECT id, "organizationId" AS organization_id, "userId" AS user_id,
               role, "createdAt" AS created_at
        FROM member
        WHERE "organizationId" = %s AND "userId" = %s
        """,
        tenant_user,
    )
    preferences = _row(
        cursor,
        """
        SELECT id, user_id, organization_id, theme, default_model,
               default_chunk_strategy, notification_email, notification_in_app,
               notification_digest, token_used_today, budget_reset_at,
               onboarding_completed, created_at, updated_at
        FROM user_preferences
        WHERE user_id = %s AND organization_id = %s
        """,
        user_tenant,
    )
    tenant_config = _row(
        cursor,
        """
        SELECT id, organization_id, plan, max_documents, max_storage_bytes,
               max_token_budget_daily, created_at, updated_at
        FROM tenant_config
        WHERE organization_id = %s
        """,
        (identity.tenant_id,),
    )

    documents = _rows(
        cursor,
        """
        SELECT id, user_id, tenant_id, title, source_type, mime_type,
               file_size_bytes, content_hash, chunk_count,
               processing_total_chunks, entity_count, tags, status,
               processing_generation, current_version_id, error_message, metadata,
               created_at, updated_at, deleted_at
        FROM documents
        WHERE tenant_id = %s AND user_id = %s
        ORDER BY created_at
        """,
        tenant_user,
    )
    document_versions = _rows(
        cursor,
        """
        SELECT id, document_id, tenant_id, user_id, version_number, title,
               source_type, mime_type, file_size_bytes, content_hash,
               tags, source_time, source_time_origin, recorded_at, status,
               error_message, processing_generation, current_derivation_id,
               pending_derivation_id, parser_profile, source_metadata,
               created_at, updated_at
        FROM document_versions
        WHERE tenant_id = %s AND user_id = %s
        ORDER BY document_id, version_number
        """,
        tenant_user,
    )
    document_source_objects = _rows(
        cursor,
        """
        SELECT id, document_version_id, document_id, tenant_id, user_id,
               storage_backend, original_filename, claimed_mime_type,
               detected_mime_type, byte_length, content_sha256,
               checksum_sha256_base64, storage_class, server_side_encryption,
               bucket_key_enabled, status, unavailable_reason, last_error,
               stored_at, last_verified_at, delete_requested_at, deleted_at,
               retention_blocked_until, created_at, updated_at
        FROM document_source_objects
        WHERE tenant_id = %s AND user_id = %s
        ORDER BY document_id, document_version_id, created_at, id
        """,
        tenant_user,
    )
    document_parsed_artifacts = _rows(
        cursor,
        """
        SELECT id, source_object_id, document_version_id, document_id,
               tenant_id, user_id, content_text, content_sha256, byte_length,
               mime_type, producer_profile, status, unavailable_reason,
               created_at
        FROM document_parsed_artifacts
        WHERE tenant_id = %s AND user_id = %s
        ORDER BY document_id, document_version_id, created_at, id
        """,
        tenant_user,
    )
    document_layout_artifacts = _rows(
        cursor,
        """
        SELECT id, source_object_id, parsed_artifact_id, document_version_id,
               document_id, tenant_id, user_id, artifact_type, mime_type,
               content_encoding, byte_length, uncompressed_byte_length,
               content_sha256, canonical_content_sha256,
               checksum_sha256_base64, storage_class, server_side_encryption,
               bucket_key_enabled, layout_schema_version, producer_profile,
               coordinate_system, page_join_contract, page_count,
               text_run_count, status, last_error, stored_at,
               last_verified_at, delete_requested_at, deleted_at,
               created_at, updated_at
        FROM document_layout_artifacts
        WHERE tenant_id = %s AND user_id = %s
        ORDER BY document_id, document_version_id, created_at, id
        """,
        tenant_user,
    )
    document_layout_pages = _rows(
        cursor,
        """
        SELECT id, layout_artifact_id, parsed_artifact_id,
               document_version_id, document_id, tenant_id, user_id,
               page_index, page_label, width_points, height_points,
               rotation_degrees, media_x0, media_y0, media_x1, media_y1,
               crop_x0, crop_y0, crop_x1, crop_y1,
               parsed_start, parsed_end, extraction_status, created_at
        FROM document_layout_pages
        WHERE tenant_id = %s AND user_id = %s
        ORDER BY document_id, document_version_id, page_index
        """,
        tenant_user,
    )
    document_text_runs = _rows(
        cursor,
        """
        SELECT run.id, run.layout_artifact_id, run.layout_page_id,
               run.page_index, run.parsed_start, run.parsed_end,
               run.reading_order, run.source_block_index,
               run.line_index, run.span_index, run.text_sha256,
               run.bbox_x0, run.bbox_y0, run.bbox_x1, run.bbox_y1,
               run.quad_ul_x, run.quad_ul_y, run.quad_ur_x, run.quad_ur_y,
               run.quad_ll_x, run.quad_ll_y, run.quad_lr_x, run.quad_lr_y,
               run.direction_x, run.direction_y, run.writing_mode,
               run.created_at
        FROM document_text_runs AS run
        JOIN document_layout_pages AS page ON page.id = run.layout_page_id
        WHERE page.tenant_id = %s AND page.user_id = %s
        ORDER BY page.document_id, page.document_version_id,
                 run.page_index, run.reading_order
        """,
        tenant_user,
    )
    document_derivations = _rows(
        cursor,
        """
        SELECT id, document_version_id, document_id, tenant_id, user_id,
               processing_generation, artifact_type, input_text_hash,
               input_parsed_artifact_id,
               parser_profile, chunker_profile, embedding_profile, status,
               chunk_count, processing_total_chunks, error_message, created_at,
               updated_at, completed_at, superseded_at
        FROM document_derivations
        WHERE tenant_id = %s AND user_id = %s
        ORDER BY document_id, document_version_id, created_at, id
        """,
        tenant_user,
    )
    chunks = _rows(
        cursor,
        """
        SELECT id, document_id, document_version_id, derivation_id,
               user_id, tenant_id, content,
               contextualized_content, embedding::text AS embedding,
               chunk_index, token_count, start_char, end_char, page_number,
               section_title, language, processing_generation, embedding_profile,
               text_locator_status, text_locator_profile,
               text_locator_unavailable_reason,
               embedded_at,
               metadata, created_at
        FROM chunks
        WHERE tenant_id = %s AND user_id = %s
        ORDER BY document_id, document_version_id, derivation_id, chunk_index
        """,
        tenant_user,
    )
    document_embedding_jobs = _rows(
        cursor,
        """
        SELECT id, document_id, document_version_id, derivation_id, tenant_id,
               user_id, processing_generation, embedding_profile, batch_start,
               batch_end, total_chunks, status, available_at, publish_attempts,
               locked_at, redis_stream_id, published_at, processed_at,
               last_error, created_at, updated_at
        FROM document_embedding_jobs
        WHERE tenant_id = %s AND user_id = %s
        ORDER BY document_id, document_version_id, derivation_id, batch_start
        """,
        tenant_user,
    )
    embedding_profiles = _rows(
        cursor,
        """
        SELECT profile.identifier, profile.schema_version, profile.provider,
               profile.model, profile.dimensions, profile.distance_metric,
               profile.normalization_profile, profile.input_profile,
               profile.storage_profile, profile.registered_at
        FROM embedding_profiles AS profile
        WHERE EXISTS (
                  SELECT 1 FROM chunks AS chunk
                  WHERE chunk.embedding_profile = profile.identifier
                    AND chunk.tenant_id = %s AND chunk.user_id = %s
              )
           OR EXISTS (
                  SELECT 1 FROM document_embedding_jobs AS job
                  WHERE job.embedding_profile = profile.identifier
                    AND job.tenant_id = %s AND job.user_id = %s
              )
           OR EXISTS (
                  SELECT 1 FROM document_derivations AS derivation
                  WHERE derivation.embedding_profile = profile.identifier
                    AND derivation.tenant_id = %s AND derivation.user_id = %s
              )
           OR EXISTS (
                  SELECT 1 FROM memories AS memory
                  WHERE memory.embedding_profile = profile.identifier
                    AND memory.tenant_id = %s AND memory.user_id = %s
              )
        ORDER BY profile.identifier
        """,
        tenant_user * 4,
    )
    chat_sessions = _rows(
        cursor,
        """
        SELECT id, user_id, organization_id, title, is_pinned,
               created_at, last_active_at
        FROM chat_sessions
        WHERE organization_id = %s AND user_id = %s
        ORDER BY created_at
        """,
        tenant_user,
    )
    tasks = _rows(
        cursor,
        """
        SELECT id, user_id, tenant_id, title, description, status, priority,
               due_date, tags, source_document_id, source_agent_run_id,
               trigger_rule, version, created_at, updated_at
        FROM tasks
        WHERE tenant_id = %s AND user_id = %s
        ORDER BY created_at
        """,
        tenant_user,
    )
    agent_runs = _rows(
        cursor,
        """
        SELECT id, user_id, tenant_id, session_id, input_query, plan, model_used,
               retrieved_chunk_ids, output_response, citations, claim_evidence,
               answer_status, grounding_profile, evidence_manifest,
               evidence_manifest_db_sha256, generation_profile,
               generation_profile_db_sha256, conversation_context,
               conversation_context_db_sha256, replay_of_run_id, replay_mode,
               prompt_tokens,
               completion_tokens, total_tokens, estimated_cost_usd, latency_ms,
               retrieval_latency_ms, llm_latency_ms, eval_score, eval_details,
               critic_iterations, events, status, reserved_tokens,
               error_message, created_at, completed_at
        FROM agent_runs
        WHERE tenant_id = %s AND user_id = %s
        ORDER BY created_at
        """,
        tenant_user,
    )
    memories = _rows(
        cursor,
        """
        SELECT id, user_id, tenant_id, fact, category, confidence, source_run_id,
               embedding::text AS embedding, embedding_provider, embedding_profile,
               access_count,
               last_accessed_at, is_active, superseded_by, created_at
        FROM memories
        WHERE tenant_id = %s AND user_id = %s
        ORDER BY created_at
        """,
        tenant_user,
    )
    reminders = _rows(
        cursor,
        """
        SELECT id, user_id, tenant_id, workflow_id, workflow_run_id,
               notification_id, message, scheduled_for, status, error_message,
               created_at, delivered_at, updated_at
        FROM reminders
        WHERE tenant_id = %s AND user_id = %s
        ORDER BY created_at
        """,
        tenant_user,
    )
    automation_rules = _rows(
        cursor,
        """
        SELECT id, user_id, tenant_id, name, description, trigger_event,
               trigger_conditions, actions, is_enabled, execution_count,
               last_executed_at, version, created_at, updated_at
        FROM automation_rules
        WHERE tenant_id = %s AND user_id = %s
        ORDER BY created_at
        """,
        tenant_user,
    )
    automation_executions = _rows(
        cursor,
        """
        SELECT id, rule_id, user_id, tenant_id, trigger_event, trigger_event_id,
               workflow_id, workflow_run_id, source_document_id, created_task_id,
               status, result, error_message, started_at, completed_at, updated_at
        FROM automation_executions
        WHERE tenant_id = %s AND user_id = %s
        ORDER BY started_at
        """,
        tenant_user,
    )
    notifications = _rows(
        cursor,
        """
        SELECT id, user_id, organization_id, type, title, body, metadata,
               action_url, is_read, created_at
        FROM notifications
        WHERE organization_id = %s AND user_id = %s
        ORDER BY created_at
        """,
        tenant_user,
    )
    notification_events = _rows(
        cursor,
        """
        SELECT id, notification_id, user_id, organization_id, event_type,
               payload, status, publish_attempts, available_at, published_at,
               redis_stream_id, last_error, created_at, updated_at
        FROM notification_events
        WHERE organization_id = %s AND user_id = %s
        ORDER BY created_at
        """,
        tenant_user,
    )
    realtime_events = _rows(
        cursor,
        """
        SELECT id, user_id, organization_id, channel, event_type, payload,
               status, publish_attempts, available_at, published_at,
               redis_stream_id, last_error, created_at, updated_at
        FROM realtime_events
        WHERE organization_id = %s AND user_id = %s
        ORDER BY created_at
        """,
        tenant_user,
    )
    webhooks = _rows(
        cursor,
        """
        SELECT id, user_id, organization_id, url, events, is_enabled,
               last_triggered_at, failure_count, created_at
        FROM webhooks
        WHERE organization_id = %s AND user_id = %s
        ORDER BY created_at
        """,
        tenant_user,
    )
    webhook_deliveries = _rows(
        cursor,
        """
        SELECT delivery.id, delivery.delivery_key, delivery.event_id,
               delivery.webhook_id, delivery.event_type,
               delivery.payload, delivery.response_status, delivery.response_body,
               delivery.delivered_at, delivery.success, delivery.attempt_number,
               delivery.duration_ms, delivery.error_message
        FROM webhook_deliveries AS delivery
        JOIN webhooks AS webhook ON webhook.id = delivery.webhook_id
        WHERE webhook.organization_id = %s AND webhook.user_id = %s
        ORDER BY delivery.delivered_at
        """,
        tenant_user,
    )
    webhook_events = _rows(
        cursor,
        """
        SELECT id, user_id, organization_id, event_type, idempotency_key,
               payload, target_webhook_ids, status, workflow_id, workflow_run_id, dispatch_attempts,
               available_at, dispatched_at, completed_at, result, last_error,
               created_at, updated_at
        FROM webhook_events
        WHERE organization_id = %s AND user_id = %s
        ORDER BY created_at
        """,
        tenant_user,
    )
    semantic_cache = _rows(
        cursor,
        """
        SELECT id, user_id, tenant_id, query_embedding::text AS query_embedding,
               query_text, response_text, response_citations, model_used,
               hit_count, created_at, expires_at
        FROM semantic_cache
        WHERE tenant_id = %s AND user_id = %s
        ORDER BY created_at
        """,
        tenant_user,
    )

    return {
        "profile": profile,
        "organization": organization,
        "membership": membership,
        "preferences": preferences,
        "tenant_config": tenant_config,
        "documents": documents,
        "document_versions": document_versions,
        "document_source_objects": document_source_objects,
        "document_parsed_artifacts": document_parsed_artifacts,
        "document_layout_artifacts": document_layout_artifacts,
        "document_layout_pages": document_layout_pages,
        "document_text_runs": document_text_runs,
        "document_derivations": document_derivations,
        "chunks": chunks,
        "document_embedding_jobs": document_embedding_jobs,
        "embedding_profiles": embedding_profiles,
        "chat_sessions": chat_sessions,
        "tasks": tasks,
        "agent_runs": agent_runs,
        "memories": memories,
        "reminders": reminders,
        "automation_rules": automation_rules,
        "automation_executions": automation_executions,
        "notifications": notifications,
        "notification_events": notification_events,
        "realtime_events": realtime_events,
        "webhooks": webhooks,
        "webhook_events": webhook_events,
        "webhook_deliveries": webhook_deliveries,
        "semantic_cache": semantic_cache,
    }


@router.post("/export")
def generate_data_export(identity: RequestIdentity = Depends(require_request_identity)):
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(hours=EXPORT_RETENTION_HOURS)
    export_id = str(uuid.uuid4())
    file_name = f"certus-data-export-{now.date().isoformat()}.zip"

    with get_db_cursor() as cursor:
        _configure_export_transaction(cursor, identity)
        cursor.execute(
            """
            UPDATE data_exports
            SET status = 'expired', archive = NULL
            WHERE organization_id = %s
              AND user_id = %s
              AND expires_at <= %s
              AND archive IS NOT NULL
            """,
            (identity.tenant_id, identity.user_id, now),
        )
        budget = ExportCollectionBudget(
            max_source_bytes=settings.MAX_EXPORT_SOURCE_BYTES,
            max_records=settings.MAX_EXPORT_RECORDS,
        )
        try:
            data = _collect_export_data(_BudgetedExportCursor(cursor, budget), identity)
            counts = record_counts(data)
            payload = {
                "manifest": {
                    "schema_version": 9,
                    "product": "Certus",
                    "exported_at": now,
                    "tenant_id": identity.tenant_id,
                    "user_id": identity.user_id,
                    "credentials_excluded": [
                        "password hashes",
                        "session tokens",
                        "API key hashes",
                        "TOTP secrets",
                        "passkey credential material",
                        "webhook signing secrets",
                    ],
                    "record_counts": counts,
                },
                "data": data,
            }
            archive = build_export_archive(
                payload,
                max_uncompressed_bytes=settings.MAX_EXPORT_SOURCE_BYTES,
            )
        except ExportArchiveLimitExceeded as exc:
            raise HTTPException(
                status_code=413,
                detail=(
                    "This workspace exceeds the bounded synchronous export limit. "
                    "Use the asynchronous object-storage export path for large workspaces."
                ),
            ) from exc
        if len(archive) > settings.MAX_EXPORT_ARCHIVE_BYTES:
            raise HTTPException(
                status_code=413,
                detail="The compressed export exceeds the configured archive limit.",
            )

        cursor.execute(
            """
            INSERT INTO data_exports (
                id, user_id, organization_id, status, format, file_name,
                size_bytes, archive, record_counts, expires_at, completed_at
            )
            VALUES (%s, %s, %s, 'ready', 'application/zip', %s, %s, %s, %s, %s, %s)
            """,
            (
                export_id,
                identity.user_id,
                identity.tenant_id,
                file_name,
                len(archive),
                archive,
                Json(counts),
                expires_at,
                now,
            ),
        )

    return {
        "export_id": export_id,
        "status": "ready",
        "format": "application/zip",
        "file_name": file_name,
        "archive_size_bytes": len(archive),
        "record_counts": counts,
        "expires_at": expires_at,
        "download_url": f"/export/{export_id}",
    }


@router.get("/export/{export_id}")
def download_data_export(
    export_id: uuid.UUID,
    identity: RequestIdentity = Depends(require_request_identity),
):
    now = datetime.now(timezone.utc)
    with get_db_cursor() as cursor:
        export = _row(
            cursor,
            """
            SELECT id, status, format, file_name, archive, expires_at
            FROM data_exports
            WHERE id = %s AND organization_id = %s AND user_id = %s
            FOR UPDATE
            """,
            (str(export_id), identity.tenant_id, identity.user_id),
        )
        if not export:
            raise HTTPException(status_code=404, detail="Export not found")
        if export["expires_at"] <= now:
            cursor.execute(
                "UPDATE data_exports SET status = 'expired', archive = NULL WHERE id = %s",
                (str(export_id),),
            )
            raise HTTPException(status_code=410, detail="Export has expired")
        if export["status"] != "ready" or export["archive"] is None:
            raise HTTPException(status_code=409, detail="Export is not ready")

        cursor.execute(
            "UPDATE data_exports SET downloaded_at = %s WHERE id = %s",
            (now, str(export_id)),
        )
        archive = bytes(export["archive"])

    return Response(
        content=archive,
        media_type=export["format"],
        headers={
            "Content-Disposition": f'attachment; filename="{export["file_name"]}"',
            "Content-Length": str(len(archive)),
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )
