"""Commitment/reveal fairness HTTP adapters."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_player_id
from app.database import get_session as get_database_session
from app.rng import RewardBand as FairnessRewardBand
from app.schemas import (
    FairnessCommitRequest,
    FairnessCommitResponse,
    FairnessEvaluateRequest,
    FairnessEvaluateResponse,
    FairnessProofResponse,
    FairnessVerificationResponse,
    OutcomeResponse,
    RewardResponse,
)
from app.schemas import RewardBand as RewardBandResponse
from app.services import fairness as fairness_services

router = APIRouter()
Session = Annotated[AsyncSession, Depends(get_database_session)]
OwnerId = Annotated[UUID, Depends(get_player_id)]


@router.post(
    "/fairness/commit", response_model=FairnessCommitResponse, status_code=status.HTTP_201_CREATED
)
async def commit_fairness(
    body: FairnessCommitRequest,
    session: Session,
    owner_id: OwnerId,
) -> FairnessCommitResponse:
    proof = await fairness_services.commit_fairness(
        session, session_id=body.session_id, owner_id=owner_id
    )
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
    owner_id: OwnerId,
) -> FairnessEvaluateResponse:
    outcome, reward, proof = await fairness_services.evaluate_fairness(
        session, proof_id=body.proof_id, owner_id=owner_id, client_seed=body.client_seed
    )
    return FairnessEvaluateResponse(
        proof_id=proof.id,
        status=proof.status.value,
        outcome=OutcomeResponse.model_validate(outcome),
        reward=RewardResponse.model_validate(reward),
    )


def _fairness_proof_response(
    proof: fairness_services.RevealedFairnessProof, reward_bands: tuple[FairnessRewardBand, ...]
) -> FairnessProofResponse:
    """Translate service-validated finalized evidence for the HTTP boundary."""

    return FairnessProofResponse(
        proof_id=proof.proof_id,
        outcome_id=proof.outcome_id,
        status=proof.status.value,
        protocol_version=proof.protocol_version,
        algorithm=proof.algorithm,
        server_seed_commitment=proof.server_seed_commitment,
        server_seed_revealed=proof.server_seed_revealed,
        client_seed=proof.client_seed,
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
        raw_random_value=proof.raw_random_value,
        normalized_value=proof.normalized_value,
        derivation_attempt=proof.derivation_attempt,
        reward_key=proof.reward_key,
        reward_value=proof.reward_value,
        revealed_at=proof.revealed_at,
    )


@router.get("/fairness/outcomes/{outcome_id}/proof", response_model=FairnessProofResponse)
async def get_fairness_proof(
    outcome_id: UUID,
    session: Session,
    owner_id: OwnerId,
) -> FairnessProofResponse:
    proof, reward_bands = await fairness_services.retrieve_fairness_proof(
        session, outcome_id=outcome_id, owner_id=owner_id
    )
    return _fairness_proof_response(proof, reward_bands)


@router.get("/fairness/outcomes/{outcome_id}/verify", response_model=FairnessVerificationResponse)
async def verify_fairness_proof(
    outcome_id: UUID,
    session: Session,
    owner_id: OwnerId,
) -> FairnessVerificationResponse:
    _, _, code, verified = await fairness_services.verify_fairness_proof(
        session, outcome_id=outcome_id, owner_id=owner_id
    )
    return FairnessVerificationResponse(outcome_id=outcome_id, verified=verified, code=code)
