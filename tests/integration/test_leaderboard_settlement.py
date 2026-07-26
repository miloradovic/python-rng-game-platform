"""PostgreSQL-authoritative scores and disposable Redis leaderboard integration."""

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import repositories, services
from app.cache import (
    ProjectionKeys,
    create_redis_client,
    leaderboard_key,
    leaderboard_pointer_key,
    projection_generation_keys,
)
from app.config import get_settings
from app.database import Database
from app.models import (
    AuditRecord,
    FinalScore,
    GameSession,
    Outcome,
    OutcomeStatus,
    Player,
    Reward,
    RewardTierConfig,
    SessionStatus,
    SettlementRecipient,
    SettlementRun,
    SettlementStatus,
)
from app.rng import HmacOutcomeProvider
from tools.rebuild_leaderboard import rebuild_leaderboard
from tools.seed import seed_catalogue

pytestmark = pytest.mark.integration


async def _current_projection_keys(
    redis: Redis, game_key: str, period_start: datetime
) -> ProjectionKeys:
    period_key = period_start.strftime("%Y%m%dT%H%M%SZ")
    pointer = leaderboard_pointer_key(game_key, period_key)
    generation = await redis.get(pointer)
    assert isinstance(generation, str)
    return projection_generation_keys(game_key, period_key, generation)


async def _completed_skill_session(
    database: Database, player_id: UUID
) -> tuple[GameSession, Outcome]:
    provider = HmacOutcomeProvider("leaderboard-integration-secret-key")
    async with database.session_factory() as session:
        game_session = await services.create_session(
            session,
            request_id=uuid4(),
            player_id=player_id,
            owner_id=player_id,
            game_key="skill_check",
        )
        outcome = await services.play_session(
            session,
            session_id=game_session.id,
            owner_id=player_id,
            choice=None,
            actions=list(game_session.challenge["sequence"]),
            provider=provider,
        )
    return game_session, outcome


async def _add_settlement_score(
    session: AsyncSession,
    *,
    player_id: UUID,
    game_id: UUID,
    config_version_id: UUID,
    period_start: datetime,
    completed_at: datetime,
    final_score: int,
    session_id: UUID,
) -> FinalScore:
    game_session = GameSession(
        id=session_id,
        player_id=player_id,
        game_id=game_id,
        config_version_id=config_version_id,
        request_id=uuid4(),
        request_fingerprint="b" * 64,
        status=SessionStatus.COMPLETED,
        expires_at=period_start + timedelta(days=6),
        ended_at=completed_at,
        challenge={},
    )
    session.add(game_session)
    await session.flush()
    outcome = Outcome(
        id=uuid4(),
        session_id=game_session.id,
        player_id=player_id,
        game_id=game_id,
        config_version_id=config_version_id,
        status=OutcomeStatus.ACCEPTED,
        result={"score": final_score},
    )
    session.add(outcome)
    await session.flush()
    score = FinalScore(
        player_id=player_id,
        game_id=game_id,
        session_id=game_session.id,
        outcome_id=outcome.id,
        config_version_id=config_version_id,
        period_start=period_start,
        completed_at=completed_at,
        final_score=final_score,
    )
    session.add(score)
    await session.flush()
    return score


