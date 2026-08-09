"""HTTP and commit-timing contracts for live leaderboard invalidations."""

import asyncio
from collections.abc import AsyncGenerator, AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import uuid4

import httpx
import pytest
from fastapi import Request
from redis.exceptions import RedisError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import StreamingResponse

from app.api import leaderboards as leaderboard_api
from app.config import get_settings
from app.database import Database, get_session
from app.leaderboard_events import LeaderboardChange, LeaderboardEventHub
from app.main import create_app
from tools.seed import seed_catalogue

pytestmark = pytest.mark.api


class _ConnectedRequest:
    async def is_disconnected(self) -> bool:
        return False


async def test_stream_contract_initial_comment_event_and_privacy() -> None:
    period_start = datetime(2026, 8, 3, tzinfo=UTC)
    hub = LeaderboardEventHub()
    response = await leaderboard_api.leaderboard_events(
        "skill_check",
        cast(Request, _ConnectedRequest()),
        hub,
        period_start,
    )
    stream = cast(AsyncGenerator[bytes], response.body_iterator)

    assert isinstance(response, StreamingResponse)
    assert response.media_type == "text/event-stream"
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-accel-buffering"] == "no"
    assert await anext(stream) == b": connected\n\n"

    await hub.publish(LeaderboardChange("skill_check", period_start))
    event = await anext(stream)
    assert event.startswith(b"event: leaderboard-change\n")
    assert b'"game_key":"skill_check"' in event
    assert b'"period_start":"2026-08-03T00:00:00+00:00"' in event
    for sensitive_name in (
        b"player_id",
        b"display_name",
        b"final_score",
        b"outcome_id",
        b"seed",
        b"token",
    ):
        assert sensitive_name not in event

    await stream.aclose()
    assert hub.subscriber_count == 0


async def test_stream_emits_heartbeat(monkeypatch: pytest.MonkeyPatch) -> None:
    period_start = datetime(2026, 8, 3, tzinfo=UTC)
    hub = LeaderboardEventHub()
    monkeypatch.setattr(leaderboard_api, "_HEARTBEAT_SECONDS", 0.001)
    stream = cast(
        AsyncGenerator[bytes],
        leaderboard_api._leaderboard_event_stream(
            cast(Request, _ConnectedRequest()),
            hub,
            game_key="skill_check",
            period_start=period_start,
        ),
    )

    assert await anext(stream) == b": connected\n\n"
    assert await anext(stream) == b": heartbeat\n\n"
    await stream.aclose()


async def test_invalid_game_and_period_are_rejected_before_streaming() -> None:
    application = create_app(get_settings())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application), base_url="http://test"
    ) as client:
        invalid_game = await client.get(
            "/api/v1/leaderboards/not-a-game/events",
            params={"period_start": "2026-08-03T00:00:00Z"},
        )
        invalid_period = await client.get(
            "/api/v1/leaderboards/skill_check/events",
            params={"period_start": "2026-08-04T00:00:00Z"},
        )

    assert invalid_game.status_code == 422
    assert invalid_game.json() == {"error": {"code": "leaderboard_game_ineligible"}}
    assert invalid_period.status_code == 422
    assert invalid_period.json() == {"error": {"code": "invalid_play"}}


async def _complete_skill_check(
    client: httpx.AsyncClient,
) -> tuple[str, str]:
    player = await client.post("/api/v1/players", json={"display_name": "Live Board Player"})
    player_id = player.json()["id"]
    headers = {"X-Player-ID": player_id}
    created = await client.post(
        "/api/v1/sessions",
        headers=headers,
        json={
            "request_id": str(uuid4()),
            "player_id": player_id,
            "game_key": "skill_check",
        },
    )
    game_session = created.json()
    played = await client.post(
        f"/api/v1/sessions/{game_session['id']}/play",
        headers=headers,
        json={"actions": game_session["challenge"]["sequence"]},
    )
    assert played.status_code == 200
    return player_id, game_session["id"]


async def test_commit_publishes_once_and_redis_failure_keeps_postgres_readable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = Database(get_settings())
    try:
        async with database.session_factory.begin() as session:
            await seed_catalogue(session)
        application = create_app(get_settings())

        async def override_session() -> AsyncIterator[AsyncSession]:
            async with database.session_factory() as session:
                yield session

        async def failed_projection(*args: object) -> bool:
            del args
            raise RedisError("projection unavailable")

        application.dependency_overrides[get_session] = override_session
        monkeypatch.setattr(leaderboard_api, "project_final_score", failed_projection)
        hub = application.state.leaderboard_events
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application), base_url="http://test"
        ) as client:
            player_id, session_id = await _complete_skill_check(client)
            headers = {"X-Player-ID": player_id}
            period_start = datetime.now(UTC)
            period_start = period_start.replace(
                hour=0, minute=0, second=0, microsecond=0
            ) - timedelta(days=period_start.weekday())
            subscription = await hub.subscribe(game_key="skill_check", period_start=period_start)

            created = await client.post(
                "/api/v1/scores", headers=headers, json={"session_id": session_id}
            )
            retried = await client.post(
                "/api/v1/scores", headers=headers, json={"session_id": session_id}
            )
            event = await asyncio.wait_for(subscription.receive(), timeout=1)
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(subscription.receive(), timeout=0.05)
            board = await client.get(
                "/api/v1/leaderboards/skill_check",
                headers=headers,
                params={"period_start": created.json()["period_start"]},
            )

        assert created.status_code == 201
        assert retried.status_code == 200
        assert event == LeaderboardChange("skill_check", period_start)
        assert board.status_code == 200
        assert board.json()["source"] == "postgresql"
        assert any(item["score_id"] == created.json()["id"] for item in board.json()["items"])
    finally:
        await database.dispose()
