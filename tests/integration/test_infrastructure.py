"""Smoke tests against real Compose-managed PostgreSQL and Redis."""

import pytest
from sqlalchemy import text

from app.cache import create_redis_client
from app.config import get_settings
from app.database import Database

pytestmark = pytest.mark.integration


async def test_postgresql_connection_and_foundation_revision() -> None:
    """PostgreSQL is reachable and reports the applied reviewed migration."""

    database = Database(get_settings())
    try:
        await database.check_connection()
        async with database.engine.connect() as connection:
            revision = await connection.scalar(text("SELECT version_num FROM alembic_version"))
    finally:
        await database.dispose()

    assert revision == "0011_persistence_integrity"


async def test_real_redis_connection_when_configured() -> None:
    """The optional Redis service is reachable but used only as a projection seam."""

    client = create_redis_client(get_settings())
    assert client is not None
    try:
        assert await client.ping() is True
    finally:
        await client.aclose()
