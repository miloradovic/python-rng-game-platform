"""Smoke tests against real Compose-managed PostgreSQL and Redis."""

from uuid import uuid4

import pytest
from sqlalchemy import text

from app.cache import clear_redis_namespace, create_redis_client, leaderboard_key
from app.config import get_settings
from app.database import EXPECTED_ALEMBIC_REVISION, Database

pytestmark = pytest.mark.integration


async def test_postgresql_connection_and_current_revision() -> None:
    """PostgreSQL is reachable and reports the applied reviewed migration."""

    database = Database(get_settings())
    try:
        await database.check_connection()
        async with database.engine.connect() as connection:
            revision = await connection.scalar(text("SELECT version_num FROM alembic_version"))
    finally:
        await database.dispose()

    assert revision == EXPECTED_ALEMBIC_REVISION


async def test_real_redis_connection_when_configured() -> None:
    """The optional Redis service is reachable but used only as a projection seam."""

    client = create_redis_client(get_settings())
    assert client is not None
    try:
        assert await client.ping() is True
    finally:
        await client.aclose()


async def test_redis_namespaces_are_isolated_and_cleanup_is_scoped() -> None:
    """One run cannot observe or remove another run's projection keys."""

    client = create_redis_client(get_settings())
    assert client is not None
    first = f"isolation-{uuid4().hex}"
    second = f"isolation-{uuid4().hex}"
    first_key = leaderboard_key("skill_check", "20260720T000000Z", namespace=first)
    second_key = leaderboard_key("skill_check", "20260720T000000Z", namespace=second)
    try:
        await client.set(first_key, "first")
        await client.set(second_key, "second")

        assert await clear_redis_namespace(client, first) == 1
        assert await client.get(first_key) is None
        assert await client.get(second_key) == "second"
    finally:
        await clear_redis_namespace(client, first)
        await clear_redis_namespace(client, second)
        await client.aclose()


async def test_redis_cleanup_is_safe_after_partial_setup() -> None:
    """Scoped cleanup handles a namespace containing only partial state."""

    client = create_redis_client(get_settings())
    assert client is not None
    namespace = f"partial-{uuid4().hex}"
    temporary_key = f"{namespace}:leaderboard:v1:skill_check:partial:rebuild"
    unrelated_key = f"unrelated-{uuid4().hex}:preserved"
    try:
        await client.set(temporary_key, "partial")
        await client.set(unrelated_key, "unrelated")

        assert await clear_redis_namespace(client, namespace) == 1
        assert await client.get(unrelated_key) == "unrelated"
    finally:
        await client.delete(unrelated_key)
        await clear_redis_namespace(client, namespace)
        await client.aclose()
