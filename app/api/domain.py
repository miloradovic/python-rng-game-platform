"""Player and catalogue HTTP adapters."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app import services
from app.database import get_session as get_database_session
from app.rng import OutcomeProvider
from app.schemas import (
    GameConfigResponse,
    GameListResponse,
    GameResponse,
    OutcomeAuditResponse,
    OutcomeResponse,
    PlayerCreate,
    PlayerResponse,
    PlayRequest,
    SessionCreate,
    SessionResponse,
)

router = APIRouter()
Session = Annotated[AsyncSession, Depends(get_database_session)]


@router.post("/players", response_model=PlayerResponse, status_code=status.HTTP_201_CREATED)
async def create_player(body: PlayerCreate, session: Session) -> PlayerResponse:
    return PlayerResponse.model_validate(await services.create_player(session, body.display_name))


@router.get("/players/{player_id}", response_model=PlayerResponse)
async def get_player(
    player_id: UUID,
    session: Session,
    owner_id: Annotated[UUID, Header(alias="X-Player-ID")],
) -> PlayerResponse:
    return PlayerResponse.model_validate(
        await services.retrieve_player(session, player_id, owner_id)
    )


@router.get("/games", response_model=GameListResponse)
async def list_games(
    session: Session,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> GameListResponse:
    games = await services.catalogue(session, limit, offset)
    return GameListResponse(
        items=[GameResponse.model_validate(game) for game in games], limit=limit, offset=offset
    )


@router.get("/games/{game_key}/config", response_model=GameConfigResponse)
async def get_active_config(game_key: str, session: Session) -> GameConfigResponse:
    return GameConfigResponse.model_validate(await services.active_config(session, game_key))


@router.post("/sessions", response_model=SessionResponse, status_code=status.HTTP_201_CREATED)
async def create_session(
    body: SessionCreate,
    session: Session,
    owner_id: Annotated[UUID, Header(alias="X-Player-ID")],
) -> SessionResponse:
    game_session = await services.create_session(
        session,
        request_id=body.request_id,
        player_id=body.player_id,
        owner_id=owner_id,
        game_key=body.game_key,
    )
    return SessionResponse.model_validate(game_session)


@router.get("/sessions/{session_id}", response_model=SessionResponse)
async def get_session(
    session_id: UUID,
    session: Session,
    owner_id: Annotated[UUID, Header(alias="X-Player-ID")],
) -> SessionResponse:
    return SessionResponse.model_validate(
        await services.retrieve_session(session, session_id, owner_id)
    )


@router.delete("/sessions/{session_id}", response_model=SessionResponse)
async def cancel_session(
    session_id: UUID,
    session: Session,
    owner_id: Annotated[UUID, Header(alias="X-Player-ID")],
) -> SessionResponse:
    return SessionResponse.model_validate(
        await services.cancel_session(session, session_id, owner_id)
    )


@router.post("/sessions/{session_id}/play", response_model=OutcomeResponse)
async def play_session(
    session_id: UUID,
    body: PlayRequest,
    session: Session,
    request: Request,
    owner_id: Annotated[UUID, Header(alias="X-Player-ID")],
) -> OutcomeResponse:
    provider: OutcomeProvider = request.app.state.outcome_provider
    outcome = await services.play_session(
        session,
        session_id=session_id,
        owner_id=owner_id,
        choice=body.choice,
        actions=body.actions,
        provider=provider,
    )
    return OutcomeResponse.model_validate(outcome)


@router.get("/audit/outcomes/{outcome_id}", response_model=OutcomeAuditResponse)
async def get_outcome_audit(
    outcome_id: UUID,
    session: Session,
    owner_id: Annotated[UUID, Header(alias="X-Player-ID")],
) -> OutcomeAuditResponse:
    outcome, evidence = await services.retrieve_outcome_audit(session, outcome_id, owner_id)
    return OutcomeAuditResponse(outcome=OutcomeResponse.model_validate(outcome), evidence=evidence)
