import json
import socket
import unittest
from unittest.mock import MagicMock, patch

import httpx

from services.orchestration.app.retrieval.webhook_delivery import (
    UnsafeWebhookTarget,
    canonical_payload,
    normalize_events,
    signature,
    validate_webhook_url,
    webhook_request_target,
)
from services.shared.webhooks import (
    WebhookConfigurationError,
    decrypt_signing_secret as decrypt_shared_secret,
    encrypt_signing_secret as encrypt_shared_secret,
    response_status_is_retryable,
    send_signed_webhook,
)


def public_resolver(*_args, **_kwargs):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]


def private_resolver(*_args, **_kwargs):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))]


class WebhookDeliveryTests(unittest.TestCase):
    def test_public_https_target_is_normalized(self):
        self.assertEqual(
            validate_webhook_url("https://EXAMPLE.com/hooks?a=1", resolver=public_resolver),
            "https://example.com/hooks?a=1",
        )
        self.assertEqual(
            webhook_request_target("https://example.com/hooks", resolver=public_resolver),
            ("https://93.184.216.34/hooks", "example.com", "example.com"),
        )

    def test_private_credentials_redirect_ports_and_http_are_rejected(self):
        cases = [
            ("https://localhost/hook", private_resolver),
            ("https://user:pass@example.com/hook", public_resolver),
            ("https://example.com:8443/hook", public_resolver),
            ("http://example.com/hook", public_resolver),
        ]
        for url, resolver in cases:
            with self.subTest(url=url), self.assertRaises(UnsafeWebhookTarget):
                validate_webhook_url(url, resolver=resolver)

    def test_local_http_is_only_available_with_explicit_test_override(self):
        self.assertEqual(
            validate_webhook_url("http://127.0.0.1:9089/hook", allow_private=True),
            "http://127.0.0.1:9089/hook",
        )

    def test_signature_is_stable_and_event_validation_deduplicates(self):
        body = canonical_payload(
            "task_created",
            {"task_id": "one"},
            "delivery-one",
            created_at=1_700_000_000,
        )
        self.assertEqual(json.loads(body)["created_at"], 1_700_000_000)
        self.assertEqual(signature("secret", "123", body), signature("secret", "123", body))
        self.assertTrue(signature("secret", "123", body).startswith("v1="))
        self.assertEqual(normalize_events(["task_created", "task_created"]), ["task_created"])
        with self.assertRaises(ValueError):
            normalize_events(["unknown_event"])

    def test_only_transient_endpoint_responses_are_retryable(self):
        for status in (408, 409, 425, 429, 500, 503):
            with self.subTest(status=status):
                self.assertTrue(response_status_is_retryable(status))
        for status in (400, 401, 403, 404, 410, 422):
            with self.subTest(status=status):
                self.assertFalse(response_status_is_retryable(status))

    def test_secret_encryption_round_trip_and_production_key_requirement(self):
        encrypted = encrypt_shared_secret(
            "whsec_proof",
            "a-development-master-key",
            "",
            "development",
        )
        self.assertTrue(encrypted.startswith("v1."))
        self.assertNotIn("whsec_proof", encrypted)
        self.assertEqual(
            decrypt_shared_secret(
                encrypted,
                "a-development-master-key",
                "",
                "development",
            ),
            "whsec_proof",
        )
        with self.assertRaises(WebhookConfigurationError):
            encrypt_shared_secret("whsec_proof", "too-short", "", "production")

    def test_transport_failure_drops_exception_instance_data(self):
        secret = "whsec_transport-private"
        request = httpx.Request("POST", "https://93.184.216.34/hook")
        client = MagicMock()
        client_context = MagicMock()
        client_context.__enter__.return_value = client
        client.stream.side_effect = httpx.ConnectError(
            f"Authorization: Bearer {secret} at provider.internal",
            request=request,
        )

        with (
            patch(
                "services.shared.webhooks.webhook_request_target",
                return_value=(
                    "https://93.184.216.34/hook",
                    "example.com",
                    "example.com",
                ),
            ),
            patch("services.shared.webhooks.httpx.Client", return_value=client_context),
        ):
            outcome = send_signed_webhook(
                url="https://example.com/hook",
                secret="signing-secret",
                event_type="task_created",
                data={"task_id": "one"},
                delivery_id="delivery-one",
                created_at=1_700_000_000,
            )

        self.assertFalse(outcome.success)
        self.assertTrue(outcome.retryable)
        self.assertEqual(
            outcome.error_message,
            "webhook request failed (ConnectError)",
        )
        self.assertNotIn(secret, str(outcome))
        self.assertNotIn("provider.internal", str(outcome))


if __name__ == "__main__":
    unittest.main()
