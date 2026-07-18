"""Business use cases and explicit transaction boundaries."""

import hashlib
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app import repositories
from app.models import (
    FairnessProof,
    FairnessProofEvent,
    FairnessProofStatus,
    Game,
    GameConfigVersion,
    GameSession,
    Outcome,
    Player,
    PlayerStatus,
    Reward,
    RewardStatus,
    SessionStatus,
)
from app.rng import (
    ALGORITHM_HMAC_SHA256,
    DAILY_SPIN_MAPPING_VERSION_V1,
    PROTOCOL_VERSION_V1,
    DailySpinProof,
    FairnessError,
    OutcomeProvider,
    create_server_seed,
    daily_spin_mapping_digest,
    derive_daily_spin,
    secure_challenge,
    server_seed_commitment,
    verify_daily_spin_proof,
)
from app.rng import (
    RewardBand as FairnessRewardBand,
)
from app.schemas import game_config_adapter


class DomainError(Exception):
    """Stable domain failure mapped at the HTTP boundary."""

    code = "domain_error"


class NotFoundError(DomainError):
    code = "not_found"


class ForbiddenError(DomainError):
    code = "forbidden"


class InactiveGameError(DomainError):
    code = "game_inactive"


class InactivePlayerError(DomainError):
    code = "player_inactive"


class CooldownError(DomainError):
    code = "cooldown_active"


class ActiveSessionError(DomainError):
    code = "active_session_exists"


class InvalidTransitionError(DomainError):
    code = "invalid_transition"


class InvalidPlayError(DomainError):
    code = "invalid_play"


class SessionExpiredError(DomainError):
    code = "session_expired"


class RewardUnavailableError(DomainError):
    code = "reward_unavailable"


class InvalidAnalyticsRangeError(DomainError):
    code = "invalid_analytics_range"


async def create_player(session: AsyncSession, display_name: str) -> Player:
    player = await repositories.add_player(session, display_name)
    await session.commit()
    return player


async def retrieve_player(
    session: AsyncSession, player_id: uuid.UUID, owner_id: uuid.UUID
) -> Player:
    if player_id != owner_id:
        raise ForbiddenError
    player = await repositories.get_player(session, player_id)
    if player is None:
        raise NotFoundError
    return player


async def catalogue(session: AsyncSession, limit: int, offset: int) -> list[Game]:
    return await repositories.list_games(session, limit, offset)


async def active_config(session: AsyncSession, game_key: str) -> GameConfigVersion:
    game = await repositories.get_game(session, game_key)
    if game is None:
        raise NotFoundError
    if not game.is_active:
        raise InactiveGameError
    config = await repositories.get_active_config(session, game.id)
    if config is None:
        raise NotFoundError
    return config


def utc_now() -> datetime:
    return datetime.now(UTC)


def expire_if_due(game_session: GameSession, now: datetime) -> bool:
    """Move an active, elapsed session to its irreversible expired state."""

    if game_session.status != SessionStatus.ACTIVE or game_session.expires_at > now:
        return False
    game_session.status = SessionStatus.EXPIRED
    game_session.ended_at = now
    return True


def cancel_active(game_session: GameSession, now: datetime) -> None:
    """Apply the owner-driven terminal transition."""

    if game_session.status != SessionStatus.ACTIVE:
        raise InvalidTransitionError
    game_session.status = SessionStatus.CANCELLED
    game_session.ended_at = now


