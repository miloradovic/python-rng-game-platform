"""Commitment/reveal proof lifecycle and verification orchestration."""

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app import repositories
from app.fairness_events import (
    commit_event_payload as _commit_event_payload,
)
from app.fairness_events import (
    fairness_event_hash as _fairness_event_hash,
)
from app.fairness_events import (
    fairness_event_hash_v1 as _fairness_event_hash_v1,
)
from app.fairness_events import (
    revealed_event_payload as _revealed_event_payload,
)
from app.game_rules import InvalidRulesInputError, rules_for
from app.models import (
    FairnessProof,
    FairnessProofEvent,
    FairnessProofStatus,
    GameConfigVersion,
    Outcome,
    Reward,
    SessionStatus,
)
from app.rng import (
    ALGORITHM_HMAC_SHA256,
    DAILY_SPIN_MAPPING_VERSION_V1,
    PROTOCOL_VERSION_V1,
    DailySpinProof,
    FairnessError,
    create_server_seed,
    daily_spin_mapping_digest,
    derive_daily_spin,
    server_seed_commitment,
    verify_daily_spin_proof,
)
from app.rng import RewardBand as FairnessRewardBand
from app.services._common import _owned_outcome, _request_fingerprint, utc_now
from app.services.errors import (
    ForbiddenError,
    IdempotencyConflictError,
    InvalidPlayError,
    InvalidTransitionError,
    NotFoundError,
    SessionExpiredError,
)
from app.services.session_termination import (
    end_committed_fairness_proof as end_committed_fairness_proof,
)
from app.services.session_termination import (
    finalize_expired_session,
)


def _fairness_reward_bands(
    game_key: str, config: GameConfigVersion
) -> tuple[FairnessRewardBand, ...]:
    try:
        registered_game = rules_for(game_key)
        if registered_game.mode != "fairness":
            raise InvalidRulesInputError("game does not support fairness")
        return registered_game.fairness.fairness_reward_bands(config)
    except InvalidRulesInputError as error:
        raise InvalidPlayError from error


async def commit_fairness(
    session: AsyncSession,
    *,
    session_id: uuid.UUID,
    owner_id: uuid.UUID,
    clock: Callable[[], datetime] = utc_now,
) -> FairnessProof:
    """Durably commit a server seed before any daily-spin outcome is evaluated."""

    game_session = await repositories.lock_session(session, session_id)
    if game_session is None:
        raise NotFoundError
    if game_session.player_id != owner_id:
        raise ForbiddenError
    now = clock()
    if await finalize_expired_session(session, game_session, now=now):
        await session.commit()
        raise SessionExpiredError
    if game_session.status != SessionStatus.ACTIVE:
        raise InvalidTransitionError
    game = await repositories.get_game_by_id(session, game_session.game_id)
    config = await repositories.get_config_by_id(session, game_session.config_version_id)
    if game is None or config is None or config.game_id != game.id:
        raise InvalidPlayError
    existing = await repositories.lock_fairness_proof_by_session(session, game_session.id)
    if existing is not None:
        await session.commit()
        return existing
    bands = _fairness_reward_bands(game.key, config)
    seed = create_server_seed()
    proof = FairnessProof(
        session_id=game_session.id,
        player_id=owner_id,
        game_id=game.id,
        game_key=game.key,
        config_version_id=config.id,
        outcome_id=None,
        status=FairnessProofStatus.COMMITTED,
        protocol_version=PROTOCOL_VERSION_V1,
        algorithm=ALGORITHM_HMAC_SHA256,
        server_seed_commitment=server_seed_commitment(seed),
        nonce=0,
        client_seed=None,
        evaluation_fingerprint=None,
        mapping_version=DAILY_SPIN_MAPPING_VERSION_V1,
        mapping_digest=daily_spin_mapping_digest(bands),
        raw_random_value=None,
        normalized_value=None,
        derivation_attempt=None,
        reward_key=None,
        reward_value=None,
        evaluated_at=None,
        server_seed_revealed=None,
        revealed_at=None,
    )
    proof = await repositories.add_fairness_proof(session, proof)
    await repositories.add_fairness_seed_custody(session, proof.id, seed)
    payload = _commit_event_payload(proof)
    event = FairnessProofEvent(
        proof_id=proof.id,
        sequence=0,
        event_type="committed",
        evidence_version=2,
        payload=payload,
        status=FairnessProofStatus.COMMITTED,
        previous_evidence_hash=None,
        evidence_hash=_fairness_event_hash(
            proof_id=proof.id,
            sequence=0,
            event_type="committed",
            status=FairnessProofStatus.COMMITTED,
            created_at=now,
            previous_evidence_hash=None,
            payload=payload,
        ),
        created_at=now,
    )
    await repositories.add_fairness_proof_event(session, event)
    await session.commit()
    return proof


