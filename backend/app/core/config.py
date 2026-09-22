"""Application settings.

Single source of truth for configuration, loaded from environment variables /
``.env``. Field names map 1:1 onto the keys documented in ``.env.example``;
``tests/unit/core/test_settings_contract.py`` fails the build if a settings
field is undocumented or a documented key is unknown, so the two can never
drift apart.

Spec references:
    §9-10   provider abstraction configuration
    §11     RAG mode / dimension handling
    §19-20  persistence + object storage
    §23     token lifetimes and cookie policy
    §76     agent budgets
    §83     checkpointer backend
    §86-88  MCP server configuration
    §94     PII redaction toggles
    §128    observability must be optional
"""

from __future__ import annotations

import functools
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

AppEnv = Literal["dev", "test", "staging", "prod"]
RagMode = Literal["light", "full"]
CheckpointerBackend = Literal["memory", "redis"]

# ---------------------------------------------------------------------------
# Environment file discovery
# ---------------------------------------------------------------------------
# Resolved from this file's location rather than from the process working
# directory. A relative ".env" silently resolves against CWD, which means the
# same command behaves differently depending on where it was launched - and a
# missing .env produces *plausible* defaults (an empty password) rather than an
# error. Anchoring to the source tree removes that failure mode entirely.
_BACKEND_DIR = Path(__file__).resolve().parents[2]  # <repo>/backend
_REPO_ROOT = _BACKEND_DIR.parent  # <repo>
#: Later entries win, so a backend-local override beats the shared root file.
ENV_FILES: tuple[Path, ...] = (_REPO_ROOT / ".env", _BACKEND_DIR / ".env")

_PLACEHOLDER_SECRETS = frozenset(
    {
        "",
        "REPLACE_ME_WITH_64_BYTES_OF_ENTROPY",
        "changeme",
        "secret",
    }
)


def _split_csv(value: object) -> object:
    """Allow comma-separated strings for list-typed settings."""
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return []
        if stripped.startswith("["):
            return value
        return [item.strip() for item in stripped.split(",") if item.strip()]
    return value


