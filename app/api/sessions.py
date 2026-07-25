"""Session lifecycle HTTP adapters."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_player_id
from app.database import get_session as get_database_session
from app.schemas import SessionCreate, SessionResponse
from app.services import sessions as session_services

router = APIRouter()
Session = Annotated[AsyncSession, Depends(get_database_session)]
OwnerId = Annotated[UUID, Depends(get_player_id)]


@router.post("/sessions", response_model=SessionResponse, status_code=status.HTTP_201_CREATED)
async def create_session(
    body: SessionCreate,
    session: Session,
    owner_id: OwnerId,
) -> SessionResponse:
    game_session = await session_services.create_session(
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
    owner_id: OwnerId,
) -> SessionResponse:
    return SessionResponse.model_validate(
        await session_services.retrieve_session(session, session_id, owner_id)
    )


@router.delete("/sessions/{session_id}", response_model=SessionResponse)
async def cancel_session(
    session_id: UUID,
    session: Session,
    owner_id: OwnerId,
) -> SessionResponse:
    return SessionResponse.model_validate(
        await session_services.cancel_session(session, session_id, owner_id)
    )
