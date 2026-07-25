"""Player-owned analytics HTTP adapters."""

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_player_id
from app.database import get_session as get_database_session
from app.schemas import GameSummaryItem, GameSummaryResponse
from app.services import analytics as analytics_services

router = APIRouter()
Session = Annotated[AsyncSession, Depends(get_database_session)]
OwnerId = Annotated[UUID, Depends(get_player_id)]


@router.get("/analytics/game-summary", response_model=GameSummaryResponse)
async def get_game_summary(
    session: Session,
    owner_id: OwnerId,
    start_at: Annotated[datetime | None, Query()] = None,
    end_at: Annotated[datetime | None, Query()] = None,
    game_key: Annotated[
        str | None, Query(min_length=1, max_length=40, pattern=r"^[a-z0-9_]+$")
    ] = None,
) -> GameSummaryResponse:
    rows = await analytics_services.analytics_game_summary(
        session, owner_id=owner_id, start_at=start_at, end_at=end_at, game_key=game_key
    )
    return GameSummaryResponse(
        items=[
            GameSummaryItem(
                game_key=row.game_key,
                plays=row.plays,
                rewards_issued=row.rewards_issued,
                average_reward_value=row.average_reward_value,
            )
            for row in rows
        ],
        start_at=start_at,
        end_at=end_at,
        game_key=game_key,
    )
