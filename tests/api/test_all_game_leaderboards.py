"""HTTP contracts for server-derived scores across every seeded game."""

import asyncio
from collections.abc import AsyncIterator
from datetime import datetime
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import repositories, services
from app.config import get_settings
from app.database import Database, get_session
from app.main import create_app
from app.models import FinalScore
from tools.seed import seed_catalogue

pytestmark = pytest.mark.api


async def _complete_game(
    client: httpx.AsyncClient, game_key: str
) -> tuple[dict[str, object], str, int]:
    player = await client.post("/api/v1/players", json={"display_name": f"{game_key} Player"})
    player_id = player.json()["id"]
    headers = {"X-Player-ID": player_id}
    created = await client.post(
        "/api/v1/sessions",
        headers=headers,
        json={
            "request_id": str(uuid4()),
            "player_id": player_id,
            "game_key": game_key,
        },
    )
    assert created.status_code == 201
    game_session = created.json()
    if game_key == "daily_spin":
        commitment = await client.post(
            "/api/v1/fairness/commit",
            headers=headers,
            json={"session_id": game_session["id"]},
        )
        result = await client.post(
            "/api/v1/fairness/evaluate",
            headers=headers,
            json={"proof_id": commitment.json()["proof_id"], "client_seed": "score-test"},
        )
        expected_score = result.json()["reward"]["value"]
    else:
        intent = (
            {"choice": "red"}
            if game_key == "prediction_card"
            else {"actions": game_session["challenge"]["sequence"]}
        )
        result = await client.post(
            f"/api/v1/sessions/{game_session['id']}/play",
            headers=headers,
            json=intent,
        )
        outcome_result = result.json()["result"]
        expected_score = (
            (25 if outcome_result.get("correct") is True else 0)
            if game_key == "prediction_card"
            else outcome_result["score"]
        )
    return game_session, player_id, expected_score


@pytest.mark.parametrize("game_key", ["daily_spin", "prediction_card", "skill_check"])
async def test_submit_retry_read_and_rank_for_every_game(game_key: str) -> None:
    database = Database(get_settings())
    try:
        async with database.session_factory.begin() as session:
            await seed_catalogue(session)
        application = create_app(get_settings())

        async def override_session() -> AsyncIterator[AsyncSession]:
            async with database.session_factory() as session:
                yield session

        application.dependency_overrides[get_session] = override_session
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application), base_url="http://test"
        ) as client:
            game_session, player_id, expected_score = await _complete_game(client, game_key)
            headers = {"X-Player-ID": player_id}
            submitted = await client.post(
                "/api/v1/scores",
                headers=headers,
                json={"session_id": game_session["id"]},
            )
            retried = await client.post(
                "/api/v1/scores",
                headers=headers,
                json={"session_id": game_session["id"]},
            )
            assert submitted.status_code == 201
            assert retried.status_code == 200
            assert retried.json() == submitted.json()
            assert submitted.json()["final_score"] == expected_score

            period_start = submitted.json()["period_start"]
            board = await client.get(
                f"/api/v1/leaderboards/{game_key}",
                headers=headers,
                params={"period_start": period_start},
            )
            rank = await client.get(
                f"/api/v1/players/{player_id}/rank",
                headers=headers,
                params={"game_key": game_key, "period_start": period_start},
            )

        assert board.status_code == 200
        assert board.json()["game_key"] == game_key
        assert rank.status_code == 200
        assert rank.json()["entry"]["score_id"] == submitted.json()["id"]
        board_entry = next(
            item for item in board.json()["items"] if item["score_id"] == submitted.json()["id"]
        )
        assert board_entry == rank.json()["entry"]
    finally:
        await database.dispose()


@pytest.mark.parametrize("game_key", ["daily_spin", "prediction_card", "skill_check"])
async def test_concurrent_duplicate_score_advances_one_revision(game_key: str) -> None:
    database = Database(get_settings())
    try:
        async with database.session_factory.begin() as session:
            await seed_catalogue(session)
        application = create_app(get_settings())

        async def override_session() -> AsyncIterator[AsyncSession]:
            async with database.session_factory() as session:
                yield session

        application.dependency_overrides[get_session] = override_session
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application), base_url="http://test"
        ) as client:
            game_session, player_id, _ = await _complete_game(client, game_key)
            headers = {"X-Player-ID": player_id}
            state = await client.get(
                f"/api/v1/players/{player_id}/game-state",
                headers=headers,
                params={"game_key": game_key},
            )
            period_start, _ = services.leaderboard_period(
                datetime.fromisoformat(state.json()["session"]["ended_at"])
            )
            async with database.session_factory() as session:
                game = await repositories.get_game(session, game_key)
                assert game is not None
                initial_revision = await repositories.leaderboard_projection_revision(
                    session, game_id=game.id, period_start=period_start
                )
            first, second = await asyncio.gather(
                client.post(
                    "/api/v1/scores",
                    headers=headers,
                    json={"session_id": game_session["id"]},
                ),
                client.post(
                    "/api/v1/scores",
                    headers=headers,
                    json={"session_id": game_session["id"]},
                ),
            )
        assert sorted((first.status_code, second.status_code)) == [200, 201]
        assert first.json()["id"] == second.json()["id"]
        async with database.session_factory() as session:
            game = await repositories.get_game(session, game_key)
            assert game is not None
            score = await session.get(FinalScore, UUID(first.json()["id"]))
            assert score is not None
            count = await session.scalar(
                select(func.count())
                .select_from(FinalScore)
                .where(FinalScore.session_id == game_session["id"])
            )
            revision = await repositories.leaderboard_projection_revision(
                session,
                game_id=game.id,
                period_start=score.period_start,
            )
        assert count == 1
        assert revision == initial_revision + 1
    finally:
        await database.dispose()