async def test_settlement_ranks_unique_players_by_deterministic_best_score() -> None:
    database = Database(get_settings())
    period_start = datetime(2026, 7, 6, tzinfo=UTC)
    settlement_time = period_start + timedelta(days=7)
    player_ids = [uuid4() for _ in range(4)]
    try:
        async with database.session_factory.begin() as session:
            await seed_catalogue(session)
            session.add_all(
                Player(id=player_id, display_name=f"Ranked Player {index}")
                for index, player_id in enumerate(player_ids, start=1)
            )
            await session.flush()
            game = await repositories.get_game(session, "skill_check")
            assert game is not None
            config = await repositories.get_active_config(session, game.id)
            assert config is not None

            first_player_later_session = await _add_settlement_score(
                session,
                player_id=player_ids[0],
                game_id=game.id,
                config_version_id=config.id,
                period_start=period_start,
                completed_at=period_start + timedelta(days=1),
                final_score=1000,
                session_id=UUID(int=2),
            )
            first_player_best = await _add_settlement_score(
                session,
                player_id=player_ids[0],
                game_id=game.id,
                config_version_id=config.id,
                period_start=period_start,
                completed_at=period_start + timedelta(days=1),
                final_score=1000,
                session_id=UUID(int=1),
            )
            await _add_settlement_score(
                session,
                player_id=player_ids[1],
                game_id=game.id,
                config_version_id=config.id,
                period_start=period_start,
                completed_at=period_start + timedelta(days=2),
                final_score=900,
                session_id=UUID(int=20),
            )
            await _add_settlement_score(
                session,
                player_id=player_ids[3],
                game_id=game.id,
                config_version_id=config.id,
                period_start=period_start,
                completed_at=period_start + timedelta(days=3),
                final_score=800,
                session_id=UUID(int=40),
            )
            await _add_settlement_score(
                session,
                player_id=player_ids[2],
                game_id=game.id,
                config_version_id=config.id,
                period_start=period_start,
                completed_at=period_start + timedelta(days=3),
                final_score=800,
                session_id=UUID(int=30),
            )
            session.add(
                RewardTierConfig(
                    id=uuid4(),
                    game_id=game.id,
                    version=2,
                    payload={
                        "tiers": [
                            {
                                "key": f"rank_{rank}",
                                "min_rank": rank,
                                "max_rank": rank,
                                "reward_value": 500 - rank,
                            }
                            for rank in range(1, 5)
                        ]
                    },
                    published_at=period_start,
                )
            )

        async with database.session_factory() as session:
            run, recipients = await services.settle_leaderboard(
                session,
                game_key="skill_check",
                period_start=period_start,
                authorized=True,
                clock=lambda: settlement_time,
            )
        async with database.session_factory() as session:
            retried_run, retried_recipients = await services.settle_leaderboard(
                session,
                game_key="skill_check",
                period_start=period_start,
                authorized=True,
                clock=lambda: settlement_time,
            )

        assert first_player_later_session.id != first_player_best.id
        assert [recipient.rank for recipient in recipients] == [1, 2, 3, 4]
        assert [recipient.player_id for recipient in recipients] == player_ids
        assert recipients[0].score_id == first_player_best.id
        assert [recipient.tier_key for recipient in recipients] == [
            "rank_1",
            "rank_2",
            "rank_3",
            "rank_4",
        ]
        assert retried_run.id == run.id
        assert [
            (
                recipient.id,
                recipient.player_id,
                recipient.score_id,
                recipient.rank,
                recipient.reward_value,
            )
            for recipient in retried_recipients
        ] == [
            (
                recipient.id,
                recipient.player_id,
                recipient.score_id,
                recipient.rank,
                recipient.reward_value,
            )
            for recipient in recipients
        ]
    finally:
        await database.dispose()


