import pytest
from pydantic import ValidationError

from app.core.config import Settings

GOOD_SECRET = "x" * 40
GOOD_DB = "postgresql+asyncpg://imageapp:s3cret@db:5432/image_platform"


def test_flow_browser_disabled_by_default() -> None:
    assert Settings(_env_file=None).flow_browser_enabled is False  # type: ignore[call-arg]


def test_local_allows_placeholder_secret() -> None:
    Settings(app_env="local", _env_file=None)  # type: ignore[call-arg]


@pytest.mark.parametrize("env", ["staging", "production"])
def test_non_local_rejects_placeholder_jwt_secret(env: str) -> None:
    with pytest.raises(ValidationError, match="JWT_SECRET"):
        Settings(app_env=env, database_url=GOOD_DB, image_provider="x", _env_file=None)  # type: ignore[arg-type,call-arg]


def test_production_rejects_mock_provider() -> None:
    with pytest.raises(ValidationError, match="mock"):
        Settings(app_env="production", jwt_secret=GOOD_SECRET, database_url=GOOD_DB, _env_file=None)  # type: ignore[call-arg]


def test_production_rejects_flow_browser() -> None:
    with pytest.raises(ValidationError, match="FLOW_BROWSER_ENABLED"):
        Settings(
            app_env="production",
            jwt_secret=GOOD_SECRET,
            database_url=GOOD_DB,
            image_provider="approved_api",
            flow_browser_enabled=True,
            _env_file=None,  # type: ignore[call-arg]
        )


def test_production_valid() -> None:
    s = Settings(
        app_env="production",
        jwt_secret=GOOD_SECRET,
        database_url=GOOD_DB,
        image_provider="approved_api",
        _env_file=None,  # type: ignore[call-arg]
    )
    assert s.flow_browser_enabled is False
