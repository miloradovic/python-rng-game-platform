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
    assert response.headers["cross-origin-resource-policy"] == "same-origin"
    assert "default-src 'self'" in response.headers["content-security-policy"]
    assert "script-src 'self'" in response.headers["content-security-policy"]
    assert "unsafe-inline" not in response.headers["content-security-policy"]
    assert "unsafe-eval" not in response.headers["content-security-policy"]
    assert "Hourly Spin" in response.text
    assert "Red or Black" in response.text
    assert "Memory Rush" in response.text
    assert "Daily &lt;Spin&gt;" not in response.text
    assert "A weighted reward spin." not in response.text
    assert "Not player-visible" not in response.text
    assert "Pick a game. <em>Chase the top spot.</em>" in response.text
    assert "Quick rounds, point rewards, and live weekly leaderboards." in response.text
    assert response.text.count('class="game-card-link"') == 3
    assert response.text.count("Play now") == 4
    assert "Every hour" in response.text
    assert "Every minute" in response.text
    assert "Every 30 seconds" in response.text
    assert "Spin for a shot at the top point prize." in response.text
    assert "Pick a color. Call it right and score." in response.text
    assert "Watch the numbers. Repeat the run. Beat the board." in response.text
    assert "Fair results" in response.text
    assert "Progress saved" in response.text
    assert "Free play only" in response.text
    assert "Built for proof" not in response.text
    assert "What makes it trustworthy" not in response.text
    assert ">Games</a>" in response.text
    assert ">Leaderboard</a>" not in response.text
    assert '<span class="demo-boundary">Free play</span>' in response.text
    assert '<span class="sr-only">Player selector:</span>' in response.text
    assert "Free to play. Points have no cash value. No purchases or betting." in response.text
    assert "server-authoritative, free-to-play" not in response.text
    assert "https://" not in response.text
    assert "alpine-csp-3.15.12.min.js" in response.text
    assert "sha384-MKLWq9B+" in response.text


async def test_game_cartridges_and_embedded_leaderboards_render(application: Any) -> None:
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
    assert "Hourly Spin point-prize wheel" in game.text
    assert "Available point prizes" in game.text
    assert "Your result" in game.text
    assert "Collect points" in game.text
    assert "Fairness details" in game.text
    assert 'x-show="resultVisible" x-cloak' in game.text
    assert 'x-show="isResult"' not in game.text
    assert 'id="daily-spin-segments"' in game.text
    assert '<template x-for="segment in segments"' in game.text
    assert '<svg id="daily-spin-wheel"' in game.text
    svg_markup = game.text.split('<svg id="daily-spin-wheel"', maxsplit=1)[1].split(
        "</svg>", maxsplit=1
    )[0]
    assert "<template" not in svg_markup
    assert "x-bind:style" not in game.text
    assert "style=" not in game.text
    assert 'x-data="predictionCard"' in prediction.text
    assert "Pick the card color" in prediction.text
    assert "The card" in prediction.text
    assert "Your choice is sent before the game returns the card" in prediction.text
    assert 'x-data="skillCheck"' in skill.text
    assert "Watch" in skill.text
    assert "Repeat" in skill.text
    assert "Score" in skill.text
    assert "Score my run" in skill.text
    assert "How scoring works" in skill.text
    assert "Input speed does not change it" in skill.text
    for response, game_key, component_name in (
        (game, "daily_spin", "dailySpin"),
        (prediction, "prediction_card", "predictionCard"),
        (skill, "skill_check", "skillCheck"),
    ):
        assert response.text.count('id="live-leaderboard"') == 1
        assert response.text.count('x-data="leaderboard"') == 1
        assert f'data-game-key="{game_key}"' in response.text
        assert 'data-period-start="' in response.text
        assert 'data-period-end="' in response.text
        assert response.text.index(f'x-data="{component_name}"') < response.text.index(
            'id="live-leaderboard"'
        )
        assert 'class="game-page-header"' in response.text
        assert 'class="cadence-chip"' in response.text
        assert "> All games</a>" in response.text
        assert 'aria-label="Top 10 weekly scores"' in response.text
        assert 'aria-live="polite" aria-atomic="true"' in response.text
        assert "Choose a player to join the chase" in response.text
        assert "Be first on the board" in response.text
    assert leaderboard.status_code == 200
    assert leaderboard.text.count('id="live-leaderboard"') == 1
    assert 'x-data="leaderboard"' in leaderboard.text
    assert 'data-game-key="skill_check"' in leaderboard.text
    assert "Top scores this week" in leaderboard.text
    assert missing.status_code == 404


