"""Reward read and claim HTTP adapters."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_player_id
from app.database import get_session as get_database_session
from app.schemas import ClaimRequest, RewardListResponse, RewardResponse
from app.services import rewards as reward_services

router = APIRouter()
Session = Annotated[AsyncSession, Depends(get_database_session)]
OwnerId = Annotated[UUID, Depends(get_player_id)]


@router.post("/sessions/{session_id}/claim", response_model=RewardResponse)
async def claim_session_reward(
    session_id: UUID,
    body: ClaimRequest,
    session: Session,
    owner_id: OwnerId,
) -> RewardResponse:
    reward = await reward_services.claim_session_reward(
        session, session_id=session_id, owner_id=owner_id
    )
    del body
    return RewardResponse.model_validate(reward)


@router.get("/players/{player_id}/rewards", response_model=RewardListResponse)
async def list_player_rewards(
    player_id: UUID,
    session: Session,
    owner_id: OwnerId,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> RewardListResponse:
    rewards = await reward_services.player_rewards(
        session, player_id=player_id, owner_id=owner_id, limit=limit, offset=offset
    )
    return RewardListResponse(
        items=[RewardResponse.model_validate(reward) for reward in rewards],
        limit=limit,
        offset=offset,
    )
