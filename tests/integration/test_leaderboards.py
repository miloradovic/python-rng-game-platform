"""PostgreSQL-authoritative scores and disposable Redis leaderboard integration."""

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import repositories, services
from app.cache import create_redis_client, leaderboard_key
from app.config import get_settings
from app.database import Database, get_session
from app.main import create_app
from app.models import (
    AnalyticsEvent,
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
from tools.rebuild_leaderboard import _mapping, rebuild_leaderboard
from tools.seed import seed_catalogue

pytestmark = pytest.mark.integration


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


async def test_score_submission_is_server_derived_and_evidenced() -> None:
    database = Database(get_settings())
    player_id = uuid4()
    try:
        async with database.session_factory.begin() as session:
            await seed_catalogue(session)
            session.add(Player(id=player_id, display_name="Durable Score"))
        game_session, outcome = await _completed_skill_session(database, player_id)

        async with database.session_factory() as session:
            score, created = await services.submit_final_score(
                session, session_id=game_session.id, owner_id=player_id
            )
        async with database.session_factory() as session:
            retried, retry_created = await services.submit_final_score(
                session, session_id=game_session.id, owner_id=player_id
            )
            audit_count = await session.scalar(
                select(func.count())
                .select_from(AuditRecord)
                .where(
                    AuditRecord.entity_id == score.id,
                    AuditRecord.event_type == "final_score_submitted",
                )
            )
            event_count = await session.scalar(
                select(func.count())
                .select_from(AnalyticsEvent)
                .where(AnalyticsEvent.event_key == f"final_score_submitted:{score.id}")
            )

        assert created is True
        assert retry_created is False
        assert retried.id == score.id
        assert score.final_score == outcome.result["score"]
        assert score.completed_at == game_session.ended_at
        assert score.period_start.weekday() == 0
        assert audit_count == 1
        assert event_count == 1
    finally:
        await database.dispose()


async def test_concurrent_score_retry_creates_one_row() -> None:
    database = Database(get_settings())
    player_id = uuid4()
    try:
        async with database.session_factory.begin() as session:
            await seed_catalogue(session)
            session.add(Player(id=player_id, display_name="Concurrent Score"))
        game_session, _ = await _completed_skill_session(database, player_id)

        async def submit() -> tuple[FinalScore, bool]:
            async with database.session_factory() as session:
                return await services.submit_final_score(
                    session, session_id=game_session.id, owner_id=player_id
                )

        results = await asyncio.gather(submit(), submit())
        assert {result[0].id for result in results} == {results[0][0].id}
        assert sorted(result[1] for result in results) == [False, True]
        async with database.session_factory() as session:
            count = await session.scalar(
                select(func.count())
                .select_from(FinalScore)
                .where(FinalScore.session_id == game_session.id)
            )
        assert count == 1
    finally:
        await database.dispose()


async def test_rebuild_matches_postgresql_and_is_repeatable() -> None:
    database = Database(get_settings())
    redis = create_redis_client(get_settings())
    assert redis is not None
    player_id = uuid4()
    try:
        async with database.session_factory.begin() as session:
            await seed_catalogue(session)
            session.add(Player(id=player_id, display_name="Rebuild Score"))
        game_session, _ = await _completed_skill_session(database, player_id)
        async with database.session_factory() as session:
            score, _ = await services.submit_final_score(
                session, session_id=game_session.id, owner_id=player_id
            )
            game = await repositories.get_game(session, "skill_check")
            assert game is not None
            expected_count = await repositories.count_canonical_scores(
                session, game_id=game.id, period_start=score.period_start
            )
            await redis.delete(
                leaderboard_key("skill_check", score.period_start.strftime("%Y%m%dT%H%M%SZ"))
            )
            first = await rebuild_leaderboard(
                session, redis, game_key="skill_check", period_start=score.period_start
            )
            second = await rebuild_leaderboard(
                session, redis, game_key="skill_check", period_start=score.period_start
            )
        assert first == expected_count
        assert second == expected_count
    finally:
        await redis.aclose()
        await database.dispose()


async def test_api_projects_after_commit_and_falls_back_when_projection_is_missing() -> None:
    database = Database(get_settings())
    redis = create_redis_client(get_settings())
    assert redis is not None
    player_id = uuid4()
    try:
        async with database.session_factory.begin() as session:
            await seed_catalogue(session)
            session.add(Player(id=player_id, display_name="API Score"))
        game_session, _ = await _completed_skill_session(database, player_id)
        application = create_app(get_settings())

        async def override_session() -> AsyncIterator[AsyncSession]:
            async with database.session_factory() as session:
                yield session

        application.dependency_overrides[get_session] = override_session
        application.state.redis = redis
        headers = {"X-Player-ID": str(player_id)}
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application), base_url="http://test"
        ) as client:
            created = await client.post(
                "/api/v1/scores", headers=headers, json={"session_id": str(game_session.id)}
            )
            retried = await client.post(
                "/api/v1/scores", headers=headers, json={"session_id": str(game_session.id)}
            )
            assert created.status_code == 201
            assert retried.status_code == 200
            assert retried.json() == created.json()
            period_start = created.json()["period_start"]
            key = leaderboard_key(
                "skill_check",
                datetime.fromisoformat(period_start).strftime("%Y%m%dT%H%M%SZ"),
            )
            await redis.delete(key)
            fallback = await client.get(
                "/api/v1/leaderboards/skill_check",
                headers=headers,
                params={"period_start": period_start},
            )
            assert fallback.status_code == 200
            assert fallback.json()["source"] == "postgresql"
            assert any(
                item["score_id"] == created.json()["id"] for item in fallback.json()["items"]
            )

            async with database.session_factory() as session:
                await rebuild_leaderboard(
                    session,
                    redis,
                    game_key="skill_check",
                    period_start=datetime.fromisoformat(period_start).astimezone(UTC),
                )
            projected = await client.get(
                f"/api/v1/players/{player_id}/rank",
                headers=headers,
                params={"game_key": "skill_check", "period_start": period_start},
            )
            assert projected.status_code == 200
            assert projected.json()["source"] == "redis"
            assert projected.json()["entry"]["score_id"] == created.json()["id"]
    finally:
        await redis.aclose()
        await database.dispose()


