"""Abuse-oriented HTTP contracts for leaderboard scoring and settlement."""

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import Database, get_session
from app.main import create_app
from app.models import SettlementRun
from tools.seed import seed_catalogue

pytestmark = pytest.mark.api


async def test_leaderboard_rejects_forged_scores_and_unauthorized_settlement() -> None:
    """HTTP clients cannot choose scores, tiers, periods, or settlement authority."""

    settings = get_settings()
    database = Database(settings)
    try:
        async with database.session_factory.begin() as session:
            await seed_catalogue(session)

        application = create_app(settings)

        async def override_session() -> AsyncIterator[AsyncSession]:
            async with database.session_factory() as session:
                yield session

        application.dependency_overrides[get_session] = override_session
        token = settings.settlement_admin_token
        assert token is not None
        valid_headers = {"X-Settlement-Token": token.get_secret_value()}
        closed_period = datetime(2020, 1, 6, tzinfo=UTC).isoformat()
        open_period = datetime(2100, 1, 4, tzinfo=UTC).isoformat()
        async with database.session_factory() as session:
            runs_before = await session.scalar(select(func.count()).select_from(SettlementRun))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application), base_url="http://test"
        ) as client:
            forged_score = await client.post(
                "/api/v1/scores",
                headers={"X-Player-ID": str(uuid4())},
                json={"session_id": str(uuid4()), "final_score": 1_000_000},
            )
            unauthorized = await client.post(
                "/api/v1/leaderboards/skill_check/settle",
                params={"period_start": closed_period},
            )
            premature = await client.post(
                "/api/v1/leaderboards/skill_check/settle",
                headers=valid_headers,
                params={"period_start": open_period},
            )
            ineligible = await client.post(
                "/api/v1/leaderboards/daily_spin/settle",
                headers=valid_headers,
                params={"period_start": closed_period},
            )
        async with database.session_factory() as session:
            runs_after = await session.scalar(select(func.count()).select_from(SettlementRun))

        assert forged_score.status_code == 422
        assert unauthorized.status_code == 403
        assert unauthorized.json() == {"error": {"code": "settlement_forbidden"}}
        assert premature.status_code == 409
        assert premature.json() == {"error": {"code": "leaderboard_period_open"}}
        assert ineligible.status_code == 422
        assert ineligible.json() == {"error": {"code": "leaderboard_game_ineligible"}}
        assert runs_after == runs_before
    finally:
        await database.dispose()
