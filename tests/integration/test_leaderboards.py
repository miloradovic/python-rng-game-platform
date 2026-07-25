"""PostgreSQL-authoritative scores and disposable Redis leaderboard integration."""

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import UUID, uuid4

import httpx
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
            projection_revision = await repositories.leaderboard_projection_revision(
                session, game_id=score.game_id, period_start=score.period_start
            )
            period_score_count = await repositories.count_canonical_scores(
                session, game_id=score.game_id, period_start=score.period_start
            )

        assert created is True
        assert retry_created is False
        assert retried.id == score.id
        assert score.final_score == outcome.result["score"]
        assert score.completed_at == game_session.ended_at
        assert score.period_start.weekday() == 0
        assert audit_count == 1
        assert event_count == 1
        assert projection_revision == period_score_count
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
                leaderboard_pointer_key(
                    "skill_check", score.period_start.strftime("%Y%m%dT%H%M%SZ")
                )
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


async def test_api_projection_hit_skips_canonical_query_and_corruption_falls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
            period = datetime.fromisoformat(period_start).astimezone(UTC)
            keys = await _current_projection_keys(redis, "skill_check", period)

            async def reject_canonical_query(*args: object, **kwargs: object) -> object:
                del args, kwargs
                raise AssertionError("canonical PostgreSQL page query executed on Redis hit")

            with monkeypatch.context() as projection_spy:
                projection_spy.setattr(
                    repositories, "list_canonical_scores", reject_canonical_query
                )
                projected_page = await client.get(
                    "/api/v1/leaderboards/skill_check",
                    headers=headers,
                    params={"period_start": period_start},
                )
            assert projected_page.status_code == 200
            assert projected_page.json()["source"] == "redis"
            assert projected_page.json()["items"] == fallback.json()["items"]
            assert projected_page.json()["next_cursor"] == fallback.json()["next_cursor"]
            projected = await client.get(
                f"/api/v1/players/{player_id}/rank",
                headers=headers,
                params={"game_key": "skill_check", "period_start": period_start},
            )
            assert projected.status_code == 200
            assert projected.json()["source"] == "redis"
            assert projected.json()["entry"]["score_id"] == created.json()["id"]

            # A score mutation preserves cardinality but invalidates member integrity.
            projected_rows = cast(
                list[tuple[str, float]],
                await redis.zrange(keys.members, 0, 0, withscores=True),
            )
            member, redis_score = projected_rows[0]
            await redis.zadd(keys.members, {member: redis_score - 1})
            corrupted = await client.get(
                "/api/v1/leaderboards/skill_check",
                headers=headers,
                params={"period_start": period_start},
            )
            assert corrupted.status_code == 200
            assert corrupted.json()["source"] == "postgresql"
            assert any(
                item["score_id"] == created.json()["id"] for item in corrupted.json()["items"]
            )

            async with database.session_factory() as session:
                await rebuild_leaderboard(
                    session, redis, game_key="skill_check", period_start=period
                )
            keys = await _current_projection_keys(redis, "skill_check", period)
            projected_rows = cast(
                list[tuple[str, float]],
                await redis.zrange(keys.members, 0, 0, withscores=True),
            )
            member, redis_score = projected_rows[0]
            await redis.zrem(keys.members, member)
            await redis.zadd(
                keys.members,
                {"corrupt:member:with:same-cardinality": redis_score},
            )
            replaced = await client.get(
                "/api/v1/leaderboards/skill_check",
                headers=headers,
                params={"period_start": period_start},
            )
            assert replaced.json()["source"] == "postgresql"

            async with database.session_factory() as session:
                await rebuild_leaderboard(
                    session, redis, game_key="skill_check", period_start=period
                )
            keys = await _current_projection_keys(redis, "skill_check", period)
            await redis.hset(keys.metadata, "signature", "0" * 64)
            invalid_metadata = await client.get(
                "/api/v1/leaderboards/skill_check",
                headers=headers,
                params={"period_start": period_start},
            )
            assert invalid_metadata.json()["source"] == "postgresql"

            async with database.session_factory() as session:
                await rebuild_leaderboard(
                    session, redis, game_key="skill_check", period_start=period
                )
            keys = await _current_projection_keys(redis, "skill_check", period)
            await redis.zadd(keys.members, {"extra:corrupt:member:value": -1})
            extra = await client.get(
                "/api/v1/leaderboards/skill_check",
                headers=headers,
                params={"period_start": period_start},
            )
            assert extra.status_code == 200
            assert extra.json()["source"] == "postgresql"

            async with database.session_factory() as session:
                await rebuild_leaderboard(
                    session, redis, game_key="skill_check", period_start=period
                )
            keys = await _current_projection_keys(redis, "skill_check", period)
            member = cast(list[str], await redis.zrange(keys.members, 0, 0))[0]
            await redis.zrem(keys.members, member)
            missing = await client.get(
                "/api/v1/leaderboards/skill_check",
                headers=headers,
                params={"period_start": period_start},
            )
            assert missing.json()["source"] == "postgresql"

            async with database.session_factory() as session:
                await rebuild_leaderboard(
                    session, redis, game_key="skill_check", period_start=period
                )
            keys = await _current_projection_keys(redis, "skill_check", period)
            await redis.hset(keys.players, str(player_id), "malformed-player-member")
            corrupted_rank = await client.get(
                f"/api/v1/players/{player_id}/rank",
                headers=headers,
                params={"game_key": "skill_check", "period_start": period_start},
            )
            assert corrupted_rank.json()["source"] == "postgresql"

            await redis.set(
                leaderboard_pointer_key("skill_check", period.strftime("%Y%m%dT%H%M%SZ")),
                str(uuid4()),
            )
            stale_generation = await client.get(
                "/api/v1/leaderboards/skill_check",
                headers=headers,
                params={"period_start": period_start},
            )
            assert stale_generation.json()["source"] == "postgresql"
    finally:
        await redis.aclose()
        await database.dispose()


