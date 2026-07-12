"""PostgreSQL coverage for catalogue invariants."""

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError

from app.config import get_settings
from app.database import Database
from app.models import Game, GameConfigVersion
from tools.seed import seed_catalogue

pytestmark = pytest.mark.integration


async def test_seed_is_repeatable_and_published_configs_are_immutable() -> None:
    database = Database(get_settings())
    try:
        async with database.session_factory.begin() as session:
            await seed_catalogue(session)
            await seed_catalogue(session)
        async with database.session_factory() as session:
            assert await session.scalar(select(func.count()).select_from(Game)) == 3
            assert await session.scalar(select(func.count()).select_from(GameConfigVersion)) == 3
            config_id = await session.scalar(select(GameConfigVersion.id).limit(1))
            with pytest.raises(DBAPIError):
                await session.execute(
                    text("UPDATE game_config_versions SET version = 2 WHERE id = :id"),
                    {"id": config_id},
                )
    finally:
        await database.dispose()