async def evaluate_fairness(
    session: AsyncSession,
    *,
    proof_id: uuid.UUID,
    owner_id: uuid.UUID,
    client_seed: str,
    clock: Callable[[], datetime] = utc_now,
) -> tuple[Outcome, Reward, FairnessProof]:
    """Atomically reveal a committed daily-spin outcome and its one reward entitlement."""

    evaluation_fingerprint = _request_fingerprint(
        "evaluate_fairness", {"proof_id": str(proof_id), "client_seed": client_seed}
    )
    visible_proof = await repositories.get_fairness_proof(session, proof_id)
    if visible_proof is None:
        raise NotFoundError
    proof_owner_id, proof_session_id = visible_proof
    if proof_owner_id != owner_id:
        raise ForbiddenError
    game_session = await repositories.lock_session(session, proof_session_id)
    if game_session is None:
        raise NotFoundError
    proof = await repositories.lock_fairness_proof(session, proof_id)
    if proof is None:
        raise NotFoundError
    if proof.status == FairnessProofStatus.REVEALED:
        if proof.evaluation_fingerprint != evaluation_fingerprint:
            raise IdempotencyConflictError
        if proof.outcome_id is None:
            raise InvalidTransitionError
        outcome = await repositories.get_outcome(session, proof.outcome_id)
        reward = await repositories.lock_reward_by_session(session, proof.session_id)
        if outcome is None or reward is None:
            raise NotFoundError
        await session.commit()
        return outcome, reward, proof
    now = clock()
    if await finalize_expired_session(session, game_session, now=now, locked_proof=proof):
        await session.commit()
        raise SessionExpiredError
    if proof.status != FairnessProofStatus.COMMITTED or game_session.status != SessionStatus.ACTIVE:
        raise InvalidTransitionError
    game = await repositories.get_game_by_id(session, proof.game_id)
    config = await repositories.get_config_by_id(session, proof.config_version_id)
    custody = await repositories.get_fairness_seed_custody(session, proof.id)
    if game is None or config is None or custody is None:
        raise NotFoundError
    try:
        derivation = derive_daily_spin(
            server_seed=custody.server_seed_material,
            client_seed=client_seed,
            nonce=proof.nonce,
            config_version_id=proof.config_version_id,
            session_id=proof.session_id,
            reward_bands=_fairness_reward_bands(game.key, config),
            game_key=proof.game_key,
            protocol_version=proof.protocol_version,
            algorithm=proof.algorithm,
            mapping_version=proof.mapping_version,
        )
    except FairnessError as error:
        raise InvalidPlayError from error
    outcome = await repositories.add_outcome(
        session,
        game_session,
        {
            "reward_key": derivation.reward.key,
            "normalized_value": derivation.normalized_value,
            "derivation_digest": derivation.raw_digest_hex,
        },
    )
    reward = await repositories.add_reward(
        session, outcome, player_id=owner_id, value=derivation.reward.value
    )
    proof.outcome_id = outcome.id
    proof.status = FairnessProofStatus.REVEALED
    proof.client_seed = client_seed
    proof.evaluation_fingerprint = evaluation_fingerprint
    proof.raw_random_value = derivation.raw_digest_hex
    proof.normalized_value = derivation.normalized_value
    proof.derivation_attempt = derivation.attempt
    proof.reward_key = derivation.reward.key
    proof.reward_value = derivation.reward.value
    proof.evaluated_at = now
    proof.server_seed_revealed = custody.server_seed_material.hex()
    proof.revealed_at = now
    previous_event = await repositories.lock_latest_fairness_proof_event(session, proof.id)
    if previous_event is None:
        raise InvalidTransitionError
    payload = _revealed_event_payload(proof)
    event = FairnessProofEvent(
        proof_id=proof.id,
        sequence=1,
        event_type="revealed",
        evidence_version=2,
        payload=payload,
        status=FairnessProofStatus.REVEALED,
        previous_evidence_hash=previous_event.evidence_hash,
        evidence_hash="0" * 64,
        created_at=now,
    )
    event.evidence_hash = _fairness_event_hash(
        proof_id=proof.id,
        sequence=1,
        event_type="revealed",
        status=FairnessProofStatus.REVEALED,
        created_at=now,
        previous_evidence_hash=event.previous_evidence_hash,
        payload=payload,
    )
    await repositories.add_fairness_proof_event(session, event)
    game_session.status = SessionStatus.COMPLETED
    game_session.ended_at = now
    await repositories.add_outcome_audit(session, outcome, player_id=owner_id, game_key=game.key)
    await repositories.add_game_played_event(
        session, outcome, player_id=owner_id, game_key=game.key
    )
    await repositories.add_reward_evidence(
        session, reward, event_type="reward_issued", game_key=game.key
    )
    await session.commit()
    return outcome, reward, proof


