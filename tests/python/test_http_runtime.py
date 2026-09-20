import unittest
from pathlib import Path

from fastapi import FastAPI

from services.shared.http_runtime import (
    configured_internal_service_token,
    development_reload_enabled,
    fastapi_documentation_options,
    hardened_runtime_enabled,
    runtime_environment,
)


class HttpRuntimeConfigurationTests(unittest.TestCase):
    def test_environment_names_are_normalized_and_unknown_names_fail_safe(self):
        self.assertEqual(runtime_environment(" Production "), "production")
        self.assertFalse(hardened_runtime_enabled("development"))
        self.assertFalse(hardened_runtime_enabled("test"))
        self.assertTrue(hardened_runtime_enabled("production"))
        self.assertTrue(hardened_runtime_enabled("staging"))
        self.assertTrue(hardened_runtime_enabled(""))

    def test_openapi_and_both_documentation_uis_are_absent_when_hardened(self):
        application = FastAPI(**fastapi_documentation_options("production"))
        paths = {route.path for route in application.routes}

        self.assertNotIn("/openapi.json", paths)
        self.assertNotIn("/docs", paths)
        self.assertNotIn("/redoc", paths)

    def test_development_retains_fastapi_documentation(self):
        application = FastAPI(**fastapi_documentation_options("development"))
        paths = {route.path for route in application.routes}

        self.assertIn("/openapi.json", paths)
        self.assertIn("/docs", paths)
        self.assertIn("/redoc", paths)

    def test_internal_token_fails_closed_in_hardened_runtimes(self):
        for environment in ("production", "staging", "unexpected"):
            with self.subTest(environment=environment):
                with self.assertRaisesRegex(RuntimeError, "required"):
                    configured_internal_service_token("", environment)

    def test_internal_token_rejects_weak_nonempty_values_in_every_environment(self):
        for environment in ("development", "test", "production"):
            with self.subTest(environment=environment):
                with self.assertRaisesRegex(RuntimeError, "at least 32"):
                    configured_internal_service_token("too-short", environment)

        strong_token = " token-with-at-least-thirty-two-characters "
        self.assertEqual(
            configured_internal_service_token(strong_token, "production"),
            strong_token.strip(),
        )
        self.assertEqual(configured_internal_service_token("", "development"), "")

    def test_code_reload_is_development_only(self):
        self.assertTrue(development_reload_enabled("development"))
        self.assertFalse(development_reload_enabled("test"))
        self.assertFalse(development_reload_enabled("production"))
        self.assertFalse(development_reload_enabled("staging"))

    def test_python_api_images_default_to_hardened_header_minimizing_runtime(self):
        for service in ("ingestion", "orchestration", "mcp-tools"):
            dockerfile = Path(f"services/{service}/Dockerfile").read_text(encoding="utf-8")
            with self.subTest(service=service):
                self.assertIn("NODE_ENV=production", dockerfile)
                self.assertIn('"--no-server-header"', dockerfile)
                self.assertNotIn('"--reload"', dockerfile)


if __name__ == "__main__":
    unittest.main()
