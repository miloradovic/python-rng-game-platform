"""Player and game-catalogue HTTP adapters."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_player_id
from app.database import get_session as get_database_session
from app.schemas import (
    GameConfigResponse,
    GameListResponse,
    GameResponse,
    PlayerCreate,
    PlayerResponse,
)
from app.services import players as player_services

router = APIRouter()
Session = Annotated[AsyncSession, Depends(get_database_session)]
OwnerId = Annotated[UUID, Depends(get_player_id)]


@router.post("/players", response_model=PlayerResponse, status_code=status.HTTP_201_CREATED)
async def create_player(body: PlayerCreate, session: Session) -> PlayerResponse:
    return PlayerResponse.model_validate(
        await player_services.create_player(session, body.display_name)
    )


@router.get("/players/{player_id}", response_model=PlayerResponse)
async def get_player(
    player_id: UUID,
    session: Session,
    owner_id: OwnerId,
) -> PlayerResponse:
    return PlayerResponse.model_validate(
        await player_services.retrieve_player(session, player_id, owner_id)
    )


@router.get("/games", response_model=GameListResponse)
async def list_games(
    session: Session,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> GameListResponse:
    games = await player_services.catalogue(session, limit, offset)
    return GameListResponse(
        items=[GameResponse.model_validate(game) for game in games], limit=limit, offset=offset
    )


@router.get("/games/{game_key}/config", response_model=GameConfigResponse)
async def get_active_config(game_key: str, session: Session) -> GameConfigResponse:
    return GameConfigResponse.model_validate(await player_services.active_config(session, game_key))
