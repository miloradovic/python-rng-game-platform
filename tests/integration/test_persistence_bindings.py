"""PostgreSQL enforcement for cross-aggregate and evidence invariants."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import Database
from app.models import (
    ConfigStatus,
    FinalScore,
    Game,
    GameConfigVersion,
    GameSession,
    Outcome,
    OutcomeStatus,
    Player,
    Reward,
    RewardStatus,
    RewardTierConfig,
    SessionStatus,
    SettlementRecipient,
    SettlementRun,
    SettlementStatus,
)
from tools.seed import seed_catalogue

pytestmark = pytest.mark.integration


async def _catalogue_game(session: AsyncSession, game_key: str) -> tuple[Game, GameConfigVersion]:
    game = await session.scalar(select(Game).where(Game.key == game_key))
    assert game is not None
    config = await session.scalar(
        select(GameConfigVersion).where(
            GameConfigVersion.game_id == game.id,
            GameConfigVersion.status == ConfigStatus.PUBLISHED,
        )
    )
    assert config is not None
    return game, config


async def test_postgresql_rejects_cross_aggregate_domain_bindings() -> None:
    """Composite FKs reject inconsistent configuration, ownership, reward, and settlement data."""

    database = Database(get_settings())
    owner_id, other_player_id = uuid4(), uuid4()
    session_id = uuid4()
    period_start = datetime(2026, 1, 5, tzinfo=UTC)
    try:
        async with database.session_factory.begin() as session:
            await seed_catalogue(session)
            session.add_all(
                [
                    Player(id=owner_id, display_name="Binding Owner"),
                    Player(id=other_player_id, display_name="Binding Other"),
                ]
            )
            await session.flush()
            skill_game, skill_config = await _catalogue_game(session, "skill_check")
            daily_game, daily_config = await _catalogue_game(session, "daily_spin")

        async with database.session_factory() as session:
            session.add(
                GameSession(
                    id=uuid4(),
                    player_id=other_player_id,
                    game_id=daily_game.id,
                    config_version_id=skill_config.id,
                    request_id=uuid4(),
                    request_fingerprint="a" * 64,
                    status=SessionStatus.ACTIVE,
                    expires_at=period_start + timedelta(hours=1),
                    ended_at=None,
                    challenge={},
                )
            )
            with pytest.raises(IntegrityError):
                await session.flush()
            await session.rollback()

        async with database.session_factory.begin() as session:
            game_session = GameSession(
                id=session_id,
                player_id=owner_id,
                game_id=skill_game.id,
                config_version_id=skill_config.id,
                request_id=uuid4(),
                request_fingerprint="b" * 64,
                status=SessionStatus.COMPLETED,
                expires_at=period_start + timedelta(days=2),
                ended_at=period_start + timedelta(days=1),
                challenge={},
            )
            session.add(game_session)

        async with database.session_factory() as session:
            session.add(
                Outcome(
                    id=uuid4(),
                    session_id=session_id,
                    player_id=other_player_id,
                    game_id=skill_game.id,
                    config_version_id=skill_config.id,
                    status=OutcomeStatus.ACCEPTED,
                    result={"score": 50},
                )
            )
            with pytest.raises(IntegrityError):
                await session.flush()
            await session.rollback()

        async with database.session_factory() as session:
            session.add(
                Outcome(
                    id=uuid4(),
                    session_id=session_id,
                    player_id=owner_id,
                    game_id=skill_game.id,
                    config_version_id=daily_config.id,
                    status=OutcomeStatus.ACCEPTED,
                    result={"score": 50},
                )
            )
            with pytest.raises(IntegrityError):
                await session.flush()
            await session.rollback()

        outcome_id = uuid4()
        async with database.session_factory.begin() as session:
            outcome = Outcome(
                id=outcome_id,
                session_id=session_id,
                player_id=owner_id,
                game_id=skill_game.id,
                config_version_id=skill_config.id,
                status=OutcomeStatus.ACCEPTED,
                result={"score": 50},
            )
            session.add(outcome)

        async with database.session_factory() as session:
            session.add(
                Reward(
                    outcome_id=outcome_id,
                    settlement_recipient_id=None,
                    player_id=other_player_id,
                    status=RewardStatus.ISSUED,
                    value=50,
                    claimed_at=None,
                )
            )
            with pytest.raises(IntegrityError):
                await session.flush()
            await session.rollback()

        score_id, run_id, recipient_id = uuid4(), uuid4(), uuid4()
        async with database.session_factory.begin() as session:
            score = FinalScore(
                id=score_id,
                player_id=owner_id,
                game_id=skill_game.id,
                session_id=session_id,
                outcome_id=outcome_id,
                config_version_id=skill_config.id,
                period_start=period_start,
                completed_at=period_start + timedelta(days=1),
                final_score=50,
            )
            session.add(score)
            await session.flush()
            tier = RewardTierConfig(
                id=uuid4(),
                game_id=skill_game.id,
                version=99,
                payload={"tiers": []},
                published_at=period_start,
            )
            session.add(tier)
            await session.flush()
            run = SettlementRun(
                id=run_id,
                game_id=skill_game.id,
                period_start=period_start,
                period_end=period_start + timedelta(days=7),
                tier_config_id=tier.id,
                tier_snapshot=tier.payload,
                status=SettlementStatus.COMPLETED,
                completed_at=period_start + timedelta(days=7),
            )
            session.add(run)

        async with database.session_factory() as session:
            session.add(
                SettlementRecipient(
                    run_id=run_id,
                    player_id=other_player_id,
                    score_id=score_id,
                    game_id=skill_game.id,
                    period_start=period_start,
                    rank=1,
                    tier_key="winner",
                    reward_value=10,
                )
            )
            with pytest.raises(IntegrityError):
                await session.flush()
            await session.rollback()

        async with database.session_factory.begin() as session:
            session.add(
                SettlementRecipient(
                    id=recipient_id,
                    run_id=run_id,
                    player_id=owner_id,
                    score_id=score_id,
                    game_id=skill_game.id,
                    period_start=period_start,
                    rank=1,
                    tier_key="winner",
                    reward_value=10,
                )
            )

        async with database.session_factory() as session:
            session.add(
                Reward(
                    outcome_id=None,
                    settlement_recipient_id=recipient_id,
                    player_id=other_player_id,
                    status=RewardStatus.ISSUED,
                    value=10,
                    claimed_at=None,
                )
            )
            with pytest.raises(IntegrityError):
                await session.flush()
            await session.rollback()
    finally:
        await database.dispose()


async def test_postgresql_enforces_safe_unique_public_player_labels() -> None:
    database = Database(get_settings())
    try:
        async with database.session_factory() as session:
            session.add(Player(display_name="First", public_label="Player-ABCDEF123456"))
            await session.flush()
            session.add(Player(display_name="Second", public_label="Player-ABCDEF123456"))
            with pytest.raises(IntegrityError):
                await session.flush()
            await session.rollback()

        async with database.session_factory() as session:
            session.add(Player(display_name="Unsafe", public_label="Unsafe label"))
            with pytest.raises(IntegrityError):
                await session.flush()
            await session.rollback()
    finally:
        await database.dispose()
