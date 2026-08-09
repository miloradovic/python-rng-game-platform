"""Real PostgreSQL session/config binding coverage."""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from app import services
from app.config import get_settings
from app.database import Database
from app.models import Game, GameConfigVersion, GameSession, Player, SessionStatus
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

        async with database.session_factory() as session:
            with pytest.raises(services.IdempotencyConflictError):
                await services.create_session(
                    session,
                    request_id=request_id,
                    player_id=player_id,
                    owner_id=player_id,
                    game_key="prediction_card",
                )

        other_player_id = uuid4()
        async with database.session_factory.begin() as session:
            session.add(Player(id=other_player_id, display_name="Other Request Owner"))
        async with database.session_factory() as session:
            with pytest.raises(services.IdempotencyConflictError):
                await services.create_session(
                    session,
                    request_id=request_id,
                    player_id=other_player_id,
                    owner_id=other_player_id,
                    game_key="skill_check",
                )
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
            game = await session.scalar(select(Game).where(Game.key == "skill_check"))
            assert game is not None
            version_one = await session.scalar(
                select(GameConfigVersion).where(
                    GameConfigVersion.game_id == game.id,
                    GameConfigVersion.version == 1,
                )
            )
            assert version_one is not None
            session.add_all(
                [
                    Player(id=original_player_id, display_name="Historical Config"),
                    Player(id=new_player_id, display_name="Current Config"),
                ]
            )
            await session.flush()
            original_session = GameSession(
                id=uuid4(),
                request_id=uuid4(),
                player_id=original_player_id,
                game_id=game.id,
                config_version_id=version_one.id,
                request_fingerprint="a" * 64,
                status=SessionStatus.ACTIVE,
                expires_at=datetime.now(UTC) + timedelta(seconds=30),
                ended_at=None,
                challenge={"sequence": [1, 2]},
            )
            session.add(original_session)
            await session.flush()
            original_config_id = original_session.config_version_id
            assert original_config_id == version_one.id

        async with database.session_factory() as session:
            outcome = await services.play_session(
                session,
                session_id=original_session.id,
                owner_id=original_player_id,
                choice=None,
                actions=[1, 2],
                provider=provider,
            )
            reward = await services.claim_session_reward(
                session, session_id=original_session.id, owner_id=original_player_id
            )
            assert outcome.config_version_id == original_config_id
            assert outcome.result["score"] == 1000
            assert reward.value == 1000

        during_version_one_cooldown = original_session.created_at + timedelta(seconds=31)
        async with database.session_factory() as session:
            recovered = await services.retrieve_player_game_state(
                session,
                player_id=original_player_id,
                owner_id=original_player_id,
                game_key="skill_check",
                clock=lambda: during_version_one_cooldown,
            )
            assert recovered.game_session is not None
            assert recovered.game_session.config_version_id == original_config_id
            assert recovered.outcome is not None
            assert recovered.next_play_at == original_session.created_at + timedelta(seconds=60)

        async with database.session_factory() as session:
            current_session = await services.create_session(
                session,
                request_id=uuid4(),
                player_id=new_player_id,
                owner_id=new_player_id,
                game_key="skill_check",
            )
            assert current_session.config_version_id != original_config_id
            async with database.session_factory() as verification_session:
                current_config = await verification_session.get(
                    GameConfigVersion, current_session.config_version_id
                )
                assert current_config is not None
                assert current_config.version == 2
    finally:
        await database.dispose()


@pytest.mark.parametrize(
    ("game_key", "cooldown_seconds"),
    [("daily_spin", 3600), ("prediction_card", 60), ("skill_check", 30)],
)
async def test_version_two_cooldown_boundary_uses_server_clock(
    game_key: str, cooldown_seconds: int
) -> None:
    database = Database(get_settings())
    before_player_id = uuid4()
    boundary_player_id = uuid4()
    try:
        async with database.session_factory.begin() as session:
            await seed_catalogue(session)
            session.add_all(
                [
                    Player(id=before_player_id, display_name=f"Before {game_key}"),
                    Player(id=boundary_player_id, display_name=f"Boundary {game_key}"),
                ]
            )

        async def create_then_cancel(player_id: UUID) -> GameSession:
            async with database.session_factory() as session:
                first = await services.create_session(
                    session,
                    request_id=uuid4(),
                    player_id=player_id,
                    owner_id=player_id,
                    game_key=game_key,
                )
            async with database.session_factory() as session:
                await services.cancel_session(session, first.id, player_id)
            return first

        before = await create_then_cancel(before_player_id)
        just_before = before.created_at + timedelta(seconds=cooldown_seconds - 1)
        async with database.session_factory() as session:
            state = await services.retrieve_player_game_state(
                session,
                player_id=before_player_id,
                owner_id=before_player_id,
                game_key=game_key,
                clock=lambda: just_before,
            )
            assert state.next_play_at == before.created_at + timedelta(seconds=cooldown_seconds)
        async with database.session_factory() as session:
            with pytest.raises(services.CooldownError):
                await services.create_session(
                    session,
                    request_id=uuid4(),
                    player_id=before_player_id,
                    owner_id=before_player_id,
                    game_key=game_key,
                    clock=lambda: just_before,
                )

        boundary = await create_then_cancel(boundary_player_id)
        exactly_available = boundary.created_at + timedelta(seconds=cooldown_seconds)
        async with database.session_factory() as session:
            replacement = await services.create_session(
                session,
                request_id=uuid4(),
                player_id=boundary_player_id,
                owner_id=boundary_player_id,
                game_key=game_key,
                clock=lambda: exactly_available,
            )
            assert replacement.id != boundary.id
    finally:
        await database.dispose()
