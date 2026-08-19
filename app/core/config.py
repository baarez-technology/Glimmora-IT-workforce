"""Application settings.

Every external dependency is configurable and every one has a fallback, because
the platform must remain usable when Docker, Qdrant, Redis, MinIO or the LLM
provider are unavailable (ARCHITECTURE.md section 6).
"""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_ROOT = Path(__file__).resolve().parents[2]


class AppEnv(StrEnum):
    LOCAL = "local"
    DEV = "dev"
    TEST = "test"
    PRODUCTION = "production"


class VectorBackend(StrEnum):
    QDRANT = "qdrant"
    MEMORY = "memory"


class StorageBackend(StrEnum):
    MINIO = "minio"
    S3 = "s3"
    LOCAL = "local"


class CacheBackend(StrEnum):
    REDIS = "redis"
    MEMORY = "memory"


class AIProvider(StrEnum):
    ANTHROPIC = "anthropic"
    OPENAI = "openai"
    NULL = "null"


class EmailTransport(StrEnum):
    SMTP = "smtp"
    LOG = "log"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(BACKEND_ROOT / ".env", BACKEND_ROOT.parent / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ------------------------------------------------------------------ app
    APP_NAME: str = "Glimmora IT Workforce Intelligence Engine"
    APP_ENV: AppEnv = AppEnv.LOCAL
    API_V1_PREFIX: str = "/api/v1"
    ENABLE_DOCS: bool = True
    SECRET_KEY: str = "change-me-in-env-this-is-a-local-development-placeholder"
    FRONTEND_ORIGINS: str = "http://localhost:3000"
    DEFAULT_TIMEZONE: str = "Asia/Qatar"
    BASE_CURRENCY: str = "QAR"
    REQUEST_MAX_BYTES: int = 12 * 1024 * 1024

    # ------------------------------------------------------------- database
    DATABASE_URL: str = f"sqlite+aiosqlite:///{(BACKEND_ROOT / 'var' / 'glimmora.db').as_posix()}"
    DB_ECHO: bool = False
    DB_POOL_SIZE: int = 10
    DB_MAX_OVERFLOW: int = 20

    # ------------------------------------------------------- cache / queue
    CACHE_BACKEND: CacheBackend = CacheBackend.MEMORY
    REDIS_URL: str = "redis://localhost:6379/0"
    CELERY_BROKER_URL: str = "redis://localhost:6379/1"
    CELERY_RESULT_BACKEND: str = "redis://localhost:6379/2"
    CELERY_TASK_ALWAYS_EAGER: bool = True

    # --------------------------------------------------------- vector store
    VECTOR_BACKEND: VectorBackend = VectorBackend.MEMORY
    QDRANT_URL: str = "http://localhost:6333"
    QDRANT_API_KEY: str | None = None
    VECTOR_DIMENSIONS: int = 384

    # -------------------------------------------------------- file storage
    STORAGE_BACKEND: StorageBackend = StorageBackend.LOCAL
    MINIO_ENDPOINT: str = "http://localhost:9000"
    MINIO_ACCESS_KEY: str = ""
    MINIO_SECRET_KEY: str = ""
    MINIO_BUCKET: str = "glimmora-documents"
    MINIO_REGION: str = "us-east-1"
    LOCAL_STORAGE_PATH: Path = BACKEND_ROOT / "var" / "documents"
    MAX_UPLOAD_BYTES: int = 10 * 1024 * 1024
    SIGNED_URL_TTL_SECONDS: int = 60

    # ---------------------------------------------------------------- auth
    JWT_SECRET: str = "change-me-in-env-this-is-a-local-development-placeholder"
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_MINUTES: int = 30
    REFRESH_TOKEN_DAYS: int = 14
    PASSWORD_MIN_LENGTH: int = 12
    LOGIN_MAX_ATTEMPTS: int = 5
    LOGIN_LOCKOUT_MINUTES: int = 15

    # ------------------------------------------------------------------ ai
    LLM_PROVIDER: AIProvider = AIProvider.NULL
    LLM_MODEL: str = "claude-sonnet-5"
    LLM_API_KEY: str | None = None
    LLM_TIMEOUT_SECONDS: int = 60
    LLM_MAX_RETRIES: int = 3
    EMBEDDING_PROVIDER: AIProvider = AIProvider.NULL
    EMBEDDING_MODEL: str = "local-hash-384"
    EMBEDDING_BATCH_SIZE: int = 64
    AI_ALLOW_COMMERCIAL_CONTEXT: bool = False
    AI_DAILY_TOKEN_CEILING: int = 2_000_000
    AI_CIRCUIT_BREAKER_FAILURES: int = 5
    AI_CIRCUIT_BREAKER_COOLDOWN_SECONDS: int = 300

    # --------------------------------------------------------------- email
    EMAIL_TRANSPORT: EmailTransport = EmailTransport.LOG
    SMTP_HOST: str = ""
    SMTP_PORT: int = 587
    SMTP_USER: str = ""
    SMTP_PASSWORD: str = ""
    SMTP_FROM: str = "noreply@glimmora.ai"

    # ------------------------------------------------------ business rules
    # Defaults only. Live values come from scoring_configurations (SCORING.md s7).
    WORKING_DAYS_PER_MONTH: int = 22
    HOURS_PER_DAY: int = 8
    SLA_DEFAULT_HOURS_P5: int = 48
    SLA_URGENT_HOURS: int = 8
    SLA_DUE_SOON_HOURS: int = 24
    DOCUMENT_EXPIRING_SOON_DAYS: int = 60
    DOCUMENT_REMINDER_DAYS: str = "90,60,30,7"
    BENCH_MILESTONE_DAYS: str = "90,60,30,15,7"

    # ---------------------------------------------------------- rate limit
    RATE_LIMIT_ENABLED: bool = True
    RATE_LIMIT_LOGIN_PER_MINUTE: int = 10
    RATE_LIMIT_PARSING_PER_MINUTE: int = 30
    RATE_LIMIT_DEFAULT_PER_MINUTE: int = 300

    # ------------------------------------------------------------ logging
    LOG_LEVEL: str = "INFO"
    LOG_JSON: bool = False

    # ---------------------------------------------------------- validators
    @field_validator("BASE_CURRENCY")
    @classmethod
    def _currency_is_iso(cls, v: str) -> str:
        if len(v) != 3 or not v.isalpha():
            raise ValueError("BASE_CURRENCY must be a 3-letter ISO 4217 code")
        return v.upper()

    # -------------------------------------------------------- derived view
    @property
    def cors_origins(self) -> list[str]:
        return [o.strip() for o in self.FRONTEND_ORIGINS.split(",") if o.strip()]

    @property
    def document_reminder_days(self) -> list[int]:
        days = (int(d) for d in self.DOCUMENT_REMINDER_DAYS.split(",") if d.strip())
        return sorted(days, reverse=True)

    @property
    def bench_milestone_days(self) -> list[int]:
        days = (int(d) for d in self.BENCH_MILESTONE_DAYS.split(",") if d.strip())
        return sorted(days, reverse=True)

    @property
    def is_sqlite(self) -> bool:
        return self.DATABASE_URL.startswith("sqlite")

    @property
    def is_production(self) -> bool:
        return self.APP_ENV is AppEnv.PRODUCTION

    @property
    def docs_url(self) -> str | None:
        return f"{self.API_V1_PREFIX}/docs" if self.ENABLE_DOCS and not self.is_production else None

    @property
    def openapi_url(self) -> str | None:
        return f"{self.API_V1_PREFIX}/openapi.json" if self.docs_url else None

    def degraded_components(self) -> dict[str, str]:
        """Dependencies currently running on a fallback rather than the primary."""
        degraded: dict[str, str] = {}
        if self.is_sqlite:
            degraded["database"] = "sqlite (postgres not configured)"
        if self.VECTOR_BACKEND is VectorBackend.MEMORY:
            degraded["vector_store"] = "in-process cosine (qdrant not configured)"
        if self.STORAGE_BACKEND is StorageBackend.LOCAL:
            degraded["object_storage"] = "local filesystem (minio/s3 not configured)"
        if self.CACHE_BACKEND is CacheBackend.MEMORY:
            degraded["cache"] = "in-process TTL dict (redis not configured)"
        if self.CELERY_TASK_ALWAYS_EAGER:
            degraded["queue"] = "eager in-request execution (celery not configured)"
        if self.LLM_PROVIDER is AIProvider.NULL:
            degraded["llm"] = "deterministic rule-based parser (no LLM provider)"
        if self.EMBEDDING_PROVIDER is AIProvider.NULL:
            degraded["embeddings"] = "deterministic local hash embedder"
        if self.EMAIL_TRANSPORT is EmailTransport.LOG:
            degraded["email"] = "log-only transport (smtp not configured)"
        return degraded

    def insecure_defaults_in_use(self) -> list[str]:
        """Placeholder secrets that must never reach production."""
        problems: list[str] = []
        placeholder = "change-me-in-env"
        if placeholder in self.SECRET_KEY:
            problems.append("SECRET_KEY")
        if placeholder in self.JWT_SECRET:
            problems.append("JWT_SECRET")
        return problems


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    if settings.is_production and settings.insecure_defaults_in_use():
        raise RuntimeError(
            "Refusing to start in production with placeholder secrets: "
            + ", ".join(settings.insecure_defaults_in_use())
        )
    settings.LOCAL_STORAGE_PATH.mkdir(parents=True, exist_ok=True)
    if settings.is_sqlite:
        (BACKEND_ROOT / "var").mkdir(parents=True, exist_ok=True)
    return settings


settings = get_settings()


__all__ = [
    "AIProvider",
    "AppEnv",
    "CacheBackend",
    "EmailTransport",
    "Settings",
    "StorageBackend",
    "VectorBackend",
    "get_settings",
    "settings",
]