async def test_projection_page_boundaries_match_postgresql() -> None:
    """First, middle, partial, and empty pages are identical on a Redis hit."""

    database = Database(get_settings())
    redis = create_redis_client(get_settings())
    assert redis is not None
    player_ids = [uuid4() for _ in range(5)]
    try:
        async with database.session_factory.begin() as session:
            await seed_catalogue(session)
            session.add_all(
                [
                    Player(id=player_id, display_name=f"Page {index}")
                    for index, player_id in enumerate(player_ids)
                ]
            )
        score_period: datetime | None = None
        for player_id in player_ids:
            game_session, _ = await _completed_skill_session(database, player_id)
            async with database.session_factory() as session:
                score, _ = await services.submit_final_score(
                    session, session_id=game_session.id, owner_id=player_id
                )
                score_period = score.period_start
        assert score_period is not None
        application = create_app(get_settings())

        async def override_session() -> AsyncIterator[AsyncSession]:
            async with database.session_factory() as session:
                yield session

        application.dependency_overrides[get_session] = override_session
        application.state.redis = redis
        headers = {"X-Player-ID": str(player_ids[0])}
        offsets = (0, 2, 4, 10_000)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application), base_url="http://test"
        ) as client:
            canonical = [
                await client.get(
                    "/api/v1/leaderboards/skill_check",
                    headers=headers,
                    params={
                        "period_start": score_period.isoformat(),
                        "cursor": offset,
                        "limit": 2,
                    },
                )
                for offset in offsets
            ]
            assert all(response.json()["source"] == "postgresql" for response in canonical)
            async with database.session_factory() as session:
                await rebuild_leaderboard(
                    session,
                    redis,
                    game_key="skill_check",
                    period_start=score_period,
                )
            projected = [
                await client.get(
                    "/api/v1/leaderboards/skill_check",
                    headers=headers,
                    params={
                        "period_start": score_period.isoformat(),
                        "cursor": offset,
                        "limit": 2,
                    },
                )
                for offset in offsets
            ]

        assert all(response.json()["source"] == "redis" for response in projected)
        assert [response.json()["items"] for response in projected] == [
            response.json()["items"] for response in canonical
        ]
        assert projected[-1].json()["items"] == []
    finally:
        await redis.aclose()
        await database.dispose()


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


