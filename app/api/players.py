"""Player and game-catalogue HTTP adapters."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_player_id
from app.database import get_session as get_database_session
from app.schemas import (
    FairnessStateResponse,
    FinalScoreResponse,
    GameConfigResponse,
    GameListResponse,
    GameResponse,
    OutcomeResponse,
    PlayerCreate,
    PlayerGameSessionResponse,
    PlayerGameStateResponse,
    PlayerResponse,
    RewardResponse,
)
from app.services import players as player_services
from app.services import sessions as session_services

router = APIRouter()
Session = Annotated[AsyncSession, Depends(get_database_session)]
OwnerId = Annotated[UUID, Depends(get_player_id)]


@router.post(
    "/players",
    response_model=PlayerResponse,
    status_code=status.HTTP_201_CREATED,
    description=(
        "Create a local demonstration player. The returned public_label is generated "
        "by the server for safe leaderboard presentation; display_name remains private "
        "to owner-facing responses. X-Player-ID is a demo boundary, not authentication."
    ),
)
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


@router.get(
    "/players/{player_id}/game-state",
    response_model=PlayerGameStateResponse,
    description=(
        "Return the PostgreSQL-authoritative recovery state for one owner and game, "
        "including server time, next-play eligibility, and the latest recoverable "
        "session, outcome, reward, fairness, and final-score state. The path player ID "
        "must match X-Player-ID. Browser timers and operation journals are not authority."
    ),
)
async def get_player_game_state(
    player_id: UUID,
    session: Session,
    owner_id: OwnerId,
    game_key: Annotated[str, Query(min_length=1, max_length=40)],
) -> PlayerGameStateResponse:
    """Return authoritative state for browser recovery after an uncertain response."""

    state = await session_services.retrieve_player_game_state(
        session,
        player_id=player_id,
        owner_id=owner_id,
        game_key=game_key,
    )
    session_response = None
    if state.game_session is not None:
        fairness = None
        if state.fairness_proof is not None:
            fairness = FairnessStateResponse(
                proof_id=state.fairness_proof.id,
                status=state.fairness_proof.status.value,
                outcome_id=state.fairness_proof.outcome_id,
            )
        session_response = PlayerGameSessionResponse(
            id=state.game_session.id,
            request_id=state.game_session.request_id,
            game_key=state.game_key,
            config_version_id=state.game_session.config_version_id,
            status=state.game_session.status,
            created_at=state.game_session.created_at,
            expires_at=state.game_session.expires_at,
            ended_at=state.game_session.ended_at,
            challenge=state.game_session.challenge,
            outcome=(
                OutcomeResponse.model_validate(state.outcome) if state.outcome is not None else None
            ),
            reward=(
                RewardResponse.model_validate(state.reward) if state.reward is not None else None
            ),
            fairness=fairness,
            final_score=(
                FinalScoreResponse.model_validate(state.final_score)
                if state.final_score is not None
                else None
            ),
        )
    return PlayerGameStateResponse(
        server_time=state.server_time,
        next_play_at=state.next_play_at,
        game_key=state.game_key,
        session=session_response,
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