@dataclass(frozen=True)
class RevealedFairnessProof:
    """Immutable, non-null finalized proof evidence for response and verification use."""

    proof_id: uuid.UUID
    outcome_id: uuid.UUID
    status: FairnessProofStatus
    protocol_version: str
    algorithm: str
    server_seed_commitment: str
    server_seed_revealed: str
    client_seed: str
    nonce: int
    game_key: str
    session_id: uuid.UUID
    config_version_id: uuid.UUID
    mapping_version: str
    mapping_digest: str
    raw_random_value: str
    normalized_value: int
    derivation_attempt: int
    reward_key: str
    reward_value: int
    evaluated_at: datetime
    revealed_at: datetime


def _as_revealed_proof(proof: FairnessProof) -> RevealedFairnessProof:
    material = (
        proof.outcome_id,
        proof.server_seed_revealed,
        proof.client_seed,
        proof.raw_random_value,
        proof.normalized_value,
        proof.derivation_attempt,
        proof.reward_key,
        proof.reward_value,
        proof.evaluated_at,
        proof.revealed_at,
    )
    if proof.status != FairnessProofStatus.REVEALED or any(value is None for value in material):
        raise InvalidTransitionError
    outcome_id, seed, client_seed, raw, normalized, attempt, key, value, evaluated, revealed = (
        material
    )
    if not (
        isinstance(outcome_id, uuid.UUID)
        and isinstance(seed, str)
        and isinstance(client_seed, str)
        and isinstance(raw, str)
        and isinstance(normalized, int)
        and isinstance(attempt, int)
        and isinstance(key, str)
        and isinstance(value, int)
        and isinstance(evaluated, datetime)
        and isinstance(revealed, datetime)
    ):
        raise InvalidTransitionError
    return RevealedFairnessProof(
        proof_id=proof.id,
        outcome_id=outcome_id,
        status=proof.status,
        protocol_version=proof.protocol_version,
        algorithm=proof.algorithm,
        server_seed_commitment=proof.server_seed_commitment,
        server_seed_revealed=seed,
        client_seed=client_seed,
        nonce=proof.nonce,
        game_key=proof.game_key,
        session_id=proof.session_id,
        config_version_id=proof.config_version_id,
        mapping_version=proof.mapping_version,
        mapping_digest=proof.mapping_digest,
        raw_random_value=raw,
        normalized_value=normalized,
        derivation_attempt=attempt,
        reward_key=key,
        reward_value=value,
        evaluated_at=evaluated,
        revealed_at=revealed,
    )