async def test_static_assets_are_local_and_cache_deliberately(application: Any) -> None:
    browser_assets = (
        "core/state.js",
        "core/api-client.js",
        "core/player-store.js",
        "core/operation-journal.js",
        "core/clock.js",
        "components/shared.js",
        "components/leaderboard.js",
        "games/daily-spin.js",
        "games/prediction-card.js",
        "games/skill-check.js",
        "core/preferences.js",
        "core/final-score.js",
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application), base_url="http://test"
    ) as client:
        stylesheet = await client.get("/static/css/app.css")
        alpine = await client.get("/static/vendor/alpine-csp-3.15.12.min.js")
        scripts = [await client.get(f"/static/js/{path}") for path in browser_assets]
        game_page = await client.get("/games/daily_spin")

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
    assert "commitFairness" in scripts[7].text
    assert 'const gameKey = "daily_spin"' in scripts[7].text
    assert "wheel.animate(" in scripts[7].text
    assert "pendingOutcome: null" in scripts[7].text
    assert "pendingReward: null" in scripts[7].text
    assert "resultVisible: false" in scripts[7].text
    assert "get isResult()" not in scripts[7].text
    assert "await this.wheelAnimation.finished" in scripts[7].text
    assert "await this.settledAnimation.finished" in scripts[7].text
    assert "new Promise((resolve) => window.setTimeout" not in scripts[7].text
    assert "window.ArcadeProof.preferences.reducedMotion()" in scripts[7].text
    assert "this.cancelPresentation()" in scripts[7].text
    assert 'window.removeEventListener("arcade-proof:player"' in scripts[7].text
    assert "Spin now" in scripts[7].text
    assert "Resume spin" in scripts[7].text
    assert "String(segment.value)" in scripts[7].text
    assert "Commit & spin" not in scripts[7].text
    assert "server-authoritative" not in scripts[7].text
    assert 'document.createElementNS(svgNamespace, "g")' in scripts[7].text
    assert "container.replaceChildren(...groups)" in scripts[7].text
    assert "renderWheelSegments(this.segments)" in scripts[7].text
    assert "wheelStyle" not in scripts[7].text
    assert "authoritative_choice" in scripts[8].text
    assert "the card was" in scripts[8].text
    assert "points collected" in scripts[8].text
    assert '["create_session", "play"]' in scripts[8].text
    assert "await this.recover();" in scripts[8].text
    assert 'error.code === "invalid_transition"' in scripts[8].text
    assert 'this.session.status === "active"' in scripts[8].text
    assert scripts[8].text.count("if (this.busy) return;") >= 2
    assert "submitFinalScore" in scripts[9].text
    assert "lockedSubmission" in scripts[9].text
    assert "watchState" in scripts[9].text
    assert "repeatState" in scripts[9].text
    assert "scoreState" in scripts[9].text
    assert "Score returned by the server" not in scripts[9].text
    assert "getPlayerRank" in scripts[6].text
    assert "pageSize = 10" in scripts[6].text
    assert "playerStore.validateCurrent()" in scripts[6].text
    assert "if (this.initialized) this.scheduleRefresh();" in scripts[6].text
    assert "new EventSource(" in scripts[6].text
    assert 'addEventListener("leaderboard-change"' in scripts[6].text
    assert "pollIntervalMs = 15000" in scripts[6].text
    assert "visibilitychange" in scripts[6].text
    assert "requestGeneration" in scripts[6].text
    assert "snapshotRowPositions" in scripts[6].text
    assert "getBoundingClientRect()" in scripts[6].text
    assert "animateChanges" in scripts[6].text
    assert "row.animate(" in scripts[6].text
    assert "preferences.reducedMotion()" in scripts[6].text
    assert "describeChange" in scripts[6].text
    assert "startCurrentPeriod" in scripts[6].text
    assert "nextPage" not in scripts[6].text
    assert "validationPromise" in scripts[2].text
    assert "arcade-proof.preferences.v1" in scripts[10].text
    assert "sound: false" in scripts[10].text
    assert "prefers-reduced-motion" in scripts[10].text
    assert "submitOrRecover" in scripts[11].text
    assert "state.session?.final_score" in scripts[11].text
    assert game_page.text.index("core/final-score.js") < game_page.text.index("games/daily-spin.js")
    daily_animation = scripts[7].text.index("await this.animateResult(")
    daily_score = scripts[7].text.index("finalScores.submitOrRecover", daily_animation)
    assert daily_animation < daily_score
    action_body = (
        scripts[7]
        .text.split("async action()", maxsplit=1)[1]
        .split("segmentForPendingOutcome()", maxsplit=1)[0]
    )
    assert "this.pendingOutcome = evaluated.outcome" in action_body
    assert "this.outcome = evaluated.outcome" not in action_body
    animation_method = scripts[7].text.index("async animateResult(generation)")
    first_finish = scripts[7].text.index("await this.wheelAnimation.finished", animation_method)
    settled_finish = scripts[7].text.index("await this.settledAnimation.finished", first_finish)
    normal_reveal = scripts[7].text.index("this.revealPendingResult()", settled_finish)
    assert settled_finish < normal_reveal
    assert "this.outcome = this.pendingOutcome" in scripts[7].text
    assert "this.targetRotation(segment, 6)" in scripts[7].text
    assert "this.targetRotation(segment)" in scripts[7].text
    card_reveal = scripts[8].text.index("this.presentResult(true)")
    assert card_reveal < scripts[8].text.index("finalScores.submitOrRecover", card_reveal)


