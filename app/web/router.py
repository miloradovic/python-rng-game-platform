"""Server-rendered player page routes and repository-owned static assets."""

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_session as get_database_session
from app.models import Game
from app.services import players as player_services
from app.services.leaderboards import leaderboard_period

WEB_ROOT = Path(__file__).resolve().parent
TEMPLATE_ROOT = WEB_ROOT / "templates"
STATIC_ROOT = WEB_ROOT / "static"
REQUIRED_WEB_ASSETS = (
    Path("templates/base.html"),
    Path("templates/lobby.html"),
    Path("templates/game.html"),
    Path("templates/leaderboard.html"),
    Path("templates/components/game_card.html"),
    Path("templates/components/shared_feedback.html"),
    Path("templates/games/daily_spin.html"),
    Path("templates/games/prediction_card.html"),
    Path("templates/games/skill_check.html"),
    Path("static/css/app.css"),
    Path("static/js/core/api-client.js"),
    Path("static/js/core/clock.js"),
    Path("static/js/core/operation-journal.js"),
    Path("static/js/core/player-store.js"),
    Path("static/js/core/state.js"),
    Path("static/js/components/shared.js"),
    Path("static/js/components/leaderboard.js"),
    Path("static/js/games/daily-spin.js"),
    Path("static/js/games/prediction-card.js"),
    Path("static/js/games/skill-check.js"),
    Path("static/js/app.js"),
    Path("static/vendor/alpine-csp-3.15.12.min.js"),
)

router = APIRouter(include_in_schema=False)
templates = Jinja2Templates(directory=TEMPLATE_ROOT)
Session = Annotated[AsyncSession, Depends(get_database_session)]


@dataclass(frozen=True)
class GameCard:
    """Safe presentation metadata layered over the public catalogue."""

    key: str
    name: str
    description: str
    accent: str
    eyebrow: str


_GAME_PRESENTATION: dict[str, tuple[str, str]] = {
    "daily_spin": ("spin", "Commit. Spin. Verify."),
    "prediction_card": ("prediction", "Choose red or black."),
    "skill_check": ("skill", "Remember the sequence."),
}


def validate_web_assets(root: Path = WEB_ROOT) -> None:
    """Fail application construction when a required web asset is absent."""

    missing = [str(path) for path in REQUIRED_WEB_ASSETS if not (root / path).is_file()]
    if missing:
        joined = ", ".join(missing)
        raise RuntimeError(f"required web assets are missing: {joined}")


def _game_card(game: Game) -> GameCard:
    accent, eyebrow = _GAME_PRESENTATION.get(game.key, ("default", "Server-authoritative play."))
    return GameCard(
        key=game.key,
        name=game.name,
        description=game.description,
        accent=accent,
        eyebrow=eyebrow,
    )


async def _game_cards(session: AsyncSession) -> list[GameCard]:
    games = await player_services.catalogue(session, limit=100, offset=0)
    return [_game_card(game) for game in games if game.is_active]


def _context(request: Request, **values: object) -> dict[str, object]:
    return {"request": request, **values}


@router.get("/", response_class=HTMLResponse, name="lobby")
async def lobby(request: Request, session: Session) -> HTMLResponse:
    """Render the active public catalogue as the player lobby."""

    games = await _game_cards(session)
    return templates.TemplateResponse(
        request=request,
        name="lobby.html",
        context=_context(request, page="lobby", games=games),
    )


@router.get("/games/{game_key}", response_class=HTMLResponse, name="game_page")
async def game_page(game_key: str, request: Request, session: Session) -> HTMLResponse:
    """Render one server-authoritative game cartridge in the shared shell."""

    games = await _game_cards(session)
    game = next((candidate for candidate in games if candidate.key == game_key), None)
    if game is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="game not found")
    return templates.TemplateResponse(
        request=request,
        name="game.html",
        context=_context(request, page="game", game=game),
    )


@router.get("/leaderboard", response_class=HTMLResponse, name="leaderboard_page")
async def leaderboard_page(request: Request) -> HTMLResponse:
    """Render the current server-defined UTC leaderboard period."""

    period_start, period_end = leaderboard_period(datetime.now(UTC))
    return templates.TemplateResponse(
        request=request,
        name="leaderboard.html",
        context=_context(
            request,
            page="leaderboard",
            period_start=period_start,
            period_end=period_end,
        ),
    )
