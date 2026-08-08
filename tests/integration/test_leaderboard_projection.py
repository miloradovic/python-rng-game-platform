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
    SessionStatus,
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
            assert all("player_id" not in item for item in fallback.json()["items"])
            assert all(
                item["public_label"].startswith("Player-") for item in fallback.json()["items"]
            )
            assert any(item["is_current_player"] for item in fallback.json()["items"])
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


async def test_public_board_keeps_multiple_entries_for_one_safe_label() -> None:
    """Public recognition remains score-entry based, not unique-player settlement rank."""

    database = Database(get_settings())
    redis = create_redis_client(get_settings())
    assert redis is not None
    player_id = uuid4()
    period_start, _ = services.leaderboard_period(datetime.now(UTC))
    try:
        async with database.session_factory.begin() as session:
            await seed_catalogue(session)
            player = Player(id=player_id, display_name="Private Unmoderated Name")
            session.add(player)
            await session.flush()
            game = await repositories.get_game(session, "skill_check")
            assert game is not None
            config = await repositories.get_active_config(session, game.id)
            assert config is not None
            await _add_settlement_score(
                session,
                player_id=player_id,
                game_id=game.id,
                config_version_id=config.id,
                period_start=period_start,
                completed_at=period_start + timedelta(hours=1),
                final_score=900,
                session_id=uuid4(),
            )
            await _add_settlement_score(
                session,
                player_id=player_id,
                game_id=game.id,
                config_version_id=config.id,
                period_start=period_start,
                completed_at=period_start + timedelta(hours=2),
                final_score=700,
                session_id=uuid4(),
            )
            public_label = player.public_label

        application = create_app(get_settings())

        async def override_session() -> AsyncIterator[AsyncSession]:
            async with database.session_factory() as session:
                yield session

        application.dependency_overrides[get_session] = override_session
        application.state.redis = redis
        headers = {"X-Player-ID": str(player_id)}
        params = {"period_start": period_start.isoformat()}
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application), base_url="http://test"
        ) as client:
            canonical = await client.get(
                "/api/v1/leaderboards/skill_check", headers=headers, params=params
            )
            async with database.session_factory() as session:
                await rebuild_leaderboard(
                    session,
                    redis,
                    game_key="skill_check",
                    period_start=period_start,
                )
            projected = await client.get(
                "/api/v1/leaderboards/skill_check", headers=headers, params=params
            )

        assert canonical.status_code == 200
        current_entries = [
            entry for entry in canonical.json()["items"] if entry["public_label"] == public_label
        ]
        assert [entry["final_score"] for entry in current_entries] == [900, 700]
        assert all(entry["is_current_player"] for entry in current_entries)
        assert "Private Unmoderated Name" not in canonical.text
        assert str(player_id) not in canonical.text
        assert projected.json()["source"] == "redis"
        assert projected.json()["items"] == canonical.json()["items"]
    finally:
        await redis.aclose()
        await database.dispose()
