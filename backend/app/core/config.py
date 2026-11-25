"""Application settings, loaded from environment variables (12-factor).

No secret has a usable default: JWT_SECRET, GATEWAY_SHARED_SECRET and storage
credentials must be provided via the environment / secret manager.
"""
from __future__ import annotations

from functools import lru_cache
from typing import Annotated, Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

CsvList = Annotated[list[str], NoDecode]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)

    app_name: str = "google-flow-platform"
    environment: Literal["development", "test", "production"] = "development"
    log_level: str = "INFO"

    # --- Database / Redis -------------------------------------------------
    database_url: str = Field(..., description="postgresql+asyncpg://user:pass@host:5432/db")
    db_pool_size: int = 10
    db_max_overflow: int = 20
    redis_url: str = "redis://localhost:6379/0"

    # --- Auth -------------------------------------------------------------
    jwt_secret: str = Field(..., min_length=32)
    jwt_algorithm: str = "HS256"
    jwt_issuer: str = "google-flow-platform"
    access_token_ttl_seconds: int = 900
    refresh_token_ttl_seconds: int = 60 * 60 * 24 * 30

    # --- HTTP hardening ---------------------------------------------------
    cors_allow_origins: CsvList = Field(default_factory=list)
    max_request_bytes: int = 1_048_576
    rate_limit_enabled: bool = True
    rate_limit_auth_per_minute: int = 20
    rate_limit_generations_per_minute: int = 30
    rate_limit_default_per_minute: int = 240
    trusted_proxy_count: int = 0

    # --- Credits ----------------------------------------------------------
    signup_bonus_credits: int = 10
    credit_cost_per_image: int = 1
    non_refundable_error_codes: CsvList = Field(default_factory=lambda: ["CONTENT_POLICY_VIOLATION"])

    # --- Generation -------------------------------------------------------
    default_provider: str = "mock"
    max_images_per_generation: int = 4
    max_prompt_length: int = 2000
    allowed_aspect_ratios: CsvList = Field(default_factory=lambda: ["1:1", "16:9", "9:16", "4:3", "3:4"])

    # --- Providers --------------------------------------------------------
    enabled_providers: CsvList = Field(default_factory=lambda: ["mock"])
    # Google Flow connector stays disabled until usage rights and an approved
    # integration method are confirmed. Both flags must be set explicitly.
    google_flow_enabled: bool = False
    google_flow_integration_confirmed: bool = False

    # --- Scheduler / worker protocol ---------------------------------------
    scheduler_poll_interval_seconds: float = 1.0
    scheduler_batch_size: int = 50
    offer_ttl_seconds: int = 15
    job_timeout_seconds: int = 600
    worker_offline_after_seconds: int = 90
    heartbeat_interval_seconds: int = 25
    max_dispatch_attempts: int = 5
    queue_timeout_seconds: int = 1800
    # Concurrency cap per provider account (respects provider limits; jobs are
    # never moved to another account to evade a limit).
    max_concurrent_jobs_per_account: int = 2

    # --- Storage (S3 compatible: MinIO / Cloudflare R2 / AWS S3) ----------
    s3_endpoint_url: str | None = None  # internal endpoint used by backend
    s3_public_endpoint_url: str | None = None  # endpoint embedded in signed URLs
    s3_region: str = "us-east-1"
    s3_access_key_id: str = Field(...)
    s3_secret_access_key: str = Field(...)
    s3_bucket: str = "flow-assets"
    s3_create_bucket: bool = False
    s3_addressing_style: Literal["path", "virtual", "auto"] = "path"
    signed_url_ttl_seconds: int = 600
    upload_url_ttl_seconds: int = 900
    max_image_bytes: int = 20 * 1024 * 1024
    max_image_pixels: int = 64_000_000
    allowed_image_mime_types: CsvList = Field(default_factory=lambda: ["image/png", "image/jpeg", "image/webp"])
    thumbnail_size: int = 384

    # --- Worker PKI / mTLS gateway -----------------------------------------
    pki_ca_cert_path: str = "/certs/ca.crt"
    pki_ca_key_path: str = "/certs/ca.key"
    agent_cert_ttl_days: int = 365
    enrollment_token_ttl_seconds: int = 60 * 60 * 24
    # Shared secret the mTLS reverse proxy injects; the backend rejects agent
    # connections that do not carry it (prevents header spoofing when the
    # backend port is reachable directly).
    gateway_shared_secret: str = Field(..., min_length=24)

    @field_validator(
        "cors_allow_origins",
        "allowed_aspect_ratios",
        "enabled_providers",
        "allowed_image_mime_types",
        "non_refundable_error_codes",
        mode="before",
    )
    @classmethod
    def _split_csv(cls, v: object) -> object:
        if isinstance(v, str):
            v = v.strip()
            if v.startswith("["):
                import json

                return json.loads(v)
            return [item.strip() for item in v.split(",") if item.strip()]
        return v

    @property
    def is_production(self) -> bool:
        return self.environment == "production"


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
