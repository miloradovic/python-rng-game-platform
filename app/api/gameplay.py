"""Gameplay and outcome-audit HTTP adapters."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_outcome_provider, get_player_id
from app.database import get_session as get_database_session
from app.rng import OutcomeProvider
from app.schemas import OutcomeAuditResponse, OutcomeResponse, PlayRequest
from app.services import gameplay as gameplay_services

router = APIRouter()
Session = Annotated[AsyncSession, Depends(get_database_session)]
OwnerId = Annotated[UUID, Depends(get_player_id)]
Provider = Annotated[OutcomeProvider, Depends(get_outcome_provider)]


@router.post(
    "/sessions/{session_id}/play",
    response_model=OutcomeResponse,
    description=(
        "Resolve a direct-play session with server-authoritative rules. Repeating a completed "
        "Prediction Card request with the same choice returns its recorded outcome; changing "
        "the choice returns an idempotency conflict."
    ),
)
async def play_session(
    session_id: UUID,
    body: PlayRequest,
    session: Session,
    provider: Provider,
    owner_id: OwnerId,
) -> OutcomeResponse:
    outcome = await gameplay_services.play_session(
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
    owner_id: OwnerId,
) -> OutcomeAuditResponse:
    outcome, evidence = await gameplay_services.retrieve_outcome_audit(
        session, outcome_id, owner_id
    )
    return OutcomeAuditResponse(outcome=OutcomeResponse.model_validate(outcome), evidence=evidence)