class Settings(BaseSettings):
    """Resolved application configuration."""

    model_config = SettingsConfigDict(
        env_file=ENV_FILES,
        env_file_encoding="utf-8-sig",  # tolerate a BOM written by Windows tooling
        case_sensitive=False,
        extra="ignore",
    )

    # ------------------------------------------------------------------
    # Application
    # ------------------------------------------------------------------
    APP_NAME: str = "Nova Commerce"
    APP_ENV: AppEnv = "dev"
    APP_DEBUG: bool = True
    APP_TIMEZONE: str = "Asia/Shanghai"
    API_V1_PREFIX: str = "/api/v1"
    CORS_ALLOW_ORIGINS: list[str] = Field(default_factory=lambda: ["http://localhost:5173"])

    # ------------------------------------------------------------------
    # Security / tokens (§23)
    # ------------------------------------------------------------------
    JWT_ALGORITHM: str = "HS256"
    JWT_SECRET_KEY: SecretStr = SecretStr("")
    JWT_ISSUER: str = "nova-commerce"
    JWT_AUDIENCE: str = "nova-commerce-api"
    ACCESS_TOKEN_TTL_SECONDS: int = Field(default=900, ge=60)
    REFRESH_TOKEN_TTL_SECONDS: int = Field(default=1_209_600, ge=300)
    REFRESH_COOKIE_NAME: str = "nova_rt"
    REFRESH_COOKIE_SECURE: bool = False
    REFRESH_COOKIE_SAMESITE: Literal["lax", "strict", "none"] = "lax"
    REFRESH_COOKIE_PATH: str = "/api/v1/auth"
    # Name of the hashing algorithm, not a credential (ruff S105 false positive).
    PASSWORD_HASH_SCHEME: str = "argon2"  # noqa: S105

    # ------------------------------------------------------------------
    # MySQL (§19)
    # ------------------------------------------------------------------
    MYSQL_HOST: str = "127.0.0.1"
    MYSQL_PORT: int = 13306
    MYSQL_DATABASE: str = "nova"
    MYSQL_USER: str = "nova"
    MYSQL_PASSWORD: SecretStr = SecretStr("")
    MYSQL_ROOT_PASSWORD: SecretStr = SecretStr("")
    DATABASE_URL: str = ""
    DB_POOL_SIZE: int = Field(default=10, ge=1)
    DB_MAX_OVERFLOW: int = Field(default=20, ge=0)
    DB_POOL_RECYCLE_SECONDS: int = Field(default=1800, ge=-1)
    DB_ECHO: bool = False
    # Explicit escape hatch so the required "SELECT ... FOR UPDATE" semantics of
    # §27 are never silently downgraded on a non-MySQL backend.
    DB_REQUIRE_MYSQL: bool = True
    DB_STATEMENT_TIMEOUT_MS: int = Field(default=15_000, ge=0)

    # ------------------------------------------------------------------
    # Redis
    # ------------------------------------------------------------------
    REDIS_HOST: str = "127.0.0.1"
    REDIS_PORT: int = 16379
    REDIS_DB_CACHE: int = 0
    REDIS_DB_BROKER: int = 1
    REDIS_DB_RESULT: int = 2
    REDIS_DB_LOCK: int = 3
    REDIS_DB_CHECKPOINT: int = 4
    REDIS_PASSWORD: SecretStr = SecretStr("")
    REDIS_URL: str = ""
    REDIS_SOCKET_TIMEOUT_SECONDS: float = 5.0

    # ------------------------------------------------------------------
    # Celery
    # ------------------------------------------------------------------
    CELERY_BROKER_URL: str = ""
    CELERY_RESULT_BACKEND: str = ""
    CELERY_TASK_ALWAYS_EAGER: bool = False
    CELERY_TASK_TIME_LIMIT: int = 600

    # ------------------------------------------------------------------
    # Object storage (§20)
    # ------------------------------------------------------------------
    S3_ENDPOINT: str = "http://127.0.0.1:19000"
    # Presigned URLs must be reachable by the *client*, which may not resolve
    # the docker-internal hostname. Kept separate on purpose (ADR-011).
    S3_PUBLIC_ENDPOINT: str = "http://127.0.0.1:19000"
    S3_REGION: str = "us-east-1"
    S3_ACCESS_KEY: SecretStr = SecretStr("")
    S3_SECRET_KEY: SecretStr = SecretStr("")
    S3_USE_SSL: bool = False
    S3_FORCE_PATH_STYLE: bool = True
    S3_BUCKET_PRODUCT_IMAGES: str = "nova-product-images"
    S3_BUCKET_KNOWLEDGE: str = "nova-knowledge-private"
    S3_BUCKET_REPORTS: str = "nova-reports"
    S3_BUCKET_EVIDENCE: str = "nova-evidence"
    S3_SIGNED_URL_TTL_SECONDS: int = Field(default=900, ge=1)
    S3_MAX_UPLOAD_BYTES: int = Field(default=26_214_400, ge=1)
    S3_CONNECT_TIMEOUT_SECONDS: float = 5.0
    S3_READ_TIMEOUT_SECONDS: float = 30.0

    # ------------------------------------------------------------------
    # Vector store / RAG (§11, §56, §57)
    # ------------------------------------------------------------------
    QDRANT_URL: str = "http://127.0.0.1:16333"
    QDRANT_API_KEY: SecretStr = SecretStr("")
    QDRANT_COLLECTION_PREFIX: str = "nova"
    QDRANT_TIMEOUT_SECONDS: float = 10.0
    RAG_MODE: RagMode = "light"
    RAG_TOP_K_DENSE: int = Field(default=30, ge=1)
    RAG_TOP_K_SPARSE: int = Field(default=30, ge=1)
    RAG_RRF_K: int = Field(default=60, ge=1)
    RAG_RRF_WEIGHT_DENSE: float = 1.0
    RAG_RRF_WEIGHT_SPARSE: float = 1.0
    RAG_CANDIDATE_LIMIT: int = Field(default=40, ge=1)
    RAG_RERANK_TOP_N: int = Field(default=8, ge=1)
    RAG_MIN_EVIDENCE_SCORE: float = Field(default=0.30, ge=0.0, le=1.0)
    RAG_FUSION_STRATEGY: Literal["rrf"] = "rrf"
    RAG_ENABLE_QUERY_REWRITE: bool = True
    RAG_ENABLE_ENTITY_EXTRACTION: bool = True
    RAG_CHUNK_MAX_TOKENS: int = Field(default=512, ge=32)
    RAG_CHUNK_OVERLAP_TOKENS: int = Field(default=64, ge=0)
    RAG_PARSE_MAX_PAGES: int = Field(default=300, ge=1)
    RAG_PARSE_MAX_BYTES: int = Field(default=26_214_400, ge=1)
    RAG_PARSE_TIMEOUT_SECONDS: int = Field(default=120, ge=1)

    # ------------------------------------------------------------------
    # Model providers (§9, §10)
    # ------------------------------------------------------------------
    LLM_PROVIDER: str = "fake"
    LLM_MODEL: str = "gpt-4o-mini"
    LLM_BASE_URL: str = ""
    LLM_API_KEY: SecretStr = SecretStr("")
    LLM_TEMPERATURE: float = Field(default=0.2, ge=0.0, le=2.0)
    LLM_MAX_TOKENS: int = Field(default=2048, ge=1)
    LLM_TIMEOUT_SECONDS: float = 60.0
    LLM_MAX_RETRIES: int = Field(default=2, ge=0)

    EMBEDDING_PROVIDER: str = "fake"
    EMBEDDING_MODEL: str = "text-embedding-3-small"
    EMBEDDING_BASE_URL: str = ""
    EMBEDDING_API_KEY: SecretStr = SecretStr("")
    # 0 means "resolve from provider metadata at startup". The value is never
    # hardcoded in business code (§11).
    EMBEDDING_DIMENSION: int = Field(default=0, ge=0)
    EMBEDDING_BATCH_SIZE: int = Field(default=64, ge=1)

    RERANKER_PROVIDER: str = "none"
    RERANKER_MODEL: str = ""
    RERANKER_BASE_URL: str = ""
    RERANKER_API_KEY: SecretStr = SecretStr("")

    AI_USE_FAKE_PROVIDERS: bool = True

    # ------------------------------------------------------------------
    # Agent budgets (§76)
    # ------------------------------------------------------------------
    AGENT_MAX_PLAN_STEPS: int = Field(default=6, ge=1)
    AGENT_MAX_TOOL_CALLS: int = Field(default=12, ge=1)
    AGENT_MAX_REPLAN: int = Field(default=2, ge=0)
    AGENT_MAX_PARAM_REPAIR: int = Field(default=2, ge=0)
    AGENT_MAX_CONSECUTIVE_TOOL_FAILURES: int = Field(default=3, ge=1)
    AGENT_RUN_TIMEOUT_SECONDS: int = Field(default=180, ge=1)
    PENDING_ACTION_TTL_SECONDS: int = Field(default=900, ge=30)

    CHECKPOINTER_BACKEND: CheckpointerBackend = "memory"

    # ------------------------------------------------------------------
    # PII redaction + logging (§94, §131, §132)
    # ------------------------------------------------------------------
    PII_MASK_PHONE: bool = True
    PII_MASK_EMAIL: bool = True
    PII_MASK_ADDRESS: bool = True
    PII_MASK_ID_CARD: bool = True
    LOG_LEVEL: str = "INFO"
    LOG_JSON: bool = True

    # ------------------------------------------------------------------
    # Observability (§128: never a hard startup dependency)
    # ------------------------------------------------------------------
    OTEL_ENABLED: bool = False
    OTEL_EXPORTER_OTLP_ENDPOINT: str = "http://127.0.0.1:14317"
    OTEL_SERVICE_NAME: str = "nova-commerce-api"
    OTEL_TRACES_SAMPLER_ARG: float = 1.0

    # ------------------------------------------------------------------
    # Rate limiting (§109)
    # ------------------------------------------------------------------
    RATE_LIMIT_ENABLED: bool = True
    RATE_LIMIT_LOGIN_PER_MINUTE: int = Field(default=10, ge=1)
    RATE_LIMIT_DEFAULT_PER_MINUTE: int = Field(default=300, ge=1)
    RATE_LIMIT_MCP_PER_MINUTE: int = Field(default=120, ge=1)
    LOGIN_MAX_FAILURES: int = Field(default=5, ge=1)
    LOGIN_LOCKOUT_SECONDS: int = Field(default=900, ge=1)

    # ------------------------------------------------------------------
    # MCP server (§86-§93) - baseline verified in ADR-013
    # ------------------------------------------------------------------
    MCP_SERVER_NAME: str = "nova-commerce-mcp"
    MCP_HTTP_PATH: str = "/mcp"
    MCP_ALLOWED_ORIGINS: list[str] = Field(default_factory=lambda: ["http://localhost:5173"])
    MCP_ALLOWED_HOSTS: list[str] = Field(
        default_factory=lambda: ["localhost", "127.0.0.1", "nova.local"]
    )
    MCP_ENABLE_DNS_REBINDING_PROTECTION: bool = True
    MCP_REQUIRED_SCOPES: list[str] = Field(default_factory=lambda: ["nova.read"])
    MCP_REQUEST_TIMEOUT_SECONDS: int = Field(default=30, ge=1)
    MCP_MAX_REQUEST_BODY_BYTES: int = 1_048_576
    MCP_SESSION_IDLE_TIMEOUT_SECONDS: int = 1800
    MCP_MAX_SESSIONS: int = 200

    OIDC_ISSUER_URL: str = ""
    OIDC_JWKS_URL: str = ""
    OIDC_AUDIENCE: str = ""
    OIDC_RESOURCE_URL: str = ""
    OIDC_JWKS_CACHE_SECONDS: int = 300

    KEYCLOAK_ADMIN: str = "admin"
    KEYCLOAK_ADMIN_PASSWORD: SecretStr = SecretStr("")
    KEYCLOAK_REALM: str = "nova"
    KEYCLOAK_CLIENT_ID: str = "nova-web"
    KEYCLOAK_MCP_CLIENT_ID: str = "nova-mcp-client"

    # ------------------------------------------------------------------
    # Seed (§126)
    # ------------------------------------------------------------------
    SEED_MERCHANT_NAME: str = "Nova Digital"
    SEED_HISTORY_DAYS: int = Field(default=75, ge=1)
    SEED_PRODUCT_COUNT: int = Field(default=26, ge=1)
    SEED_SKU_COUNT: int = Field(default=68, ge=1)
    SEED_DETERMINISTIC: bool = True
    SEED_RANDOM_SEED: int = 20260922

    # ------------------------------------------------------------------
    # Validators
    # ------------------------------------------------------------------
    _csv_fields = (
        "CORS_ALLOW_ORIGINS",
        "MCP_ALLOWED_ORIGINS",
        "MCP_ALLOWED_HOSTS",
        "MCP_REQUIRED_SCOPES",
    )

    @field_validator(*_csv_fields, mode="before")
    @classmethod
    def _parse_csv(cls, value: object) -> object:
        return _split_csv(value)

    @field_validator("LOG_LEVEL")
    @classmethod
    def _upper_log_level(cls, value: str) -> str:
        allowed = {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG", "NOTSET"}
        normalised = value.upper()
        if normalised not in allowed:
            msg = f"LOG_LEVEL must be one of {sorted(allowed)}, got {value!r}"
            raise ValueError(msg)
        return normalised

    @model_validator(mode="after")
    def _derive_urls_and_guard(self) -> Settings:
        if not self.DATABASE_URL:
            self.DATABASE_URL = (
                f"mysql+pymysql://{self.MYSQL_USER}:{self.MYSQL_PASSWORD.get_secret_value()}"
                f"@{self.MYSQL_HOST}:{self.MYSQL_PORT}/{self.MYSQL_DATABASE}?charset=utf8mb4"
            )
        if not self.REDIS_URL:
            self.REDIS_URL = f"redis://{self.REDIS_HOST}:{self.REDIS_PORT}/{self.REDIS_DB_CACHE}"
        if not self.CELERY_BROKER_URL:
            self.CELERY_BROKER_URL = (
                f"redis://{self.REDIS_HOST}:{self.REDIS_PORT}/{self.REDIS_DB_BROKER}"
            )
        if not self.CELERY_RESULT_BACKEND:
            self.CELERY_RESULT_BACKEND = (
                f"redis://{self.REDIS_HOST}:{self.REDIS_PORT}/{self.REDIS_DB_RESULT}"
            )

        if self.is_hardened:
            self._assert_production_hardening()

        if self.RAG_MODE == "full" and self.RERANKER_PROVIDER == "none":
            # Allowed: full mode may legitimately run without a reranker if the
            # eval shows no gain, but it must be an explicit choice, not a typo.
            self.__dict__.setdefault("_warn_reranker_disabled", True)

        if self.RAG_CHUNK_OVERLAP_TOKENS >= self.RAG_CHUNK_MAX_TOKENS:
            msg = "RAG_CHUNK_OVERLAP_TOKENS must be smaller than RAG_CHUNK_MAX_TOKENS"
            raise ValueError(msg)

        return self

    def _assert_production_hardening(self) -> None:
        problems: list[str] = []
        if self.JWT_SECRET_KEY.get_secret_value() in _PLACEHOLDER_SECRETS:
            problems.append("JWT_SECRET_KEY is empty or a placeholder")
        if not self.REFRESH_COOKIE_SECURE:
            problems.append("REFRESH_COOKIE_SECURE must be true when APP_ENV=prod (§23)")
        if self.APP_DEBUG:
            problems.append("APP_DEBUG must be false when APP_ENV=prod")
        if self.AI_USE_FAKE_PROVIDERS:
            problems.append("AI_USE_FAKE_PROVIDERS must be false when APP_ENV=prod")
        if self.MCP_ENABLE_DNS_REBINDING_PROTECTION is False:
            problems.append("MCP_ENABLE_DNS_REBINDING_PROTECTION must stay enabled (§88)")
        if problems:
            msg = (
                f"unsafe configuration for APP_ENV={self.APP_ENV}: " + "; ".join(problems)
            )
            raise ValueError(msg)

    # ------------------------------------------------------------------
    # Convenience
    # ------------------------------------------------------------------
    @property
    def is_production(self) -> bool:
        return self.APP_ENV == "prod"

    @property
    def is_hardened(self) -> bool:
        """Environments where insecure-but-convenient defaults are refused.

        Staging is included deliberately: it is internet-reachable in every real
        deployment, so a placeholder JWT secret or a non-Secure refresh cookie
        there is a production incident waiting to be promoted.
        """
        return self.APP_ENV in {"prod", "staging"}

    @property
    def is_testing(self) -> bool:
        return self.APP_ENV == "test"

    @property
    def sync_database_url(self) -> str:
        return self.DATABASE_URL

    @property
    def all_buckets(self) -> tuple[str, ...]:
        return (
            self.S3_BUCKET_PRODUCT_IMAGES,
            self.S3_BUCKET_KNOWLEDGE,
            self.S3_BUCKET_REPORTS,
            self.S3_BUCKET_EVIDENCE,
        )

    def documented_keys(self) -> frozenset[str]:
        """Uppercase env keys this settings object understands."""
        return frozenset(self.model_fields.keys())


@functools.lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached settings accessor.

    Cached so the ``.env`` file is parsed once. Tests clear the cache with
    ``get_settings.cache_clear()`` when they need different values.
    """
    return Settings()


__all__ = ["AppEnv", "CheckpointerBackend", "RagMode", "Settings", "get_settings"]
