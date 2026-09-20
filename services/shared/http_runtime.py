import os
from typing import Optional


DEVELOPMENT_ENVIRONMENTS = frozenset({"development", "test"})
MINIMUM_INTERNAL_TOKEN_LENGTH = 32


def runtime_environment(environment: Optional[str] = None) -> str:
    if environment is None:
        environment = os.getenv("NODE_ENV", "development")
    return environment.strip().lower()


def hardened_runtime_enabled(environment: Optional[str] = None) -> bool:
    """Treat every environment except explicit development/test as hardened."""
    return runtime_environment(environment) not in DEVELOPMENT_ENVIRONMENTS


def fastapi_documentation_options(
    environment: Optional[str] = None,
) -> dict[str, Optional[str]]:
    if not hardened_runtime_enabled(environment):
        return {}
    return {
        "docs_url": None,
        "redoc_url": None,
        "openapi_url": None,
    }


def configured_internal_service_token(
    token: Optional[str] = None,
    environment: Optional[str] = None,
) -> str:
    if token is None:
        token = os.getenv("INTERNAL_SERVICE_TOKEN", "")
    normalized_token = token.strip()
    if normalized_token and len(normalized_token) < MINIMUM_INTERNAL_TOKEN_LENGTH:
        raise RuntimeError(
            f"INTERNAL_SERVICE_TOKEN must contain at least {MINIMUM_INTERNAL_TOKEN_LENGTH} characters"
        )
    if hardened_runtime_enabled(environment) and not normalized_token:
        raise RuntimeError("INTERNAL_SERVICE_TOKEN is required outside development and test")
    return normalized_token


def development_reload_enabled(environment: Optional[str] = None) -> bool:
    return runtime_environment(environment) == "development"
