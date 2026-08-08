"""Player-facing HTML and static-delivery contracts."""

from collections.abc import AsyncIterator
from typing import Any, cast
from uuid import uuid4

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.database import get_session as get_database_session
from app.main import create_app
from app.models import Game
from app.services import players as player_services
from app.web.router import validate_web_assets

pytestmark = pytest.mark.api


@pytest.fixture
def application(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> Any:
    async def catalogue(session: AsyncSession, limit: int, offset: int) -> list[Game]:
        del session, limit, offset
        return [
            Game(
                id=uuid4(),
                key="daily_spin",
                name="Daily <Spin>",
                description="A weighted reward spin.",
                is_active=True,
            ),
            Game(
                id=uuid4(),
                key="prediction_card",
                name="Prediction Card",
                description="Choose red or black.",
                is_active=True,
            ),
            Game(
                id=uuid4(),
                key="skill_check",
                name="Skill Check",
                description="Remember the sequence.",
                is_active=True,
            ),
            Game(
                id=uuid4(),
                key="inactive_game",
                name="Inactive",
                description="Not player-visible.",
                is_active=False,
            ),
        ]

    async def database_session() -> AsyncIterator[AsyncSession]:
        yield cast(AsyncSession, object())

    monkeypatch.setattr(player_services, "catalogue", catalogue)
    app = create_app(settings)
    app.dependency_overrides[get_database_session] = database_session
    return app


async def test_lobby_renders_catalogue_with_strict_browser_headers(application: Any) -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application), base_url="http://test"
    ) as client:
        response = await client.get("/")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["cross-origin-opener-policy"] == "same-origin"
    assert "default-src 'self'" in response.headers["content-security-policy"]
    assert "script-src 'self'" in response.headers["content-security-policy"]
    assert "unsafe-inline" not in response.headers["content-security-policy"]
    assert "unsafe-eval" not in response.headers["content-security-policy"]
    assert "Daily &lt;Spin&gt;" in response.text
    assert "Not player-visible" not in response.text
    assert "https://" not in response.text
    assert "alpine-csp-3.15.12.min.js" in response.text
    assert "sha384-MKLWq9B+" in response.text


async def test_game_cartridges_and_leaderboard_render(application: Any) -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application), base_url="http://test"
    ) as client:
        game = await client.get("/games/daily_spin")
        prediction = await client.get("/games/prediction_card")
        skill = await client.get("/games/skill_check")
        leaderboard = await client.get("/leaderboard")
        missing = await client.get("/games/unknown")

    assert game.status_code == 200
    assert 'x-data="dailySpin"' in game.text
    assert "Weighted Daily Spin reward wheel" in game.text
    assert 'x-data="predictionCard"' in prediction.text
    assert "What color will the server card be?" in prediction.text
    assert 'x-data="skillCheck"' in skill.text
    assert "Local timing does not increase or reduce it" in skill.text
    assert leaderboard.status_code == 200
    assert "Weekly leaderboard" in leaderboard.text
    assert missing.status_code == 404


async def test_static_assets_are_local_and_cache_deliberately(application: Any) -> None:
    browser_assets = (
        "core/state.js",
        "core/api-client.js",
        "core/player-store.js",
        "core/operation-journal.js",
        "core/clock.js",
        "components/shared.js",
        "games/daily-spin.js",
        "games/prediction-card.js",
        "games/skill-check.js",
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application), base_url="http://test"
    ) as client:
        stylesheet = await client.get("/static/css/app.css")
        alpine = await client.get("/static/vendor/alpine-csp-3.15.12.min.js")
        scripts = [await client.get(f"/static/js/{path}") for path in browser_assets]

    assert stylesheet.status_code == 200
    assert stylesheet.headers["content-type"].startswith("text/css")
    assert stylesheet.headers["cache-control"] == "public, max-age=3600"
    assert "--teal: #67f1cf" in stylesheet.text
    assert alpine.status_code == 200
    assert "javascript" in alpine.headers["content-type"]
    assert alpine.headers["cache-control"] == "public, max-age=31536000, immutable"
    assert len(alpine.content) == 61_522
    assert all(script.status_code == 200 for script in scripts)
    assert all("javascript" in script.headers["content-type"] for script in scripts)
    assert all(script.headers["cache-control"] == "public, max-age=3600" for script in scripts)
    assert "X-Player-ID" in scripts[1].text
    assert "clientSeed" in scripts[3].text
    assert "commitFairness" in scripts[6].text
    assert 'const gameKey = "daily_spin"' in scripts[6].text
    assert "authoritative_choice" in scripts[7].text
    assert '["create_session", "play"]' in scripts[7].text
    assert "submitFinalScore" in scripts[8].text
    assert "lockedSubmission" in scripts[8].text


async def test_player_csp_does_not_break_openapi_documentation(application: Any) -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application), base_url="http://test"
    ) as client:
        response = await client.get("/docs")

    assert response.status_code == 200
    assert "Content-Security-Policy" not in response.headers
    assert response.headers["x-frame-options"] == "DENY"


def test_required_web_assets_fail_fast_when_absent(tmp_path: Any) -> None:
    with pytest.raises(RuntimeError, match="required web assets are missing"):
        validate_web_assets(tmp_path)