async def test_closed_period_settlement_is_concurrent_and_reward_idempotent() -> None:
    database = Database(get_settings())
    player_id = uuid4()
    period_start = datetime(2026, 7, 13, tzinfo=UTC)
    settlement_time = period_start + timedelta(days=7)
    try:
        async with database.session_factory.begin() as session:
            await seed_catalogue(session)
            player = Player(id=player_id, display_name="Settlement Winner")
            session.add(player)
            await session.flush()
            game = await repositories.get_game(session, "skill_check")
            assert game is not None
            config = await repositories.get_active_config(session, game.id)
            assert config is not None
            game_session = GameSession(
                id=uuid4(),
                player_id=player_id,
                game_id=game.id,
                config_version_id=config.id,
                request_id=uuid4(),
                request_fingerprint="a" * 64,
                status=SessionStatus.COMPLETED,
                expires_at=period_start + timedelta(days=2),
                ended_at=period_start + timedelta(days=1),
                challenge={},
            )
            session.add(game_session)
            await session.flush()
            outcome = Outcome(
                id=uuid4(),
                session_id=game_session.id,
                player_id=player_id,
                game_id=game.id,
                config_version_id=config.id,
                status=OutcomeStatus.ACCEPTED,
                result={"score": 900},
            )
            session.add(outcome)
            await session.flush()
            score = FinalScore(
                player_id=player_id,
                game_id=game.id,
                session_id=game_session.id,
                outcome_id=outcome.id,
                config_version_id=config.id,
                period_start=period_start,
                completed_at=game_session.ended_at,
                final_score=900,
            )
            session.add(score)
            await session.flush()
            await _add_settlement_score(
                session,
                player_id=player_id,
                game_id=game.id,
                config_version_id=config.id,
                period_start=period_start,
                completed_at=period_start + timedelta(days=2),
                final_score=800,
                session_id=uuid4(),
            )
            session.add(
                RewardTierConfig(
                    id=uuid4(),
                    game_id=game.id,
                    version=3,
                    payload={
                        "tiers": [
                            {
                                "key": "champion",
                                "min_rank": 1,
                                "max_rank": 1,
                                "reward_value": 500,
                            }
                        ]
                    },
                    published_at=period_start,
                )
            )
            await session.flush()
            tier_config = await repositories.settlement_tier_config(
                session, game_id=game.id, period_end=period_start + timedelta(days=7)
            )
            assert tier_config is not None
            run = SettlementRun(
                game_id=game.id,
                period_start=period_start,
                period_end=period_start + timedelta(days=7),
                tier_config_id=tier_config.id,
                tier_snapshot=tier_config.payload,
                status=SettlementStatus.PROCESSING,
                completed_at=None,
            )
            session.add(run)
            await session.flush()
            session.add(
                SettlementRecipient(
                    run_id=run.id,
                    player_id=player_id,
                    score_id=score.id,
                    game_id=game.id,
                    period_start=period_start,
                    rank=1,
                    tier_key="champion",
                    reward_value=500,
                )
            )

        async def settle() -> tuple[SettlementRun, list[SettlementRecipient]]:
            async with database.session_factory() as session:
                return await services.settle_leaderboard(
                    session,
                    game_key="skill_check",
                    period_start=period_start,
                    authorized=True,
                    clock=lambda: settlement_time,
                )

        results = await asyncio.gather(settle(), settle())
        assert results[0][0].id == results[1][0].id
        assert len(results[0][1]) == 1
        assert results[0][1][0].score_id == score.id
        async with database.session_factory() as session:
            rewards = await session.scalar(
                select(func.count())
                .select_from(Reward)
                .where(Reward.settlement_recipient_id == results[0][1][0].id)
            )
            audits = await session.scalar(
                select(func.count())
                .select_from(AuditRecord)
                .where(
                    AuditRecord.entity_id == results[0][0].id,
                    AuditRecord.event_type == "leaderboard_settled",
                )
            )
        assert rewards == 1
        assert audits == 1
    finally:
        await database.dispose()


async def test_rebuild_removes_corruption_and_preserves_projection_isolation() -> None:
    """A target is replaced from PostgreSQL without touching a separate scope."""

    database = Database(get_settings())
    redis = create_redis_client(get_settings())
    assert redis is not None
    player_id = uuid4()
    try:
        async with database.session_factory.begin() as session:
            await seed_catalogue(session)
            session.add(Player(id=player_id, display_name="Corrupt Projection"))
        game_session, _ = await _completed_skill_session(database, player_id)
        async with database.session_factory() as session:
            score, _ = await services.submit_final_score(
                session, session_id=game_session.id, owner_id=player_id
            )
            period_key = score.period_start.strftime("%Y%m%dT%H%M%SZ")
            unrelated = leaderboard_key("daily_spin", period_key)
            game = await repositories.get_game(session, "skill_check")
            assert game is not None
            expected_count = await repositories.count_canonical_scores(
                session, game_id=game.id, period_start=score.period_start
            )
            assert (
                await rebuild_leaderboard(
                    session, redis, game_key="skill_check", period_start=score.period_start
                )
                == expected_count
            )
            old_keys = await _current_projection_keys(redis, "skill_check", score.period_start)
            await redis.zadd(old_keys.members, {"corrupt-member": -1_000_000})
            await redis.set(unrelated, "unrelated")
            assert (
                await rebuild_leaderboard(
                    session, redis, game_key="skill_check", period_start=score.period_start
                )
                == expected_count
            )
            new_keys = await _current_projection_keys(redis, "skill_check", score.period_start)

        assert new_keys != old_keys
        assert 0 < await redis.ttl(old_keys.members) <= 300
        assert await redis.zcard(new_keys.members) == expected_count
        assert await redis.get(unrelated) == "unrelated"
    finally:
        await redis.aclose()
        await database.dispose()
