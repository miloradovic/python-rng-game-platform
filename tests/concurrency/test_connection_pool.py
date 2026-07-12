"""Foundation concurrency smoke coverage for the async database pool."""

import asyncio

import pytest

from app.config import get_settings
from app.database import Database

pytestmark = [pytest.mark.integration, pytest.mark.concurrency]


async def test_concurrent_database_readiness_checks() -> None:
    """Independent concurrent checks safely share the async engine pool."""

    database = Database(get_settings())
    try:
        await asyncio.gather(*(database.check_connection() for _ in range(10)))
    finally:
        await database.dispose()
