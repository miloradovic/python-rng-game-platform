"""Player and catalogue HTTP adapters."""

from datetime import datetime
from typing import Annotated, cast
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app import services
from app.database import get_session as get_database_session
from app.models import FairnessProof
from app.rng import OutcomeProvider
from app.rng import RewardBand as FairnessRewardBand
from app.schemas import (
    ClaimRequest,
    FairnessCommitRequest,
    FairnessCommitResponse,
    FairnessEvaluateRequest,
    FairnessEvaluateResponse,
    FairnessProofResponse,
    FairnessVerificationResponse,
    GameConfigResponse,
    GameListResponse,
    GameResponse,
    GameSummaryItem,
    GameSummaryResponse,
    OutcomeAuditResponse,
    OutcomeResponse,
    PlayerCreate,
    PlayerResponse,
    PlayRequest,
    RewardListResponse,
    RewardResponse,
    SessionCreate,
    SessionResponse,
)
from app.schemas import (
    RewardBand as RewardBandResponse,
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


@router.post("/sessions/{session_id}/claim", response_model=RewardResponse)
async def claim_session_reward(
    session_id: UUID,
    body: ClaimRequest,
    session: Session,
    owner_id: Annotated[UUID, Header(alias="X-Player-ID")],
) -> RewardResponse:
    reward = await services.claim_session_reward(session, session_id=session_id, owner_id=owner_id)
    del body
    return RewardResponse.model_validate(reward)


@router.get("/players/{player_id}/rewards", response_model=RewardListResponse)
async def list_player_rewards(
    player_id: UUID,
    session: Session,
    owner_id: Annotated[UUID, Header(alias="X-Player-ID")],
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> RewardListResponse:
    rewards = await services.player_rewards(
        session, player_id=player_id, owner_id=owner_id, limit=limit, offset=offset
    )
    return RewardListResponse(
        items=[RewardResponse.model_validate(reward) for reward in rewards],
        limit=limit,
        offset=offset,
    )


@router.get("/audit/outcomes/{outcome_id}", response_model=OutcomeAuditResponse)
async def get_outcome_audit(
    outcome_id: UUID,
    session: Session,
    owner_id: Annotated[UUID, Header(alias="X-Player-ID")],
) -> OutcomeAuditResponse:
    outcome, evidence = await services.retrieve_outcome_audit(session, outcome_id, owner_id)
    return OutcomeAuditResponse(outcome=OutcomeResponse.model_validate(outcome), evidence=evidence)


@router.get("/analytics/game-summary", response_model=GameSummaryResponse)
async def get_game_summary(
    session: Session,
    owner_id: Annotated[UUID, Header(alias="X-Player-ID")],
    start_at: Annotated[datetime | None, Query()] = None,
    end_at: Annotated[datetime | None, Query()] = None,
    game_key: Annotated[
        str | None, Query(min_length=1, max_length=40, pattern=r"^[a-z0-9_]+$")
    ] = None,
) -> GameSummaryResponse:
    rows = await services.analytics_game_summary(
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


@router.post(
    "/fairness/commit", response_model=FairnessCommitResponse, status_code=status.HTTP_201_CREATED
)
async def commit_fairness(
    body: FairnessCommitRequest,
    session: Session,
    owner_id: Annotated[UUID, Header(alias="X-Player-ID")],
) -> FairnessCommitResponse:
    proof = await services.commit_fairness(session, session_id=body.session_id, owner_id=owner_id)
    return FairnessCommitResponse(
        proof_id=proof.id,
        status=proof.status.value,
        protocol_version=proof.protocol_version,
        algorithm=proof.algorithm,
        server_seed_commitment=proof.server_seed_commitment,
        session_id=proof.session_id,
        game_key=proof.game_key,
        config_version_id=proof.config_version_id,
    )


@router.post("/fairness/evaluate", response_model=FairnessEvaluateResponse)
async def evaluate_fairness(
    body: FairnessEvaluateRequest,
    session: Session,
    owner_id: Annotated[UUID, Header(alias="X-Player-ID")],
) -> FairnessEvaluateResponse:
    outcome, reward, proof = await services.evaluate_fairness(
        session, proof_id=body.proof_id, owner_id=owner_id, client_seed=body.client_seed
    )
    return FairnessEvaluateResponse(
        proof_id=proof.id,
        status=proof.status.value,
        outcome=OutcomeResponse.model_validate(outcome),
        reward=RewardResponse.model_validate(reward),
    )


def _fairness_proof_response(
    proof: FairnessProof, reward_bands: tuple[FairnessRewardBand, ...]
) -> FairnessProofResponse:
    """Translate service-validated finalized evidence for the HTTP boundary."""

    return FairnessProofResponse(
        proof_id=proof.id,
        outcome_id=cast(UUID, proof.outcome_id),
        status=proof.status.value,
        protocol_version=proof.protocol_version,
        algorithm=proof.algorithm,
        server_seed_commitment=proof.server_seed_commitment,
        server_seed_revealed=cast(str, proof.server_seed_revealed),
        client_seed=cast(str, proof.client_seed),
        nonce=proof.nonce,
        game_key=proof.game_key,
        session_id=proof.session_id,
        config_version_id=proof.config_version_id,
        mapping_version=proof.mapping_version,
        mapping_digest=proof.mapping_digest,
        reward_bands=[
            RewardBandResponse(key=band.key, weight=band.weight, value=band.value)
            for band in reward_bands
        ],
        raw_random_value=cast(str, proof.raw_random_value),
        normalized_value=cast(int, proof.normalized_value),
        derivation_attempt=cast(int, proof.derivation_attempt),
        reward_key=cast(str, proof.reward_key),
        reward_value=cast(int, proof.reward_value),
        revealed_at=cast(datetime, proof.revealed_at),
    )


@router.get("/fairness/outcomes/{outcome_id}/proof", response_model=FairnessProofResponse)
async def get_fairness_proof(
    outcome_id: UUID,
    session: Session,
    owner_id: Annotated[UUID, Header(alias="X-Player-ID")],
) -> FairnessProofResponse:
    proof, reward_bands = await services.retrieve_fairness_proof(
        session, outcome_id=outcome_id, owner_id=owner_id
    )
    return _fairness_proof_response(proof, reward_bands)


@router.get("/fairness/outcomes/{outcome_id}/verify", response_model=FairnessVerificationResponse)
async def verify_fairness_proof(
    outcome_id: UUID,
    session: Session,
    owner_id: Annotated[UUID, Header(alias="X-Player-ID")],
) -> FairnessVerificationResponse:
    _, _, code, verified = await services.verify_fairness_proof(
        session, outcome_id=outcome_id, owner_id=owner_id
    )
    return FairnessVerificationResponse(outcome_id=outcome_id, verified=verified, code=code)
