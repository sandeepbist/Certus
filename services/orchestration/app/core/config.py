import os
from dotenv import load_dotenv

from services.shared.worker_runtime import bounded_int_env

load_dotenv()


class Settings:
    DATABASE_URL: str = os.getenv(
        "DATABASE_URL",
        "postgresql://nexus:nexus_dev_password@localhost:5432/nexus",
    )
    NEO4J_URI: str = os.getenv("NEO4J_URI", "bolt://localhost:7687")
    NEO4J_USER: str = os.getenv("NEO4J_USER", "neo4j")
    NEO4J_PASSWORD: str = os.getenv("NEO4J_PASSWORD", "nexus_neo4j_dev")
    REDIS_URL: str = os.getenv("REDIS_URL", "redis://localhost:6379/0")
    MCP_TOOLS_URL: str = os.getenv("MCP_TOOLS_URL", "http://localhost:8003")
    NODE_ENV: str = os.getenv("NODE_ENV", "development")
    INTERNAL_SERVICE_TOKEN: str = os.getenv("INTERNAL_SERVICE_TOKEN", "")
    WEBHOOK_ENCRYPTION_KEY: str = os.getenv("WEBHOOK_ENCRYPTION_KEY", "")
    WEBHOOK_ALLOW_PRIVATE_TARGETS: bool = os.getenv(
        "WEBHOOK_ALLOW_PRIVATE_TARGETS", "false"
    ).strip().casefold() in {"1", "true", "yes"}
    DB_POOL_MIN_SIZE: int = int(os.getenv("ORCHESTRATION_DB_POOL_MIN_SIZE", "1"))
    DB_POOL_MAX_SIZE: int = int(os.getenv("ORCHESTRATION_DB_POOL_MAX_SIZE", "12"))
    DB_POOL_ACQUIRE_TIMEOUT_SECONDS: float = float(
        os.getenv("ORCHESTRATION_DB_POOL_ACQUIRE_TIMEOUT_SECONDS", "2")
    )
    DB_CONNECT_TIMEOUT_SECONDS: int = int(
        os.getenv("ORCHESTRATION_DB_CONNECT_TIMEOUT_SECONDS", "3")
    )
    MAX_EXPORT_ARCHIVE_BYTES: int = bounded_int_env(
        "MAX_EXPORT_ARCHIVE_BYTES",
        100 * 1024 * 1024,
        1024 * 1024,
        1024 * 1024 * 1024,
    )
    MAX_EXPORT_SOURCE_BYTES: int = bounded_int_env(
        "MAX_EXPORT_SOURCE_BYTES",
        64 * 1024 * 1024,
        1024 * 1024,
        1024 * 1024 * 1024,
    )
    MAX_EXPORT_RECORDS: int = bounded_int_env(
        "MAX_EXPORT_RECORDS", 250_000, 100, 5_000_000
    )
    EXPORT_FETCH_BATCH_SIZE: int = bounded_int_env(
        "EXPORT_FETCH_BATCH_SIZE", 250, 10, 10_000
    )
    EXPORT_STATEMENT_TIMEOUT_MS: int = bounded_int_env(
        "EXPORT_STATEMENT_TIMEOUT_MS", 150_000, 1_000, 600_000
    )

    def validate(self) -> None:
        if not 1 <= self.DB_POOL_MIN_SIZE <= self.DB_POOL_MAX_SIZE <= 100:
            raise ValueError(
                "Orchestration database pool sizes must satisfy 1 <= min <= max <= 100"
            )
        if not 0.1 <= self.DB_POOL_ACQUIRE_TIMEOUT_SECONDS <= 30:
            raise ValueError(
                "ORCHESTRATION_DB_POOL_ACQUIRE_TIMEOUT_SECONDS must be between 0.1 and 30"
            )
        if not 1 <= self.DB_CONNECT_TIMEOUT_SECONDS <= 30:
            raise ValueError(
                "ORCHESTRATION_DB_CONNECT_TIMEOUT_SECONDS must be between 1 and 30"
            )

settings = Settings()
settings.validate()