async def create_session(
    session: AsyncSession,
    *,
    request_id: uuid.UUID,
    player_id: uuid.UUID,
    owner_id: uuid.UUID,
    game_key: str,
    clock: Callable[[], datetime] = utc_now,
) -> GameSession:
    if player_id != owner_id:
        raise ForbiddenError
    player = await repositories.lock_player(session, player_id)
    if player is None:
        raise NotFoundError
    existing = await repositories.get_session_by_request(session, player_id, request_id)
    if existing is not None:
        await session.commit()
        return existing
    if player.status != PlayerStatus.ACTIVE:
        raise InactivePlayerError
    game = await repositories.get_game(session, game_key)
    if game is None:
        raise NotFoundError
    if not game.is_active:
        raise InactiveGameError
    config = await repositories.get_active_config(session, game.id)
    if config is None:
        raise NotFoundError
    now = clock()
    await repositories.expire_active_sessions(session, player.id, game.id, now)
    latest = await repositories.latest_session(session, player.id, game.id)
    if latest is not None and latest.status == SessionStatus.ACTIVE:
        raise ActiveSessionError
    payload = game_config_adapter.validate_python(config.payload)
    if latest is not None and latest.created_at + timedelta(seconds=payload.cooldown_seconds) > now:
        raise CooldownError
    duration = getattr(payload, "duration_seconds", 300)
    challenge: dict[str, object] = {}
    if game.key == "skill_check":
        challenge = {"sequence": secure_challenge()}
    game_session = await repositories.add_session(
        session,
        request_id=request_id,
        player_id=player.id,
        game_id=game.id,
        config_version_id=config.id,
        expires_at=now + timedelta(seconds=duration),
        challenge=challenge,
    )
    await repositories.add_session_audit(session, game_session)
    await repositories.add_session_started_event(session, game_session, game_key=game.key)
    await session.commit()
    return game_session


def _daily_spin_result(
    game_session: GameSession,
    game_key: str,
    config: GameConfigVersion,
    provider: OutcomeProvider,
) -> dict[str, object]:
    payload = game_config_adapter.validate_python(config.payload)
    if payload.game_type != "daily_spin":
        raise InvalidPlayError
    total_weight = sum(reward.weight for reward in payload.rewards)
    derived = provider.uniform(
        session_id=game_session.id,
        game_key=game_key,
        config_version_id=config.id,
        purpose="daily_spin",
        upper_bound=total_weight,
    )
    cursor = derived.value
    selected = payload.rewards[-1]
    for reward in payload.rewards:
        if cursor < reward.weight:
            selected = reward
            break
        cursor -= reward.weight
    return {
        "reward_key": selected.key,
        "normalized_value": derived.value,
        "derivation_digest": derived.digest_hex,
    }


def _prediction_result(
    game_session: GameSession,
    game_key: str,
    config: GameConfigVersion,
    provider: OutcomeProvider,
    choice: str | None,
) -> dict[str, object]:
    payload = game_config_adapter.validate_python(config.payload)
    if payload.game_type != "prediction_card" or choice not in payload.choices:
        raise InvalidPlayError
    derived = provider.uniform(
        session_id=game_session.id,
        game_key=game_key,
        config_version_id=config.id,
        purpose="prediction_card",
        upper_bound=len(payload.choices),
    )
    authoritative_choice = payload.choices[derived.value]
    return {
        "player_choice": choice,
        "authoritative_choice": authoritative_choice,
        "correct": choice == authoritative_choice,
        "normalized_value": derived.value,
        "derivation_digest": derived.digest_hex,
    }


def _skill_result(
    game_session: GameSession,
    config: GameConfigVersion,
    actions: list[int] | None,
    now: datetime,
) -> dict[str, object]:
    payload = game_config_adapter.validate_python(config.payload)
    challenge = game_session.challenge.get("sequence")
    if (
        payload.game_type != "skill_check"
        or actions is None
        or not isinstance(challenge, list)
        or len(actions) != len(set(actions))
    ):
        raise InvalidPlayError
    correct_prefix = 0
    for submitted, expected in zip(actions, challenge, strict=False):
        if submitted != expected:
            break
        correct_prefix += 1
    score = (payload.max_score * correct_prefix) // len(challenge)
    elapsed_ms = max(0, int((now - game_session.created_at).total_seconds() * 1000))
    return {
        "score": score,
        "correct_actions": correct_prefix,
        "submitted_actions": len(actions),
        "elapsed_ms": elapsed_ms,
    }


def reward_value(config: GameConfigVersion, outcome: Outcome) -> int:
    """Derive an entitlement only from an accepted outcome and its immutable config."""
    payload = game_config_adapter.validate_python(config.payload)
    if payload.game_type == "daily_spin":
        reward_key = outcome.result.get("reward_key")
        for band in payload.rewards:
            if band.key == reward_key:
                return band.value
        raise InvalidPlayError
    if payload.game_type == "prediction_card":
        return payload.correct_reward if outcome.result.get("correct") is True else 0
    if payload.game_type == "skill_check":
        score = outcome.result.get("score")
        if (
            not isinstance(score, int)
            or isinstance(score, bool)
            or not 0 <= score <= payload.max_score
        ):
            raise InvalidPlayError
        return score
    raise InvalidPlayError