async def test_accessibility_preferences_and_api_cache_isolation(application: Any) -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application), base_url="http://test"
    ) as client:
        lobby = await client.get("/")
        skill = await client.get("/games/skill_check")
        api = await client.get("/api/v1")

    assert 'id="preferences-dialog"' in lobby.text
    assert "Enable supplementary sounds" in lobby.text
    assert 'aria-describedby="display-name-error"' in lobby.text
    assert 'aria-haspopup="dialog"' in lobby.text
    assert "number keys" in skill.text
    assert 'id="skill-result-title" tabindex="-1"' in skill.text
    assert api.status_code == 200
    assert api.headers["cache-control"] == "no-store"
    assert api.headers["cross-origin-resource-policy"] == "same-origin"


async def test_player_csp_does_not_break_openapi_documentation(application: Any) -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application), base_url="http://test"
    ) as client:
        response = await client.get("/docs")

    assert response.status_code == 200
    assert "Content-Security-Policy" not in response.headers
    assert response.headers["x-frame-options"] == "DENY"


async def test_openapi_describes_frontend_recovery_and_public_identity(application: Any) -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application), base_url="http://test"
    ) as client:
        response = await client.get("/openapi.json")

    assert response.status_code == 200
    paths = response.json()["paths"]
    create_description = paths["/api/v1/players"]["post"]["description"]
    recovery_description = paths["/api/v1/players/{player_id}/game-state"]["get"]["description"]
    leaderboard_description = paths["/api/v1/leaderboards/{game_key}"]["get"]["description"]
    assert "public_label is generated by the server" in create_description
    assert "PostgreSQL-authoritative recovery state" in recovery_description
    assert "must match X-Player-ID" in recovery_description
    assert "never the player UUID or private display name" in leaderboard_description


def test_required_web_assets_fail_fast_when_absent(tmp_path: Any) -> None:
    with pytest.raises(RuntimeError, match="required web assets are missing"):
        validate_web_assets(tmp_path)
