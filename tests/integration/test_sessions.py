"""Real PostgreSQL session/config binding coverage."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from app import services
from app.config import get_settings
from app.database import Database
from app.models import ConfigStatus, Game, GameConfigVersion, Player
from app.rng import HmacOutcomeProvider
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


async def test_config_rollover_cannot_reinterpret_an_existing_session_or_outcome() -> None:
    database = Database(get_settings())
    original_player_id = uuid4()
    new_player_id = uuid4()
    provider = HmacOutcomeProvider("config-history-integration-secret-32-bytes")
    try:
        async with database.session_factory.begin() as session:
            await seed_catalogue(session)
            session.add_all(
                [
                    Player(id=original_player_id, display_name="Historical Config"),
                    Player(id=new_player_id, display_name="Current Config"),
                ]
            )
        async with database.session_factory() as session:
            original_session = await services.create_session(
                session,
                request_id=uuid4(),
                player_id=original_player_id,
                owner_id=original_player_id,
                game_key="skill_check",
            )
            original_config_id = original_session.config_version_id

        async with database.session_factory.begin() as session:
            game = await session.scalar(select(Game).where(Game.key == "skill_check"))
            original_config = await session.get(GameConfigVersion, original_config_id)
            assert game is not None
            assert original_config is not None
            original_max_score = original_config.payload["max_score"]
            assert isinstance(original_max_score, int)
            latest_version = await session.scalar(
                select(func.max(GameConfigVersion.version)).where(
                    GameConfigVersion.game_id == game.id
                )
            )
            assert latest_version is not None
            original_config.status = ConfigStatus.RETIRED
            session.add(
                GameConfigVersion(
                    id=uuid4(),
                    game_id=game.id,
                    version=latest_version + 1,
                    status=ConfigStatus.PUBLISHED,
                    payload={
                        "game_type": "skill_check",
                        "cooldown_seconds": 60,
                        "duration_seconds": 30,
                        "max_score": original_max_score + 1000,
                    },
                    published_at=datetime.now(UTC),
                )
            )

        async with database.session_factory() as session:
            outcome = await services.play_session(
                session,
                session_id=original_session.id,
                owner_id=original_player_id,
                choice=None,
                actions=list(original_session.challenge["sequence"]),
                provider=provider,
            )
            reward = await services.claim_session_reward(
                session, session_id=original_session.id, owner_id=original_player_id
            )
            assert outcome.config_version_id == original_config_id
            assert outcome.result["score"] == original_max_score
            assert reward.value == original_max_score

        async with database.session_factory() as session:
            current_session = await services.create_session(
                session,
                request_id=uuid4(),
                player_id=new_player_id,
                owner_id=new_player_id,
                game_key="skill_check",
            )
            assert current_session.config_version_id != original_config_id
    finally:
        await database.dispose()
