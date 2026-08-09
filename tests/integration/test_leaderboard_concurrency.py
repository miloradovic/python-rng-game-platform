"""PostgreSQL-authoritative scores and disposable Redis leaderboard integration."""

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import httpx
import pytest
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app import services
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
            first_score, _, _ = await services.submit_final_score(
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
