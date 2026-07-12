"""Concurrent session creation is serialized by the player row."""

import asyncio
from uuid import uuid4

import pytest

from app import services
from app.config import get_settings
from app.database import Database
from app.models import Player
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

        async def create() -> object:
            async with database.session_factory() as session:
                return await services.create_session(
                    session,
                    request_id=request_id,
                    player_id=player_id,
                    owner_id=player_id,
                    game_key="skill_check",
                )

        first, second = await asyncio.gather(create(), create())
        assert first.id == second.id  # type: ignore[attr-defined]
    finally:
        await database.dispose()
