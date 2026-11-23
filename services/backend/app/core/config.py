from functools import lru_cache
from typing import Literal

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_PLACEHOLDER_SECRETS = {"", "REPLACE_WITH_RANDOM_SECRET", "CHANGE_ME", "REPLACE_ME"}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_env: Literal["local", "test", "staging", "production"] = "local"
    database_url: str = "postgresql+asyncpg://imageapp:CHANGE_ME@localhost:5432/image_platform"
    redis_url: str = "redis://localhost:6379/0"

    jwt_secret: str = "REPLACE_WITH_RANDOM_SECRET"
    jwt_access_ttl_minutes: int = 15
    jwt_refresh_ttl_days: int = 30

    image_provider: str = "mock"
    # Spec: Flow browser connector is OFF by default and may only be enabled after
    # legal/ToS verification (Milestone 0).
    flow_browser_enabled: bool = False

    s3_endpoint: str | None = None
    s3_bucket: str = "generated-images"
    s3_access_key_id: str | None = None
    s3_secret_access_key: str | None = None

    health_check_timeout_seconds: float = 2.0

    @model_validator(mode="after")
    def _validate_for_environment(self) -> "Settings":
        if self.app_env in ("staging", "production"):
            if self.jwt_secret in _PLACEHOLDER_SECRETS or len(self.jwt_secret) < 32:
                raise ValueError(
                    "JWT_SECRET must be a random value of >= 32 chars outside local/test"
                )
            if "CHANGE_ME" in self.database_url:
                raise ValueError("DATABASE_URL still contains the CHANGE_ME placeholder")
            if self.image_provider == "mock" and self.app_env == "production":
                raise ValueError("IMAGE_PROVIDER=mock is not allowed in production")
        if self.flow_browser_enabled and self.app_env == "production":
            raise ValueError(
                "FLOW_BROWSER_ENABLED is refused in production until Milestone 0 sign-off"
            )
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
