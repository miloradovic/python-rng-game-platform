"""Complete HTTP proof for every flagship game path."""

from collections.abc import AsyncIterator
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import Database, get_session
from app.main import create_app
from app.models import AnalyticsEvent, AuditRecord, Outcome, Reward
from tools.seed import seed_catalogue

pytestmark = pytest.mark.api


async def test_complete_mvp_flow_for_every_game() -> None:
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
            for game_key in ("daily_spin", "prediction_card", "skill_check"):
                player = await client.post(
                    "/api/v1/players", json={"display_name": f"MVP {game_key}"}
                )
                assert player.status_code == 201
                player_id = player.json()["id"]
                headers = {"X-Player-ID": player_id}

                config = await client.get(f"/api/v1/games/{game_key}/config")
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
                assert created.json()["config_version_id"] == config.json()["id"]

                if game_key == "prediction_card":
                    play_body = {"choice": "red"}
                elif game_key == "skill_check":
                    play_body = {"actions": created.json()["challenge"]["sequence"]}
                else:
                    play_body = {}
                played = await client.post(
                    f"/api/v1/sessions/{created.json()['id']}/play",
                    headers=headers,
                    json=play_body,
                )
                assert played.status_code == 200
                assert played.json()["config_version_id"] == config.json()["id"]

                claimed = await client.post(
                    f"/api/v1/sessions/{created.json()['id']}/claim",
                    headers=headers,
                    json={},
                )
                retried_claim = await client.post(
                    f"/api/v1/sessions/{created.json()['id']}/claim",
                    headers=headers,
                    json={},
                )
                assert claimed.status_code == 200
                assert retried_claim.json() == claimed.json()
                if game_key == "daily_spin":
                    values = {
                        band["key"]: band["value"] for band in config.json()["payload"]["rewards"]
                    }
                    assert claimed.json()["value"] == values[played.json()["result"]["reward_key"]]
                elif game_key == "prediction_card":
                    expected = (
                        config.json()["payload"]["correct_reward"]
                        if played.json()["result"]["correct"]
                        else 0
                    )
                    assert claimed.json()["value"] == expected
                else:
                    assert claimed.json()["value"] == played.json()["result"]["score"]

                audit = await client.get(
                    f"/api/v1/audit/outcomes/{played.json()['id']}", headers=headers
                )
                rewards = await client.get(f"/api/v1/players/{player_id}/rewards", headers=headers)
                summary = await client.get(
                    f"/api/v1/analytics/game-summary?game_key={game_key}", headers=headers
                )
                assert audit.status_code == 200
                assert audit.json()["evidence"]["config_version_id"] == config.json()["id"]
                assert rewards.json()["items"] == [claimed.json()]
                assert summary.json()["items"][0]["plays"] == 1
                assert summary.json()["items"][0]["rewards_issued"] == 1
                assert summary.json()["items"][0]["average_reward_value"] == claimed.json()["value"]

        async with database.session_factory() as session:
            outcome_count = await session.scalar(select(func.count()).select_from(Outcome))
            reward_count = await session.scalar(select(func.count()).select_from(Reward))
            audit_count = await session.scalar(select(func.count()).select_from(AuditRecord))
            event_count = await session.scalar(select(func.count()).select_from(AnalyticsEvent))
            assert outcome_count is not None
            assert reward_count is not None
            assert audit_count is not None
            assert event_count is not None
            assert outcome_count >= 3
            assert reward_count >= 3
            assert audit_count >= 9
            assert event_count >= 12
    finally:
        await database.dispose()