def claim_reward(reward: Reward, now: datetime) -> bool:
    """Apply the issued-to-claimed transition; return false for an idempotent retry."""
    if reward.status == RewardStatus.CLAIMED:
        return False
    if reward.status != RewardStatus.ISSUED:
        raise RewardUnavailableError
    reward.status = RewardStatus.CLAIMED
    reward.claimed_at = now
    return True


async def play_session(
    session: AsyncSession,
    *,
    session_id: uuid.UUID,
    owner_id: uuid.UUID,
    choice: str | None,
    actions: list[int] | None,
    provider: OutcomeProvider,
    clock: Callable[[], datetime] = utc_now,
) -> Outcome:
    game_session = await repositories.lock_session(session, session_id)
    if game_session is None:
        raise NotFoundError
    if game_session.player_id != owner_id:
        raise ForbiddenError
    now = clock()
    if expire_if_due(game_session, now):
        await session.commit()
        raise SessionExpiredError
    if game_session.status != SessionStatus.ACTIVE:
        raise InvalidTransitionError
    game = await repositories.get_game_by_id(session, game_session.game_id)
    config = await repositories.get_config_by_id(session, game_session.config_version_id)
    if game is None or config is None or config.game_id != game.id:
        raise NotFoundError
    if game.key == "daily_spin":
        if choice is not None or actions is not None:
            raise InvalidPlayError
        result = _daily_spin_result(game_session, game.key, config, provider)
    elif game.key == "prediction_card":
        if actions is not None:
            raise InvalidPlayError
        result = _prediction_result(game_session, game.key, config, provider, choice)
    elif game.key == "skill_check":
        if choice is not None:
            raise InvalidPlayError
        result = _skill_result(game_session, config, actions, now)
    else:
        raise InvalidPlayError
    outcome = await repositories.add_outcome(session, game_session, result)
    reward = await repositories.add_reward(
        session, outcome, player_id=owner_id, value=reward_value(config, outcome)
    )
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
    return outcome


async def claim_session_reward(
    session: AsyncSession,
    *,
    session_id: uuid.UUID,
    owner_id: uuid.UUID,
    clock: Callable[[], datetime] = utc_now,
) -> Reward:
    reward = await repositories.lock_reward_by_session(session, session_id)
    if reward is None:
        game_session = await repositories.get_session(session, session_id)
        if game_session is None:
            raise NotFoundError
        if game_session.player_id != owner_id:
            raise ForbiddenError
        raise RewardUnavailableError
    if reward.player_id != owner_id:
        raise ForbiddenError
    changed = claim_reward(reward, clock())
    if changed:
        game_key = await repositories.game_key_for_reward(session, reward.id)
        if game_key is None:
            raise NotFoundError
        await repositories.add_reward_evidence(
            session, reward, event_type="reward_claimed", game_key=game_key
        )
    await session.commit()
    return reward


async def player_rewards(
    session: AsyncSession, *, player_id: uuid.UUID, owner_id: uuid.UUID, limit: int, offset: int
) -> list[Reward]:
    if player_id != owner_id:
        raise ForbiddenError
    if await repositories.get_player(session, player_id) is None:
        raise NotFoundError
    return await repositories.list_player_rewards(session, player_id, limit, offset)


def validate_analytics_range(start_at: datetime | None, end_at: datetime | None) -> None:
    """Require aware timestamps and a non-empty [start, end) interval."""
    for boundary in (start_at, end_at):
        if boundary is not None and boundary.utcoffset() is None:
            raise InvalidAnalyticsRangeError
    if start_at is not None and end_at is not None and start_at >= end_at:
        raise InvalidAnalyticsRangeError


