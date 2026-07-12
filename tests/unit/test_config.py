"""Unit tests for fail-fast typed configuration."""

import pytest
from pydantic import ValidationError

from app.config import AppEnvironment, Settings

pytestmark = pytest.mark.unit


def test_settings_accept_async_connection_schemes() -> None:
    """Supported async PostgreSQL and Redis URLs validate successfully."""

    settings = Settings(
        _env_file=None,
        app_env="test",
        database_url="postgresql+asyncpg://user:password@db:5432/database",
        redis_url="redis://redis:6379/0",
    )

    assert settings.app_env is AppEnvironment.TEST
    assert settings.database_url.get_secret_value().startswith("postgresql+asyncpg://")
    assert "password" not in repr(settings)


@pytest.mark.parametrize(
    "database_url",
    [
        "postgresql://user:password@db:5432/database",
        "sqlite+aiosqlite:///database.db",
    ],
)
def test_settings_reject_non_asyncpg_database_scheme(database_url: str) -> None:
    """A second or synchronous database access model fails validation."""

    with pytest.raises(ValidationError, match="postgresql\\+asyncpg"):
        Settings(_env_file=None, database_url=database_url)


def test_settings_require_database_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """Application configuration fails fast without authoritative storage."""

    monkeypatch.delenv("DATABASE_URL")
    with pytest.raises(ValidationError, match="database_url"):
        Settings(_env_file=None)
