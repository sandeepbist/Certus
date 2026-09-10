import json
import socket
import time
import uuid
from dataclasses import dataclass
from secrets import token_urlsafe
from typing import Any, Callable

from services.shared.webhooks import (
    SUPPORTED_WEBHOOK_EVENTS,
    UnsafeWebhookTarget,
    WebhookConfigurationError,
    canonical_payload,
    decrypt_signing_secret as decrypt_shared_secret,
    encrypt_signing_secret as encrypt_shared_secret,
    normalize_events,
    send_signed_webhook,
    signature,
    signing_secret_cipher as shared_signing_secret_cipher,
    validate_webhook_url as validate_shared_url,
    webhook_request_target as shared_request_target,
)

try:
    from app.core.config import settings
except ModuleNotFoundError:  # Repository-root unit test imports.
    from services.orchestration.app.core.config import settings


@dataclass(frozen=True)
class DeliveryOutcome:
    id: str
    success: bool
    response_status: int | None
    duration_ms: int
    error_message: str | None


def signing_secret_cipher():
    return shared_signing_secret_cipher(
        settings.WEBHOOK_ENCRYPTION_KEY,
        settings.INTERNAL_SERVICE_TOKEN,
        settings.NODE_ENV,
    )


def new_signing_secret() -> str:
    return f"whsec_{token_urlsafe(32)}"


def encrypt_signing_secret(secret: str) -> str:
    return encrypt_shared_secret(
        secret,
        settings.WEBHOOK_ENCRYPTION_KEY,
        settings.INTERNAL_SERVICE_TOKEN,
        settings.NODE_ENV,
    )


def decrypt_signing_secret(encrypted_secret: str) -> str:
    return decrypt_shared_secret(
        encrypted_secret,
        settings.WEBHOOK_ENCRYPTION_KEY,
        settings.INTERNAL_SERVICE_TOKEN,
        settings.NODE_ENV,
    )


def validate_webhook_url(
    value: str,
    *,
    allow_private: bool | None = None,
    resolver: Callable[..., list[tuple[Any, ...]]] = socket.getaddrinfo,
) -> str:
    return validate_shared_url(
        value,
        allow_private=(
            settings.WEBHOOK_ALLOW_PRIVATE_TARGETS if allow_private is None else allow_private
        ),
        resolver=resolver,
    )


def webhook_request_target(
    value: str,
    *,
    allow_private: bool | None = None,
    resolver: Callable[..., list[tuple[Any, ...]]] = socket.getaddrinfo,
) -> tuple[str, str, str]:
    return shared_request_target(
        value,
        allow_private=(
            settings.WEBHOOK_ALLOW_PRIVATE_TARGETS if allow_private is None else allow_private
        ),
        resolver=resolver,
    )


def deliver_webhook(
    webhook: dict[str, Any],
    event_type: str,
    data: dict[str, Any],
    *,
    attempt_number: int = 1,
) -> DeliveryOutcome:
    """Deliver the user-triggered test event synchronously.

    Product events use the Temporal activity in the workflow service. Both
    paths share exactly the same signing and SSRF-hardened HTTP transport.
    """
    from app.core.db import get_db_cursor

    delivery_id = str(uuid.uuid4())
    created_at = int(time.time())
    secret = decrypt_signing_secret(webhook["secret"])
    # Validate before creating a delivery row. The shared transport validates
    # again immediately before opening the socket to defend against DNS rebinding.
    webhook_request_target(webhook["url"])
    payload = json.loads(
        canonical_payload(event_type, data, delivery_id, created_at=created_at)
    )

    with get_db_cursor(dict_cursor=False) as cursor:
        cursor.execute(
            """
            INSERT INTO webhook_deliveries (
                id, webhook_id, event_type, payload, success, attempt_number,
                delivery_key, attempted_at
            ) VALUES (%s, %s, %s, %s::jsonb, false, %s, %s, NOW())
            """,
            (
                delivery_id,
                str(webhook["id"]),
                event_type,
                json.dumps(payload),
                attempt_number,
                delivery_id,
            ),
        )

    try:
        outcome = send_signed_webhook(
            url=webhook["url"],
            secret=secret,
            event_type=event_type,
            data=data,
            delivery_id=delivery_id,
            created_at=created_at,
            allow_private=settings.WEBHOOK_ALLOW_PRIVATE_TARGETS,
        )
    except (UnsafeWebhookTarget, WebhookConfigurationError) as error:
        error_message = str(error)[:500]
        with get_db_cursor(dict_cursor=False) as cursor:
            cursor.execute(
                """
                UPDATE webhook_deliveries
                SET success = false, error_message = %s, delivered_at = NOW()
                WHERE id = %s
                """,
                (error_message, delivery_id),
            )
            cursor.execute(
                """
                UPDATE webhooks
                SET last_triggered_at = NOW(), failure_count = failure_count + 1,
                    last_error = %s, updated_at = NOW()
                WHERE id = %s
                """,
                (error_message, str(webhook["id"])),
            )
        raise
    with get_db_cursor(dict_cursor=False) as cursor:
        cursor.execute(
            """
            UPDATE webhook_deliveries
            SET response_status = %s, response_body = %s, success = %s,
                error_message = %s, duration_ms = %s, delivered_at = NOW()
            WHERE id = %s
            """,
            (
                outcome.response_status,
                outcome.response_body,
                outcome.success,
                outcome.error_message,
                outcome.duration_ms,
                delivery_id,
            ),
        )
        cursor.execute(
            """
            UPDATE webhooks
            SET last_triggered_at = NOW(),
                failure_count = CASE WHEN %s THEN 0 ELSE failure_count + 1 END,
                last_error = %s, updated_at = NOW()
            WHERE id = %s
            """,
            (outcome.success, outcome.error_message, str(webhook["id"])),
        )

    return DeliveryOutcome(
        id=delivery_id,
        success=outcome.success,
        response_status=outcome.response_status,
        duration_ms=outcome.duration_ms,
        error_message=outcome.error_message,
    )
