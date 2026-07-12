"""HTTP contracts for server-created sessions and ownership."""

from collections.abc import AsyncIterator
from uuid import uuid4

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import Database, get_session
from app.main import create_app
from app.models import Player
from tools.seed import seed_catalogue

pytestmark = pytest.mark.api


async def test_create_session_binds_server_config_and_enforces_owner() -> None:
    database = Database(get_settings())
    player_id = uuid4()
    try:
        async with database.session_factory.begin() as session:
            await seed_catalogue(session)
            session.add(Player(id=player_id, display_name="API Session Test"))

        application = create_app(get_settings())

        async def override_session() -> AsyncIterator[AsyncSession]:
            async with database.session_factory() as session:
                yield session

        application.dependency_overrides[get_session] = override_session
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application), base_url="http://test"
        ) as client:
            forbidden = await client.post(
                "/api/v1/sessions",
                headers={"X-Player-ID": str(uuid4())},
                json={
                    "request_id": str(uuid4()),
                    "player_id": str(player_id),
                    "game_key": "skill_check",
                },
            )
            request_id = uuid4()
            created = await client.post(
                "/api/v1/sessions",
                headers={"X-Player-ID": str(player_id)},
                json={
                    "request_id": str(request_id),
                    "player_id": str(player_id),
                    "game_key": "skill_check",
                },
            )
            retried = await client.post(
                "/api/v1/sessions",
                headers={"X-Player-ID": str(player_id)},
                json={
                    "request_id": str(request_id),
                    "player_id": str(player_id),
                    "game_key": "skill_check",
                },
            )
            forged = await client.post(
                f"/api/v1/sessions/{created.json()['id']}/play",
                headers={"X-Player-ID": str(player_id)},
                json={"score": 1000},
            )
            played = await client.post(
                f"/api/v1/sessions/{created.json()['id']}/play",
                headers={"X-Player-ID": str(player_id)},
                json={"actions": created.json()["challenge"]["sequence"]},
            )
            replayed = await client.post(
                f"/api/v1/sessions/{created.json()['id']}/play",
                headers={"X-Player-ID": str(player_id)},
                json={"actions": created.json()["challenge"]["sequence"]},
            )
            forged_claim = await client.post(
                f"/api/v1/sessions/{created.json()['id']}/claim",
                headers={"X-Player-ID": str(player_id)},
                json={"value": 999999},
            )
            wrong_owner_claim = await client.post(
                f"/api/v1/sessions/{created.json()['id']}/claim",
                headers={"X-Player-ID": str(uuid4())},
                json={},
            )
            claimed = await client.post(
                f"/api/v1/sessions/{created.json()['id']}/claim",
                headers={"X-Player-ID": str(player_id)},
                json={},
            )
            retried_claim = await client.post(
                f"/api/v1/sessions/{created.json()['id']}/claim",
                headers={"X-Player-ID": str(player_id)},
                json={},
            )
            rewards = await client.get(
                f"/api/v1/players/{player_id}/rewards",
                headers={"X-Player-ID": str(player_id)},
            )
            audit = await client.get(
                f"/api/v1/audit/outcomes/{played.json()['id']}",
                headers={"X-Player-ID": str(player_id)},
            )

        assert forbidden.status_code == 403
        assert forbidden.json() == {"error": {"code": "forbidden"}}
        assert created.status_code == 201
        assert created.json()["config_version_id"]
        assert retried.json()["id"] == created.json()["id"]
        assert forged.status_code == 422
        assert played.status_code == 200
        assert played.json()["result"]["score"] == 1000
        assert replayed.status_code == 409
        assert forged_claim.status_code == 422
        assert wrong_owner_claim.status_code == 403
        assert claimed.status_code == 200
        assert claimed.json()["value"] == 1000
        assert claimed.json()["status"] == "claimed"
        assert retried_claim.json() == claimed.json()
        assert rewards.json()["items"] == [claimed.json()]
        assert audit.status_code == 200
        assert audit.json()["evidence"]["session_id"] == created.json()["id"]
    finally:
        await database.dispose()
