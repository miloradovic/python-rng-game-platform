"""Shared typed fixtures for the foundation test suite."""

import pytest

from app.config import Settings


@pytest.fixture
def settings() -> Settings:
    """Return isolated settings that do not read a developer's local env file."""

    return Settings(
        _env_file=None,
        app_env="test",
        database_url="postgresql+asyncpg://test:test@db:5432/test",
        redis_url=None,
        outcome_hmac_secret="x" * 32,
    )
