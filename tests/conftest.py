"""Shared typed fixtures and isolated database lifecycle for the test suite."""

import asyncio
import os
from urllib.parse import urlparse
from uuid import uuid4

import pytest
from alembic.config import Config
from sqlalchemy import text

import app.models  # noqa: F401 - register all tables before truncation
from alembic import command
from app.cache import clear_redis_namespace, create_redis_client
from app.config import Settings, get_settings, validate_test_redis_url
from app.database import Base, Database

_TEST_REDIS_NAMESPACE = f"pytest-{uuid4().hex}"
_DEVELOPMENT_REDIS_URL = os.environ.get("REDIS_URL")


def _test_database_url() -> str:
    """Return and validate the dedicated Compose test database URL."""

    database_url = os.environ.get("TEST_DATABASE_URL")
    if database_url is None:
        raise pytest.UsageError("TEST_DATABASE_URL is required for the containerized test suite")
    expected_database = os.environ.get("TEST_POSTGRES_DB", "rng_game_platform_test")
    if urlparse(database_url).path.removeprefix("/") != expected_database:
        raise pytest.UsageError("TEST_DATABASE_URL must target TEST_POSTGRES_DB, never development")
    return database_url


def _configure_test_environment() -> None:
    """Redirect all application settings reads to isolated infrastructure."""

    os.environ["APP_ENV"] = "test"
    os.environ["DATABASE_URL"] = _test_database_url()
    test_redis_url = os.environ.get("TEST_REDIS_URL")
    if test_redis_url is None:
        raise pytest.UsageError("TEST_REDIS_URL is required for the containerized test suite")
    if _DEVELOPMENT_REDIS_URL is None:
        raise pytest.UsageError(
            "REDIS_URL is required to prove TEST_REDIS_URL is not the development target"
        )
    try:
        validate_test_redis_url(test_redis_url, _DEVELOPMENT_REDIS_URL)
    except ValueError as error:
        raise pytest.UsageError(str(error)) from error
    os.environ["REDIS_URL"] = test_redis_url
    os.environ["REDIS_KEY_NAMESPACE"] = _TEST_REDIS_NAMESPACE
    get_settings.cache_clear()


async def _clear_test_database() -> None:
    """Remove only disposable test data while retaining the migrated schema."""

    database = Database(get_settings())
    try:
        table_names = ", ".join(f'"{table.name}"' for table in Base.metadata.sorted_tables)
        async with database.engine.begin() as connection:
            await connection.execute(text(f"TRUNCATE TABLE {table_names} RESTART IDENTITY CASCADE"))
    finally:
        await database.dispose()


async def _clear_test_redis() -> None:
    """Remove only keys owned by this test run."""

    client = create_redis_client(get_settings())
    if client is None:
        raise pytest.UsageError("TEST_REDIS_URL did not configure a Redis client")
    try:
        await clear_redis_namespace(client, _TEST_REDIS_NAMESPACE)
    finally:
        await client.aclose()


def pytest_sessionstart(session: pytest.Session) -> None:
    """Migrate then clear the test database before collection executes tests."""

    del session
    _configure_test_environment()
    try:
        asyncio.run(_clear_test_redis())
        command.upgrade(Config("alembic.ini"), "head")
        asyncio.run(_clear_test_database())
    except BaseException:
        asyncio.run(_clear_test_redis())
        raise


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Leave the dedicated test database empty for a deterministic next run."""

    del session, exitstatus
    _configure_test_environment()
    try:
        asyncio.run(_clear_test_database())
    finally:
        asyncio.run(_clear_test_redis())


@pytest.fixture
def settings() -> Settings:
    """Return isolated settings without reading a developer's local env file."""

    return Settings(
        _env_file=None,
        app_env="test",
        database_url=_test_database_url(),
        redis_url=os.environ["TEST_REDIS_URL"],
        redis_key_namespace=_TEST_REDIS_NAMESPACE,
        outcome_hmac_secret="x" * 32,
    )
