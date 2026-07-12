"""Concurrent play cannot create two terminal outcomes."""

import asyncio
from uuid import uuid4

import pytest

from app import services
from app.config import get_settings
from app.database import Database
from app.models import Outcome, Player
from app.rng import HmacOutcomeProvider
from tools.seed import seed_catalogue

pytestmark = pytest.mark.concurrency


async def test_concurrent_play_has_one_durable_winner() -> None:
    database = Database(get_settings())
    player_id = uuid4()
    provider = HmacOutcomeProvider("concurrent-gameplay-secret-key-32bytes")
    try:
        async with database.session_factory.begin() as session:
            await seed_catalogue(session)
            session.add(Player(id=player_id, display_name="Concurrent Play"))
        async with database.session_factory() as session:
            game_session = await services.create_session(
                session,
                request_id=uuid4(),
                player_id=player_id,
                owner_id=player_id,
                game_key="daily_spin",
            )

        async def play() -> object:
            async with database.session_factory() as session:
                return await services.play_session(
                    session,
                    session_id=game_session.id,
                    owner_id=player_id,
                    choice=None,
                    actions=None,
                    provider=provider,
                )

        results = await asyncio.gather(play(), play(), return_exceptions=True)
        assert sum(isinstance(result, Outcome) for result in results) == 1
        assert sum(isinstance(result, services.InvalidTransitionError) for result in results) == 1
    finally:
        await database.dispose()
