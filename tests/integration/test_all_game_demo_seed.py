"""All-game demo scores and Redis projections remain game-key isolated."""

from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from app.cache import create_redis_client, leaderboard_pointer_key, projection_generation_keys
from app.config import get_settings
from app.database import Database
from app.game_rules import GameKey
from app.models import FinalScore, Game
from app.rng import HmacOutcomeProvider
from tools.seed import seed_catalogue
from tools.seed_demo_leaderboards import seed_all_game_demo_data

pytestmark = pytest.mark.integration


async def test_all_game_demo_seed_is_repeatable_and_projection_keys_do_not_mix() -> None:
    database = Database(get_settings())
    redis = create_redis_client(get_settings())
    assert redis is not None
    provider = HmacOutcomeProvider("all-game-demo-test-secret-key-12345")
    now = datetime(2026, 8, 5, 12, tzinfo=UTC)
    try:
        async with database.session_factory.begin() as session:
            await seed_catalogue(session)
        first = await seed_all_game_demo_data(database, provider, client=redis, clock=lambda: now)
        second = await seed_all_game_demo_data(database, provider, client=redis, clock=lambda: now)

        assert first.sessions_completed == first.scores_created == len(GameKey)
        assert second.sessions_completed == second.scores_created == 0
        assert set(first.projection_rows) == {game_key.value for game_key in GameKey}
        assert second.projection_rows == first.projection_rows

        async with database.session_factory() as session:
            rows = list(
                await session.execute(
                    select(Game.key, FinalScore.id)
                    .join(FinalScore, FinalScore.game_id == Game.id)
                    .where(FinalScore.period_start == first.period_start)
                )
            )
        scores_by_game = {game_key: score_id for game_key, score_id in rows}
        assert set(scores_by_game) == {game_key.value for game_key in GameKey}

        period_key = first.period_start.strftime("%Y%m%dT%H%M%SZ")
        member_keys: set[str] = set()
        for game_key, score_id in scores_by_game.items():
            generation = await redis.get(leaderboard_pointer_key(game_key, period_key))
            assert isinstance(generation, str)
            keys = projection_generation_keys(game_key, period_key, generation)
            member_keys.add(keys.members)
            members = await redis.zrange(keys.members, 0, -1)
            assert len(members) == first.projection_rows[game_key]
            assert any(str(score_id) in member for member in members)
        assert len(member_keys) == len(GameKey)
    finally:
        await redis.aclose()
        await database.dispose()
