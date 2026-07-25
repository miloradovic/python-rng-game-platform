"""Unit tests for fail-fast typed configuration."""

import pytest
from pydantic import ValidationError

from app.config import AppEnvironment, Settings, validate_test_redis_url

pytestmark = pytest.mark.unit


def test_settings_accept_async_connection_schemes() -> None:
    """Supported async PostgreSQL and Redis URLs validate successfully."""

    settings = Settings(
        _env_file=None,
        app_env="test",
        database_url="postgresql+asyncpg://user:password@db:5432/database",
        redis_url="redis://redis:6379/0",
        leaderboard_projection_hmac_secret="p" * 32,
        outcome_hmac_secret="x" * 32,
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


def test_settings_require_outcome_hmac_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    """Player-visible cryptographic outcomes fail closed without a key."""

    monkeypatch.delenv("OUTCOME_HMAC_SECRET")
    with pytest.raises(ValidationError, match="outcome_hmac_secret"):
        Settings(
            _env_file=None,
            database_url="postgresql+asyncpg://user:password@db:5432/database",
        )


def test_production_rejects_development_credentials_and_transport() -> None:
    """Production cannot start with Compose defaults or plaintext Redis."""

    with pytest.raises(ValidationError, match="ssl=require"):
        Settings(
            _env_file=None,
            app_env="production",
            database_url="postgresql+asyncpg://rng_app:local-password@db/database",
            redis_url="redis://redis/0",
            outcome_hmac_secret="development-only-outcome-key-32-bytes-minimum",  # noqa: S106
            settlement_admin_token="development-only-settlement-token",  # noqa: S106
        )


def test_production_accepts_explicit_secure_configuration() -> None:
    """Production validation accepts encrypted connections and non-default secrets."""

    settings = Settings(
        _env_file=None,
        app_env="production",
        database_url="postgresql+asyncpg://rng_app:strong-password@db/database?ssl=require",
        redis_url="rediss://redis/0",
        leaderboard_projection_hmac_secret="p" * 32,
        outcome_hmac_secret="o" * 32,
        settlement_admin_token="s" * 32,
    )

    assert settings.app_env is AppEnvironment.PRODUCTION


def test_redis_requires_projection_integrity_key() -> None:
    """Redis cannot be configured without keyed projection validation."""

    with pytest.raises(ValidationError, match="LEADERBOARD_PROJECTION_HMAC_SECRET"):
        Settings(
            _env_file=None,
            database_url="postgresql+asyncpg://user:password@db/database",
            redis_url="redis://redis/1",
            leaderboard_projection_hmac_secret=None,
            outcome_hmac_secret="o" * 32,
        )


def test_production_rejects_development_projection_key() -> None:
    with pytest.raises(ValidationError, match="cannot use the development value"):
        Settings(
            _env_file=None,
            app_env="production",
            database_url="postgresql+asyncpg://rng_app:strong-password@db/database?ssl=require",
            redis_url="rediss://redis/0",
            leaderboard_projection_hmac_secret=(  # noqa: S106
                "development-only-projection-key-32-bytes-minimum"
            ),
            outcome_hmac_secret="o" * 32,
            settlement_admin_token="s" * 32,
        )


@pytest.mark.parametrize(
    ("test_url", "development_url", "message"),
    [
        ("redis://redis:6379/0", "redis://redis:6379/0", "database 0"),
        ("redis://redis:6379", "redis://redis:6379/0", "database 0"),
        ("redis://redis:6379/1", "redis://redis:6379/1", "development Redis URL"),
    ],
)
def test_test_redis_url_rejects_unsafe_targets(
    test_url: str, development_url: str, message: str
) -> None:
    """Tests fail closed before connecting to development Redis."""

    with pytest.raises(ValueError, match=message):
        validate_test_redis_url(test_url, development_url)


def test_test_redis_url_accepts_dedicated_nonzero_database() -> None:
    assert (
        validate_test_redis_url("redis://redis:6379/1", "redis://redis:6379/0")
        == "redis://redis:6379/1"
    )
