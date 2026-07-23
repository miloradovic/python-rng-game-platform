"""Concurrent session creation is serialized by the player row."""

import asyncio
from uuid import UUID, uuid4

import pytest

from app import services
from app.config import get_settings
from app.database import Database
from app.models import GameSession, Player
from tools.seed import seed_catalogue

pytestmark = pytest.mark.concurrency


async def test_concurrent_retry_returns_one_session() -> None:
    database = Database(get_settings())
    player_id = uuid4()
    request_id = uuid4()
    try:
        async with database.session_factory.begin() as session:
            await seed_catalogue(session)
            session.add(Player(id=player_id, display_name="Concurrent Test"))

        async def create() -> GameSession:
            async with database.session_factory() as session:
                return await services.create_session(
                    session,
                    request_id=request_id,
                    player_id=player_id,
                    owner_id=player_id,
                    game_key="skill_check",
                )

        first, second = await asyncio.gather(create(), create())
        assert first.id == second.id
    finally:
        await database.dispose()


async def test_cross_player_request_key_race_returns_one_stable_conflict() -> None:
    database = Database(get_settings())
    first_player, second_player, request_id = uuid4(), uuid4(), uuid4()
    try:
        async with database.session_factory.begin() as session:
            await seed_catalogue(session)
            session.add_all(
                [
                    Player(id=first_player, display_name="First Intent"),
                    Player(id=second_player, display_name="Second Intent"),
                ]
            )

        async def create(player_id: UUID) -> object:
            async with database.session_factory() as session:
                return await services.create_session(
                    session,
                    request_id=request_id,
                    player_id=player_id,
                    owner_id=player_id,
                    game_key="skill_check",
                )

        results = await asyncio.gather(
            create(first_player), create(second_player), return_exceptions=True
        )
        assert sum(isinstance(result, GameSession) for result in results) == 1
        assert sum(isinstance(result, services.IdempotencyConflictError) for result in results) == 1
    finally:
        await database.dispose()
