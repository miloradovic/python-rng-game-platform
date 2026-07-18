"""HTTP integration coverage for commitment/reveal evaluation."""

from collections.abc import AsyncIterator
from uuid import uuid4

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import Database, get_session
from app.main import create_app
from tools.seed import seed_catalogue

pytestmark = pytest.mark.api


async def test_daily_spin_commit_and_evaluate_are_authorized_and_idempotent() -> None:
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
            player = await client.post("/api/v1/players", json={"display_name": "Fair Player"})
            other = await client.post("/api/v1/players", json={"display_name": "Other Player"})
            player_id = player.json()["id"]
            created = await client.post(
                "/api/v1/sessions",
                headers={"X-Player-ID": player_id},
                json={
                    "request_id": str(uuid4()),
                    "player_id": player_id,
                    "game_key": "daily_spin",
                },
            )
            session_id = created.json()["id"]
            forbidden = await client.post(
                "/api/v1/fairness/commit",
                headers={"X-Player-ID": other.json()["id"]},
                json={"session_id": session_id},
            )
            assert forbidden.status_code == 403

            headers = {"X-Player-ID": player_id}
            committed = await client.post(
                "/api/v1/fairness/commit", headers=headers, json={"session_id": session_id}
            )
            retried_commit = await client.post(
                "/api/v1/fairness/commit", headers=headers, json={"session_id": session_id}
            )
            assert committed.status_code == 201
            assert retried_commit.json() == committed.json()
            assert committed.json()["status"] == "committed"
            assert "server_seed" not in committed.json()

            evaluated = await client.post(
                "/api/v1/fairness/evaluate",
                headers=headers,
                json={"proof_id": committed.json()["proof_id"], "client_seed": "api-client-seed"},
            )
            retried_evaluation = await client.post(
                "/api/v1/fairness/evaluate",
                headers=headers,
                json={"proof_id": committed.json()["proof_id"], "client_seed": "changed-seed"},
            )
            assert evaluated.status_code == 200
            assert retried_evaluation.json() == evaluated.json()
            assert evaluated.json()["status"] == "revealed"
            assert evaluated.json()["outcome"]["result"]["reward_key"]
            assert "server_seed" not in evaluated.json()
    finally:
        await database.dispose()