def verify_fairness_event_chain(
    proof: FairnessProof, events: list[FairnessProofEvent]
) -> tuple[bool, str]:
    """Validate ordering, linkage, hashes, and the material proof snapshot."""

    if not events:
        return False, "event_chain_missing"
    previous: str | None = None
    for sequence, event in enumerate(events):
        if event.sequence != sequence or event.previous_evidence_hash != previous:
            return False, "event_chain_link_mismatch"
        if event.evidence_version == 1:
            expected = _fairness_event_hash_v1(
                proof_id=event.proof_id,
                sequence=event.sequence,
                event_type=event.event_type,
                status=event.status,
                created_at=event.created_at,
                previous_evidence_hash=event.previous_evidence_hash,
            )
        elif event.evidence_version == 2:
            expected = _fairness_event_hash(
                proof_id=event.proof_id,
                sequence=event.sequence,
                event_type=event.event_type,
                status=event.status,
                created_at=event.created_at,
                previous_evidence_hash=event.previous_evidence_hash,
                payload=event.payload,
            )
        else:
            return False, "unsupported_event_version"
        if expected != event.evidence_hash:
            return False, "event_hash_mismatch"
        previous = event.evidence_hash
    if events[0].evidence_version == 2 and events[0].payload != _commit_event_payload(proof):
        return False, "commit_payload_mismatch"
    if proof.status == FairnessProofStatus.REVEALED:
        final = events[-1]
        if final.status != FairnessProofStatus.REVEALED:
            return False, "event_terminal_state_mismatch"
        if final.evidence_version == 2 and final.payload != _revealed_event_payload(proof):
            return False, "revealed_payload_mismatch"
    return True, "verified"


async def retrieve_fairness_proof(
    session: AsyncSession, *, outcome_id: uuid.UUID, owner_id: uuid.UUID
) -> tuple[RevealedFairnessProof, tuple[FairnessRewardBand, ...]]:
    """Return immutable finalized proof evidence owned by the requesting player."""

    outcome, _ = await _owned_outcome(session, outcome_id=outcome_id, owner_id=owner_id)
    proof = await repositories.get_fairness_proof_by_outcome(session, outcome.id)
    if proof is None:
        raise NotFoundError
    revealed = _as_revealed_proof(proof)
    config = await repositories.get_config_by_id(session, proof.config_version_id)
    if config is None:
        raise NotFoundError
    return revealed, _fairness_reward_bands(proof.game_key, config)


async def verify_fairness_proof(
    session: AsyncSession, *, outcome_id: uuid.UUID, owner_id: uuid.UUID
) -> tuple[RevealedFairnessProof, tuple[FairnessRewardBand, ...], str, bool]:
    """Recalculate proof derivation and validate its append-only lifecycle chain."""

    proof_model = await repositories.get_fairness_proof_by_outcome(session, outcome_id)
    proof, bands = await retrieve_fairness_proof(session, outcome_id=outcome_id, owner_id=owner_id)
    if proof_model is None:
        raise NotFoundError
    result = verify_daily_spin_proof(
        DailySpinProof(
            protocol_version=proof.protocol_version,
            algorithm=proof.algorithm,
            server_seed_commitment=proof.server_seed_commitment,
            server_seed_hex=proof.server_seed_revealed,
            client_seed=proof.client_seed,
            nonce=proof.nonce,
            game_key=proof.game_key,
            config_version_id=proof.config_version_id,
            session_id=proof.session_id,
            reward_bands=bands,
            mapping_version=proof.mapping_version,
            mapping_digest=proof.mapping_digest,
            raw_digest_hex=proof.raw_random_value,
            normalized_value=proof.normalized_value,
            attempt=proof.derivation_attempt,
            reward_key=proof.reward_key,
            reward_value=proof.reward_value,
        )
    )
    if not result.verified:
        return proof, bands, result.code, False
    events = await repositories.list_fairness_proof_events(session, proof.proof_id)
    chain_verified, chain_code = verify_fairness_event_chain(proof_model, events)
    return proof, bands, chain_code, chain_verified