async def analytics_game_summary(
    session: AsyncSession,
    *,
    owner_id: uuid.UUID,
    start_at: datetime | None,
    end_at: datetime | None,
    game_key: str | None,
) -> list[repositories.GameSummaryRow]:
    validate_analytics_range(start_at, end_at)
    if await repositories.get_player(session, owner_id) is None:
        raise NotFoundError
    return await repositories.game_summary(
        session, player_id=owner_id, start_at=start_at, end_at=end_at, game_key=game_key
    )


async def retrieve_outcome_audit(
    session: AsyncSession, outcome_id: uuid.UUID, owner_id: uuid.UUID
) -> tuple[Outcome, dict[str, object]]:
    outcome = await repositories.get_outcome(session, outcome_id)
    if outcome is None:
        raise NotFoundError
    game_session = await repositories.get_session(session, outcome.session_id)
    if game_session is None:
        raise NotFoundError
    if game_session.player_id != owner_id:
        raise ForbiddenError
    audit = await repositories.get_outcome_audit(session, outcome_id)
    if audit is None:
        raise NotFoundError
    return outcome, audit.evidence


async def retrieve_session(
    session: AsyncSession,
    session_id: uuid.UUID,
    owner_id: uuid.UUID,
    clock: Callable[[], datetime] = utc_now,
) -> GameSession:
    game_session = await repositories.get_session(session, session_id)
    if game_session is None:
        raise NotFoundError
    if game_session.player_id != owner_id:
        raise ForbiddenError
    if expire_if_due(game_session, clock()):
        await session.commit()
    return game_session


async def cancel_session(
    session: AsyncSession, session_id: uuid.UUID, owner_id: uuid.UUID
) -> GameSession:
    game_session = await repositories.get_session(session, session_id)
    if game_session is None:
        raise NotFoundError
    if game_session.player_id != owner_id:
        raise ForbiddenError
    cancel_active(game_session, utc_now())
    await session.commit()
    return game_session


def _fairness_event_hash(
    *,
    proof_id: uuid.UUID,
    sequence: int,
    event_type: str,
    status: FairnessProofStatus,
    created_at: datetime,
    previous_evidence_hash: str | None,
) -> str:
    """Produce the frozen v1 append-only event hash from explicit UTC evidence."""

    timestamp = created_at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    evidence = (
        "RNG-GAME-PLATFORM-PVF-EVENT/1\n"
        f"proof_id={proof_id}\n"
        f"sequence={sequence}\n"
        f"event_type={event_type}\n"
        f"status={status.value}\n"
        f"created_at={timestamp}\n"
        f"previous_evidence_hash={previous_evidence_hash or ''}\n"
    )
    return hashlib.sha256(evidence.encode("ascii")).hexdigest()


