"""Shared outbound-webhook security and transport primitives.

Database state belongs to the calling service. Keeping URL validation, signing,
encryption, and the HTTP request in one shared module prevents the synchronous
test endpoint and the Temporal worker from drifting apart.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import json
import socket
import time
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import urlsplit, urlunsplit

import httpx
from cryptography.fernet import Fernet, InvalidToken


SUPPORTED_WEBHOOK_EVENTS = (
    "document_ready",
    "agent_run_completed",
    "automation_triggered",
    "automation_failed",
    "memory_extracted",
    "task_created",
    "task_completed",
    "export_ready",
)
MAX_RESPONSE_BODY = 2_000
RETRYABLE_RESPONSE_STATUSES = frozenset({408, 409, 425, 429})


class WebhookConfigurationError(RuntimeError):
    pass


class UnsafeWebhookTarget(ValueError):
    pass


@dataclass(frozen=True)
class WebhookTransportOutcome:
    success: bool
    response_status: int | None
    response_body: str | None
    duration_ms: int
    error_message: str | None
    retryable: bool


def webhook_master_key(
    encryption_key: str,
    internal_service_token: str,
    node_env: str,
) -> str:
    master_key = encryption_key.strip()
    if not master_key and node_env != "production":
        master_key = internal_service_token.strip()
    if not master_key:
        raise WebhookConfigurationError(
            "Webhook secret encryption is not configured. Set WEBHOOK_ENCRYPTION_KEY."
        )
    if node_env == "production" and len(master_key) < 32:
        raise WebhookConfigurationError(
            "WEBHOOK_ENCRYPTION_KEY must contain at least 32 characters in production."
        )
    return master_key


def signing_secret_cipher(
    encryption_key: str,
    internal_service_token: str,
    node_env: str,
) -> Fernet:
    master_key = webhook_master_key(encryption_key, internal_service_token, node_env)
    derived = hashlib.sha256(f"certus:webhooks:v1:{master_key}".encode()).digest()
    return Fernet(base64.urlsafe_b64encode(derived))


def encrypt_signing_secret(
    secret: str,
    encryption_key: str,
    internal_service_token: str,
    node_env: str,
) -> str:
    cipher = signing_secret_cipher(encryption_key, internal_service_token, node_env)
    return f"v1.{cipher.encrypt(secret.encode()).decode()}"


def decrypt_signing_secret(
    encrypted_secret: str,
    encryption_key: str,
    internal_service_token: str,
    node_env: str,
) -> str:
    if not encrypted_secret.startswith("v1."):
        raise WebhookConfigurationError("The webhook uses an unsupported legacy secret format.")
    cipher = signing_secret_cipher(encryption_key, internal_service_token, node_env)
    try:
        return cipher.decrypt(encrypted_secret[3:].encode()).decode()
    except InvalidToken as error:
        raise WebhookConfigurationError(
            "The webhook signing secret could not be decrypted."
        ) from error


def normalize_events(events: list[str]) -> list[str]:
    normalized = list(dict.fromkeys(event.strip().casefold() for event in events if event.strip()))
    unsupported = sorted(set(normalized).difference(SUPPORTED_WEBHOOK_EVENTS))
    if unsupported:
        raise ValueError(f"Unsupported webhook events: {', '.join(unsupported)}")
    if not normalized:
        raise ValueError("Select at least one webhook event")
    return normalized


def _target_addresses(
    hostname: str,
    port: int,
    resolver: Callable[..., list[tuple[Any, ...]]],
) -> set[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    try:
        literal = ipaddress.ip_address(hostname)
        return {literal}
    except ValueError:
        pass
    try:
        resolved = resolver(hostname, port, type=socket.SOCK_STREAM)
    except socket.gaierror as error:
        raise UnsafeWebhookTarget("The webhook hostname could not be resolved") from error
    addresses = {ipaddress.ip_address(item[4][0]) for item in resolved}
    if not addresses:
        raise UnsafeWebhookTarget("The webhook hostname did not resolve to an address")
    return addresses


def validate_webhook_url(
    value: str,
    *,
    allow_private: bool = False,
    resolver: Callable[..., list[tuple[Any, ...]]] = socket.getaddrinfo,
) -> str:
    candidate = value.strip()
    if len(candidate) > 500:
        raise UnsafeWebhookTarget("Webhook URLs must be 500 characters or fewer")
    parsed = urlsplit(candidate)
    if parsed.scheme not in ({"https", "http"} if allow_private else {"https"}):
        raise UnsafeWebhookTarget("Webhook URLs must use HTTPS")
    if parsed.username or parsed.password:
        raise UnsafeWebhookTarget("Webhook URLs cannot contain credentials")
    if parsed.fragment:
        raise UnsafeWebhookTarget("Webhook URLs cannot contain fragments")
    hostname = (parsed.hostname or "").rstrip(".").casefold()
    if not hostname:
        raise UnsafeWebhookTarget("Webhook URLs require a hostname")
    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError as error:
        raise UnsafeWebhookTarget("Webhook URL port is invalid") from error
    if not allow_private and port != 443:
        raise UnsafeWebhookTarget("Public webhook targets must use HTTPS port 443")

    addresses = _target_addresses(hostname, port, resolver)
    if not allow_private and any(not address.is_global for address in addresses):
        raise UnsafeWebhookTarget("Webhook targets must resolve only to public internet addresses")

    try:
        normalized_host = hostname.encode("idna").decode("ascii")
    except UnicodeError as error:
        raise UnsafeWebhookTarget("Webhook hostname is invalid") from error
    netloc = normalized_host
    if (parsed.scheme, port) not in {("https", 443), ("http", 80)}:
        netloc = f"{normalized_host}:{port}"
    return urlunsplit((parsed.scheme, netloc, parsed.path or "/", parsed.query, ""))


def webhook_request_target(
    value: str,
    *,
    allow_private: bool = False,
    resolver: Callable[..., list[tuple[Any, ...]]] = socket.getaddrinfo,
) -> tuple[str, str, str]:
    """Return a DNS-pinned URL, Host header, and TLS SNI hostname."""
    normalized = validate_webhook_url(value, allow_private=allow_private, resolver=resolver)
    parsed = urlsplit(normalized)
    hostname = parsed.hostname or ""
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    addresses = _target_addresses(hostname, port, resolver)
    if not allow_private and any(not address.is_global for address in addresses):
        raise UnsafeWebhookTarget("Webhook targets must resolve only to public internet addresses")
    address = sorted(addresses, key=lambda item: (item.version, str(item)))[0]
    pinned_host = f"[{address}]" if address.version == 6 else str(address)
    pinned_netloc = pinned_host
    if (parsed.scheme, port) not in {("https", 443), ("http", 80)}:
        pinned_netloc = f"{pinned_host}:{port}"
    pinned_url = urlunsplit((parsed.scheme, pinned_netloc, parsed.path, parsed.query, ""))
    return pinned_url, parsed.netloc, hostname


def canonical_payload(
    event_type: str,
    data: dict[str, Any],
    delivery_id: str,
    *,
    created_at: int | None = None,
) -> bytes:
    return json.dumps(
        {
            "id": delivery_id,
            "type": event_type,
            "created_at": int(time.time()) if created_at is None else created_at,
            "data": data,
        },
        separators=(",", ":"),
        sort_keys=True,
        default=str,
    ).encode()


def signature(secret: str, timestamp: str, body: bytes) -> str:
    digest = hmac.new(secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()
    return f"v1={digest}"


def response_status_is_retryable(status: int) -> bool:
    return status in RETRYABLE_RESPONSE_STATUSES or status >= 500


def send_signed_webhook(
    *,
    url: str,
    secret: str,
    event_type: str,
    data: dict[str, Any],
    delivery_id: str,
    created_at: int | None = None,
    allow_private: bool = False,
) -> WebhookTransportOutcome:
    target_url, host_header, sni_hostname = webhook_request_target(
        url,
        allow_private=allow_private,
    )
    body = canonical_payload(event_type, data, delivery_id, created_at=created_at)
    timestamp = str(int(time.time()))
    started = time.monotonic()
    response_status: int | None = None
    response_body: str | None = None
    error_message: str | None = None
    success = False
    retryable = False
    try:
        with httpx.Client(
            timeout=httpx.Timeout(5.0, connect=3.0),
            follow_redirects=False,
            trust_env=False,
        ) as client:
            with client.stream(
                "POST",
                target_url,
                content=body,
                headers={
                    "Content-Type": "application/json",
                    "Host": host_header,
                    "User-Agent": "Certus-Webhook/1.0",
                    "X-Certus-Event": event_type,
                    "X-Certus-Delivery": delivery_id,
                    "X-Certus-Timestamp": timestamp,
                    "X-Certus-Signature": signature(secret, timestamp, body),
                },
                extensions={"sni_hostname": sni_hostname},
            ) as response:
                response_status = response.status_code
                response_bytes = bytearray()
                for chunk in response.iter_bytes():
                    response_bytes.extend(chunk[: max(0, MAX_RESPONSE_BODY - len(response_bytes))])
                    if len(response_bytes) >= MAX_RESPONSE_BODY:
                        break
                response_body = bytes(response_bytes).decode(
                    response.encoding or "utf-8",
                    errors="replace",
                )
        success = response_status is not None and 200 <= response_status < 300
        if not success and response_status is not None:
            error_message = f"Endpoint returned HTTP {response_status}"
            retryable = response_status_is_retryable(response_status)
    except httpx.HTTPError as error:
        error_message = str(error)[:500]
        retryable = True

    return WebhookTransportOutcome(
        success=success,
        response_status=response_status,
        response_body=response_body,
        duration_ms=round((time.monotonic() - started) * 1000),
        error_message=error_message,
        retryable=retryable,
    )