async def test_empty_and_concurrent_rebuilds_converge_safely() -> None:
    """Empty stale keys are removed and simultaneous rebuilds use isolated temps."""

    database = Database(get_settings())
    redis = create_redis_client(get_settings())
    assert redis is not None
    period_start = datetime(2100, 1, 4, tzinfo=UTC)
    try:
        async with database.session_factory.begin() as session:
            await seed_catalogue(session)

        async def rebuild() -> int:
            async with database.session_factory() as session:
                return await rebuild_leaderboard(
                    session, redis, game_key="skill_check", period_start=period_start
                )

        assert sorted(await asyncio.gather(rebuild(), rebuild())) == [0, 0]
        keys = await _current_projection_keys(redis, "skill_check", period_start)
        assert await redis.zcard(keys.members) == 0
        assert await redis.exists(keys.metadata) == 1
    finally:
        await redis.aclose()
        await database.dispose()


async def test_concurrent_rebuild_score_commit_and_read_preserve_canonical_results() -> None:
    """A racing generation is either validated or ignored, never partially trusted."""

    database = Database(get_settings())
    redis = create_redis_client(get_settings())
    assert redis is not None
    first_player, second_player = uuid4(), uuid4()
    try:
        async with database.session_factory.begin() as session:
            await seed_catalogue(session)
            session.add_all(
                [
                    Player(id=first_player, display_name="Concurrent Reader"),
                    Player(id=second_player, display_name="Concurrent Writer"),
                ]
            )
        first_session, _ = await _completed_skill_session(database, first_player)
        second_session, _ = await _completed_skill_session(database, second_player)
        async with database.session_factory() as session:
            first_score, _ = await services.submit_final_score(
                session, session_id=first_session.id, owner_id=first_player
            )
            await rebuild_leaderboard(
                session,
                redis,
                game_key="skill_check",
                period_start=first_score.period_start,
            )

        application = create_app(get_settings())

        async def override_session() -> AsyncIterator[AsyncSession]:
            async with database.session_factory() as session:
                yield session

        application.dependency_overrides[get_session] = override_session
        application.state.redis = redis
        period = first_score.period_start.isoformat()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application), base_url="http://test"
        ) as client:

            async def rebuild() -> int:
                async with database.session_factory() as session:
                    return await rebuild_leaderboard(
                        session,
                        redis,
                        game_key="skill_check",
                        period_start=first_score.period_start,
                    )

            submit, _, racing_read = await asyncio.gather(
                client.post(
                    "/api/v1/scores",
                    headers={"X-Player-ID": str(second_player)},
                    json={"session_id": str(second_session.id)},
                ),
                rebuild(),
                client.get(
                    "/api/v1/leaderboards/skill_check",
                    headers={"X-Player-ID": str(first_player)},
                    params={"period_start": period},
                ),
            )
            final_read = await client.get(
                "/api/v1/leaderboards/skill_check",
                headers={"X-Player-ID": str(first_player)},
                params={"period_start": period},
            )

        assert submit.status_code == 201
        assert racing_read.status_code == 200
        assert final_read.status_code == 200
        durable_ids = {first_score.id, UUID(submit.json()["id"])}
        returned_ids = {UUID(item["score_id"]) for item in final_read.json()["items"]}
        assert durable_ids <= returned_ids
    finally:
        await redis.aclose()
        await database.dispose()