async def test_closed_period_settlement_is_concurrent_and_reward_idempotent() -> None:
    database = Database(get_settings())
    player_id = uuid4()
    period_start = datetime(2026, 7, 6, tzinfo=UTC)
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
            session.add(
                RewardTierConfig(
                    id=uuid4(),
                    game_id=game.id,
                    version=1,
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
            target = leaderboard_key("skill_check", period_key)
            unrelated = leaderboard_key("daily_spin", period_key)
            await redis.zadd(target, {"corrupt-member": -1_000_000})
            await redis.zadd(unrelated, {"unrelated-member": -7})
            game = await repositories.get_game(session, "skill_check")
            assert game is not None
            expected = await repositories.list_canonical_scores(
                session, game_id=game.id, period_start=score.period_start
            )
            assert await rebuild_leaderboard(
                session, redis, game_key="skill_check", period_start=score.period_start
            ) == len(expected)

        assert await redis.zrange(target, 0, -1, withscores=True) == [
            (member, float(value)) for member, value in map(_mapping, expected)
        ]
        assert await redis.zrange(unrelated, 0, -1, withscores=True) == [("unrelated-member", -7.0)]
    finally:
        await redis.aclose()
        await database.dispose()


async def test_empty_and_concurrent_rebuilds_converge_safely() -> None:
    """Empty stale keys are removed and simultaneous rebuilds use isolated temps."""

    database = Database(get_settings())
    redis = create_redis_client(get_settings())
    assert redis is not None
    period_start = datetime(2100, 1, 4, tzinfo=UTC)
    target = leaderboard_key("skill_check", period_start.strftime("%Y%m%dT%H%M%SZ"))
    try:
        async with database.session_factory.begin() as session:
            await seed_catalogue(session)
        await redis.zadd(target, {"stale-member": -100})

        async def rebuild() -> int:
            async with database.session_factory() as session:
                return await rebuild_leaderboard(
                    session, redis, game_key="skill_check", period_start=period_start
                )

        assert sorted(await asyncio.gather(rebuild(), rebuild())) == [0, 0]
        assert await redis.exists(target) == 0
    finally:
        await redis.aclose()
        await database.dispose()
