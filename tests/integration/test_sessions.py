"""Real PostgreSQL session/config binding coverage."""

from uuid import uuid4

import pytest

from app import services
from app.config import get_settings
from app.database import Database
from app.models import Player
from tools.seed import seed_catalogue

pytestmark = pytest.mark.integration


async def test_session_binds_published_config_and_retry_is_idempotent() -> None:
    database = Database(get_settings())
    player_id = uuid4()
    request_id = uuid4()
    try:
        async with database.session_factory.begin() as session:
            await seed_catalogue(session)
            session.add(Player(id=player_id, display_name="Session Test"))
        async with database.session_factory() as session:
            first = await services.create_session(
                session,
                request_id=request_id,
                player_id=player_id,
                owner_id=player_id,
                game_key="skill_check",
            )
        async with database.session_factory() as session:
            retried = await services.create_session(
                session,
                request_id=request_id,
                player_id=player_id,
                owner_id=player_id,
                game_key="skill_check",
            )
        assert first.id == retried.id
        assert first.config_version_id == retried.config_version_id
    finally:
        await database.dispose()