def _daily_spin_fairness_bands(config: GameConfigVersion) -> tuple[FairnessRewardBand, ...]:
    payload = game_config_adapter.validate_python(config.payload)
    if payload.game_type != "daily_spin":
        raise InvalidPlayError
    return tuple(
        FairnessRewardBand(reward.key, reward.weight, reward.value) for reward in payload.rewards
    )


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
    if expire_if_due(game_session, now):
        await session.commit()
        raise SessionExpiredError
    if game_session.status != SessionStatus.ACTIVE:
        raise InvalidTransitionError
    game = await repositories.get_game_by_id(session, game_session.game_id)
    config = await repositories.get_config_by_id(session, game_session.config_version_id)
    if game is None or config is None or game.key != "daily_spin" or config.game_id != game.id:
        raise InvalidPlayError
    existing = await repositories.lock_fairness_proof_by_session(session, game_session.id)
    if existing is not None:
        await session.commit()
        return existing
    bands = _daily_spin_fairness_bands(config)
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
    event = FairnessProofEvent(
        proof_id=proof.id,
        sequence=0,
        event_type="committed",
        status=FairnessProofStatus.COMMITTED,
        previous_evidence_hash=None,
        evidence_hash=_fairness_event_hash(
            proof_id=proof.id,
            sequence=0,
            event_type="committed",
            status=FairnessProofStatus.COMMITTED,
            created_at=now,
            previous_evidence_hash=None,
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

    proof = await repositories.lock_fairness_proof(session, proof_id)
    if proof is None:
        raise NotFoundError
    if proof.player_id != owner_id:
        raise ForbiddenError
    if proof.status == FairnessProofStatus.REVEALED:
        if proof.outcome_id is None:
            raise InvalidTransitionError
        outcome = await repositories.get_outcome(session, proof.outcome_id)
        reward = await repositories.lock_reward_by_session(session, proof.session_id)
        if outcome is None or reward is None:
            raise NotFoundError
        await session.commit()
        return outcome, reward, proof
    game_session = await repositories.lock_session(session, proof.session_id)
    if game_session is None:
        raise NotFoundError
    now = clock()
    if expire_if_due(game_session, now):
        await session.commit()
        raise SessionExpiredError
    if proof.status != FairnessProofStatus.COMMITTED or game_session.status != SessionStatus.ACTIVE:
        raise InvalidTransitionError
    game = await repositories.get_game_by_id(session, proof.game_id)
    config = await repositories.get_config_by_id(session, proof.config_version_id)
    custody = await repositories.get_fairness_seed_custody(session, proof.id)
    if game is None or config is None or custody is None or game.key != "daily_spin":
        raise NotFoundError
    try:
        derivation = derive_daily_spin(
            server_seed=custody.server_seed_material,
            client_seed=client_seed,
            nonce=proof.nonce,
            config_version_id=proof.config_version_id,
            session_id=proof.session_id,
            reward_bands=_daily_spin_fairness_bands(config),
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
    proof.raw_random_value = derivation.raw_digest_hex
    proof.normalized_value = derivation.normalized_value
    proof.derivation_attempt = derivation.attempt
    proof.reward_key = derivation.reward.key
    proof.reward_value = derivation.reward.value
    proof.evaluated_at = now
    proof.server_seed_revealed = custody.server_seed_material.hex()
    proof.revealed_at = now
    event = FairnessProofEvent(
        proof_id=proof.id,
        sequence=1,
        event_type="revealed",
        status=FairnessProofStatus.REVEALED,
        previous_evidence_hash=_fairness_event_hash(
            proof_id=proof.id,
            sequence=0,
            event_type="committed",
            status=FairnessProofStatus.COMMITTED,
            created_at=proof.created_at,
            previous_evidence_hash=None,
        ),
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


async def retrieve_fairness_proof(
    session: AsyncSession, *, outcome_id: uuid.UUID, owner_id: uuid.UUID
) -> tuple[FairnessProof, tuple[FairnessRewardBand, ...]]:
    """Return only finalized proof evidence owned by the requesting player."""

    outcome = await repositories.get_outcome(session, outcome_id)
    if outcome is None:
        raise NotFoundError
    game_session = await repositories.get_session(session, outcome.session_id)
    if game_session is None:
        raise NotFoundError
    if game_session.player_id != owner_id:
        raise ForbiddenError
    proof = await repositories.get_fairness_proof_by_outcome(session, outcome.id)
    if proof is None:
        raise NotFoundError
    if (
        proof.status != FairnessProofStatus.REVEALED
        or proof.server_seed_revealed is None
        or proof.client_seed is None
        or proof.raw_random_value is None
        or proof.normalized_value is None
        or proof.derivation_attempt is None
        or proof.reward_key is None
        or proof.reward_value is None
        or proof.revealed_at is None
    ):
        raise InvalidTransitionError
    config = await repositories.get_config_by_id(session, proof.config_version_id)
    if config is None:
        raise NotFoundError
    return proof, _daily_spin_fairness_bands(config)


async def verify_fairness_proof(
    session: AsyncSession, *, outcome_id: uuid.UUID, owner_id: uuid.UUID
) -> tuple[FairnessProof, tuple[FairnessRewardBand, ...], str, bool]:
    """Independently recalculate every stored finalized proof field."""

    proof, bands = await retrieve_fairness_proof(session, outcome_id=outcome_id, owner_id=owner_id)
    if (
        proof.server_seed_revealed is None
        or proof.client_seed is None
        or proof.raw_random_value is None
        or proof.normalized_value is None
        or proof.derivation_attempt is None
        or proof.reward_key is None
        or proof.reward_value is None
    ):
        raise InvalidTransitionError
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
    return proof, bands, result.code, result.verified
