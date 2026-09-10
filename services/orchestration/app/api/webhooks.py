import uuid

import psycopg2
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, field_validator

from app.core.db import get_db_cursor
from app.core.identity import RequestIdentity, require_request_identity
from app.retrieval.webhook_delivery import (
    SUPPORTED_WEBHOOK_EVENTS,
    UnsafeWebhookTarget,
    WebhookConfigurationError,
    deliver_webhook,
    encrypt_signing_secret,
    new_signing_secret,
    normalize_events,
    signing_secret_cipher,
    validate_webhook_url,
)


router = APIRouter(prefix="", tags=["Outbound Webhooks"])


class WebhookCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    url: str = Field(min_length=1, max_length=500)
    events: list[str] = Field(min_length=1, max_length=len(SUPPORTED_WEBHOOK_EVENTS))

    @field_validator("name")
    @classmethod
    def clean_name(cls, value: str) -> str:
        clean = " ".join(value.split())
        if not clean:
            raise ValueError("Webhook name cannot be blank")
        return clean

    @field_validator("events")
    @classmethod
    def clean_events(cls, value: list[str]) -> list[str]:
        return normalize_events(value)


class WebhookUpdate(BaseModel):
    version: int = Field(ge=1)
    name: str | None = Field(default=None, min_length=1, max_length=100)
    url: str | None = Field(default=None, min_length=1, max_length=500)
    events: list[str] | None = Field(default=None, min_length=1, max_length=len(SUPPORTED_WEBHOOK_EVENTS))
    is_enabled: bool | None = None

    @field_validator("name")
    @classmethod
    def clean_name(cls, value: str | None) -> str | None:
        return " ".join(value.split()) if value is not None else None

    @field_validator("events")
    @classmethod
    def clean_events(cls, value: list[str] | None) -> list[str] | None:
        return normalize_events(value) if value is not None else None


WEBHOOK_FIELDS = """
    id, name, url, events, secret_hint, is_enabled, last_triggered_at,
    failure_count, last_error, version, created_at, updated_at
"""


def _delivery_ready() -> None:
    try:
        signing_secret_cipher()
    except WebhookConfigurationError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error


def _safe_url(value: str) -> str:
    try:
        return validate_webhook_url(value)
    except UnsafeWebhookTarget as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@router.get("/webhooks")
def list_webhooks(identity: RequestIdentity = Depends(require_request_identity)):
    with get_db_cursor() as cursor:
        cursor.execute(
            f"""
            SELECT {WEBHOOK_FIELDS},
                   (SELECT COUNT(*) FROM webhook_deliveries delivery WHERE delivery.webhook_id = webhooks.id) AS delivery_count,
                   (SELECT COUNT(*) FROM webhook_deliveries delivery WHERE delivery.webhook_id = webhooks.id AND delivery.success) AS success_count
            FROM webhooks
            WHERE organization_id = %s AND user_id = %s
            ORDER BY created_at DESC, id DESC
            """,
            (identity.tenant_id, identity.user_id),
        )
        webhooks = [dict(webhook) for webhook in cursor.fetchall()]
    return {"webhooks": webhooks, "supported_events": list(SUPPORTED_WEBHOOK_EVENTS)}


@router.post("/webhooks", status_code=201)
def create_webhook(
    request: WebhookCreate,
    identity: RequestIdentity = Depends(require_request_identity),
):
    _delivery_ready()
    target_url = _safe_url(request.url)
    signing_secret = new_signing_secret()
    webhook_id = str(uuid.uuid4())
    try:
        with get_db_cursor() as cursor:
            cursor.execute(
                "SELECT COUNT(*) AS total FROM webhooks WHERE organization_id = %s AND user_id = %s",
                (identity.tenant_id, identity.user_id),
            )
            if cursor.fetchone()["total"] >= 20:
                raise HTTPException(status_code=409, detail="The workspace webhook limit of 20 has been reached")
            cursor.execute(
                f"""
                INSERT INTO webhooks (
                    id, user_id, organization_id, name, url, events, secret,
                    secret_hint, is_enabled, failure_count, version, created_at, updated_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, true, 0, 1, NOW(), NOW())
                RETURNING {WEBHOOK_FIELDS}
                """,
                (
                    webhook_id,
                    identity.user_id,
                    identity.tenant_id,
                    request.name,
                    target_url,
                    request.events,
                    encrypt_signing_secret(signing_secret),
                    signing_secret[-4:],
                ),
            )
            webhook = dict(cursor.fetchone())
    except psycopg2.errors.UniqueViolation as error:
        raise HTTPException(status_code=409, detail="This webhook URL is already configured") from error
    return {"webhook": webhook, "signing_secret": signing_secret}


@router.patch("/webhooks/{webhook_id}")
def update_webhook(
    webhook_id: uuid.UUID,
    request: WebhookUpdate,
    identity: RequestIdentity = Depends(require_request_identity),
):
    updates = request.model_dump(exclude={"version"}, exclude_unset=True)
    if not updates:
        raise HTTPException(status_code=422, detail="At least one webhook field must be updated")
    if "url" in updates:
        updates["url"] = _safe_url(updates["url"])
    assignments = [f"{field} = %s" for field in updates]
    params = [*updates.values(), str(webhook_id), identity.tenant_id, identity.user_id, request.version]
    try:
        with get_db_cursor() as cursor:
            cursor.execute(
                f"""
                UPDATE webhooks
                SET {', '.join(assignments)}, version = version + 1, updated_at = NOW()
                WHERE id = %s AND organization_id = %s AND user_id = %s AND version = %s
                RETURNING {WEBHOOK_FIELDS}
                """,
                params,
            )
            webhook = cursor.fetchone()
            if not webhook:
                cursor.execute(
                    "SELECT version FROM webhooks WHERE id = %s AND organization_id = %s AND user_id = %s",
                    (str(webhook_id), identity.tenant_id, identity.user_id),
                )
                current = cursor.fetchone()
                if not current:
                    raise HTTPException(status_code=404, detail="Webhook not found")
                raise HTTPException(
                    status_code=409,
                    detail={"code": "version_conflict", "current_version": current["version"]},
                )
    except psycopg2.errors.UniqueViolation as error:
        raise HTTPException(status_code=409, detail="This webhook URL is already configured") from error
    return {"webhook": dict(webhook)}


