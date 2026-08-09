"""PostgreSQL coverage for versioned catalogue invariants."""

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError

from app import repositories
from app.config import get_settings
from app.database import Database
from app.models import ConfigStatus, Game, GameConfigVersion
from tools.seed import seed_catalogue

pytestmark = pytest.mark.integration


async def test_seed_is_repeatable_and_selects_latest_immutable_published_config() -> None:
    database = Database(get_settings())
    try:
        async with database.session_factory.begin() as session:
            assert await seed_catalogue(session) >= 0
            assert await seed_catalogue(session) == 0

        async with database.session_factory() as session:
            assert await session.scalar(select(func.count()).select_from(Game)) == 3
            assert await session.scalar(select(func.count()).select_from(GameConfigVersion)) == 6
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(GameConfigVersion)
                    .where(GameConfigVersion.status == ConfigStatus.PUBLISHED)
                )
                == 6
            )

            expected_cooldowns = {
                "daily_spin": 3600,
                "prediction_card": 60,
                "skill_check": 30,
            }
            games = list((await session.scalars(select(Game).order_by(Game.key))).all())
            for game in games:
                active = await repositories.get_active_config(session, game.id)
                assert active is not None
                assert active.version == 2
                assert active.payload["cooldown_seconds"] == expected_cooldowns[game.key]

            published_ids = list(
                (
                    await session.scalars(
                        select(GameConfigVersion.id).where(
                            GameConfigVersion.status == ConfigStatus.PUBLISHED
                        )
                    )
                ).all()
            )

        for config_id in published_ids:
            async with database.session_factory() as session:
                with pytest.raises(DBAPIError):
                    await session.execute(
                        text(
                            "UPDATE game_config_versions "
                            "SET payload = jsonb_set(payload, '{cooldown_seconds}', '1') "
                            "WHERE id = :id"
                        ),
                        {"id": config_id},
                    )
            async with database.session_factory() as session:
                with pytest.raises(DBAPIError):
                    await session.execute(
                        text("DELETE FROM game_config_versions WHERE id = :id"),
                        {"id": config_id},
                    )
    finally:
        await database.dispose()