@router.post("/webhooks/{webhook_id}/rotate-secret")
def rotate_webhook_secret(
    webhook_id: uuid.UUID,
    identity: RequestIdentity = Depends(require_request_identity),
):
    _delivery_ready()
    signing_secret = new_signing_secret()
    with get_db_cursor() as cursor:
        cursor.execute(
            f"""
            UPDATE webhooks
            SET secret = %s, secret_hint = %s, version = version + 1, updated_at = NOW()
            WHERE id = %s AND organization_id = %s AND user_id = %s
            RETURNING {WEBHOOK_FIELDS}
            """,
            (
                encrypt_signing_secret(signing_secret),
                signing_secret[-4:],
                str(webhook_id),
                identity.tenant_id,
                identity.user_id,
            ),
        )
        webhook = cursor.fetchone()
        if not webhook:
            raise HTTPException(status_code=404, detail="Webhook not found")
    return {"webhook": dict(webhook), "signing_secret": signing_secret}


@router.post("/webhooks/{webhook_id}/test")
def test_webhook(
    webhook_id: uuid.UUID,
    identity: RequestIdentity = Depends(require_request_identity),
):
    _delivery_ready()
    with get_db_cursor() as cursor:
        cursor.execute(
            """
            SELECT id, url, secret
            FROM webhooks
            WHERE id = %s AND organization_id = %s AND user_id = %s
            """,
            (str(webhook_id), identity.tenant_id, identity.user_id),
        )
        webhook = cursor.fetchone()
        if not webhook:
            raise HTTPException(status_code=404, detail="Webhook not found")
    try:
        outcome = deliver_webhook(
            dict(webhook),
            "webhook.test",
            {"message": "Certus webhook verification", "workspace_id": identity.tenant_id},
        )
    except (WebhookConfigurationError, UnsafeWebhookTarget) as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    if not outcome.success:
        raise HTTPException(
            status_code=502,
            detail={
                "code": "webhook_delivery_failed",
                "message": outcome.error_message or "The webhook endpoint rejected the test event.",
                "delivery": outcome.__dict__,
            },
        )
    return {"delivery": outcome.__dict__}


@router.get("/webhooks/{webhook_id}/deliveries")
def list_webhook_deliveries(
    webhook_id: uuid.UUID,
    limit: int = Query(default=25, ge=1, le=100),
    page_cursor: uuid.UUID | None = Query(default=None, alias="cursor"),
    identity: RequestIdentity = Depends(require_request_identity),
):
    with get_db_cursor() as cursor:
        if page_cursor is not None:
            cursor.execute(
                """
                SELECT delivery.attempted_at, delivery.id
                FROM webhook_deliveries delivery
                JOIN webhooks webhook ON webhook.id = delivery.webhook_id
                WHERE delivery.id = %s AND webhook.id = %s
                  AND webhook.organization_id = %s AND webhook.user_id = %s
                """,
                (str(page_cursor), str(webhook_id), identity.tenant_id, identity.user_id),
            )
            anchor = cursor.fetchone()
            if not anchor:
                raise HTTPException(status_code=422, detail="The webhook delivery cursor is invalid.")
        else:
            anchor = None
        page_filter = ""
        params: list[object] = [str(webhook_id), identity.tenant_id, identity.user_id]
        if anchor:
            page_filter = "AND (delivery.attempted_at, delivery.id) < (%s, %s)"
            params.extend([anchor["attempted_at"], anchor["id"]])
        params.append(limit + 1)
        cursor.execute(
            """
            SELECT delivery.id, delivery.delivery_key, delivery.event_id,
                   delivery.event_type, delivery.response_status,
                   delivery.response_body, delivery.success, delivery.error_message,
                   delivery.duration_ms, delivery.attempt_number, delivery.attempted_at
            FROM webhook_deliveries delivery
            JOIN webhooks webhook ON webhook.id = delivery.webhook_id
            WHERE webhook.id = %s AND webhook.organization_id = %s AND webhook.user_id = %s
              {page_filter}
            ORDER BY delivery.attempted_at DESC, delivery.id DESC
            LIMIT %s
            """.format(page_filter=page_filter),
            params,
        )
        rows = cursor.fetchall()
        has_more = len(rows) > limit
        deliveries = [dict(delivery) for delivery in rows[:limit]]
    return {
        "deliveries": deliveries,
        "pagination": {
            "limit": limit,
            "next_cursor": str(deliveries[-1]["id"]) if has_more else None,
        },
    }


@router.delete("/webhooks/{webhook_id}")
def delete_webhook(
    webhook_id: uuid.UUID,
    identity: RequestIdentity = Depends(require_request_identity),
):
    with get_db_cursor(dict_cursor=False) as cursor:
        cursor.execute(
            """
            DELETE FROM webhooks
            WHERE id = %s AND organization_id = %s AND user_id = %s
            RETURNING id
            """,
            (str(webhook_id), identity.tenant_id, identity.user_id),
        )
        if not cursor.fetchone():
            raise HTTPException(status_code=404, detail="Webhook not found")
    return {"deleted": True, "webhook_id": str(webhook_id)}
