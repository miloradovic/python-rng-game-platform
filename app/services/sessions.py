"""Session lifecycle commands and queries."""

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app import repositories
from app.game_rules import InvalidRulesInputError, rules_for
from app.models import (
    FairnessProof,
    FairnessProofStatus,
    FinalScore,
    GameSession,
    Outcome,
    PlayerStatus,
    Reward,
    SessionStatus,
)
from app.schemas import game_config_adapter
from app.services._common import _owned_session, _request_fingerprint, utc_now
from app.services.errors import (
    ActiveSessionError,
    CooldownError,
    ForbiddenError,
    IdempotencyConflictError,
    InactiveGameError,
    InactivePlayerError,
    InvalidPlayError,
    NotFoundError,
)
from app.services.session_termination import (
    end_committed_fairness_proof,
    finalize_expired_session,
)
from app.session_transitions import (
    cancel_active as cancel_active,
)
from app.session_transitions import (
    expire_if_due as expire_if_due,
)


@dataclass(frozen=True, slots=True)
class PlayerGameState:
    """Owner-scoped durable recovery state for one player and game."""

    server_time: datetime
    next_play_at: datetime | None
    game_key: str
    game_session: GameSession | None
    outcome: Outcome | None
    reward: Reward | None
    fairness_proof: FairnessProof | None
    final_score: FinalScore | None


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
    request_fingerprint = _request_fingerprint(
        "create_session", {"player_id": str(player_id), "game_key": game_key}
    )
    await repositories.lock_session_request_id(session, request_id)
    player = await repositories.lock_player(session, player_id)
    if player is None:
        raise NotFoundError
    existing = await repositories.get_session_by_request(session, request_id)
    if existing is not None:
        if existing.request_fingerprint != request_fingerprint:
            raise IdempotencyConflictError
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
    expired_sessions = await repositories.lock_expired_active_sessions(
        session, player.id, game.id, now
    )
    for expired_session in expired_sessions:
        await finalize_expired_session(session, expired_session, now=now)
    latest = await repositories.latest_session(session, player.id, game.id)
    if latest is not None and latest.status == SessionStatus.ACTIVE:
        raise ActiveSessionError
    payload = game_config_adapter.validate_python(config.payload)
    if latest is not None and latest.created_at + timedelta(seconds=payload.cooldown_seconds) > now:
        raise CooldownError
    duration = getattr(payload, "duration_seconds", 300)
    try:
        challenge = rules_for(game.key).definition.create_challenge()
    except InvalidRulesInputError as error:
        raise InvalidPlayError from error
    game_session = await repositories.add_session(
        session,
        request_id=request_id,
        player_id=player.id,
        game_id=game.id,
        config_version_id=config.id,
        request_fingerprint=request_fingerprint,
        expires_at=now + timedelta(seconds=duration),
        challenge=challenge,
    )
    await repositories.add_session_audit(session, game_session)
    await repositories.add_session_started_event(session, game_session, game_key=game.key)
    await session.commit()
    return game_session


async def retrieve_session(
    session: AsyncSession,
    session_id: uuid.UUID,
    owner_id: uuid.UUID,
    clock: Callable[[], datetime] = utc_now,
) -> GameSession:
    game_session = await _owned_session(
        session, session_id=session_id, owner_id=owner_id, for_update=True
    )
    if await finalize_expired_session(session, game_session, now=clock()):
        await session.commit()
    return game_session


async def retrieve_player_game_state(
    session: AsyncSession,
    *,
    player_id: uuid.UUID,
    owner_id: uuid.UUID,
    game_key: str,
    clock: Callable[[], datetime] = utc_now,
) -> PlayerGameState:
    """Compose authoritative state needed to recover an interrupted browser operation."""

    if player_id != owner_id:
        raise ForbiddenError
    player = await repositories.lock_player(session, player_id)
    if player is None:
        raise NotFoundError
    game = await repositories.get_game(session, game_key)
    if game is None:
        raise NotFoundError
    config = await repositories.get_active_config(session, game.id)
    if config is None:
        raise NotFoundError

    now = clock()
    expired_sessions = await repositories.lock_expired_active_sessions(
        session, player.id, game.id, now
    )
    for expired_session in expired_sessions:
        await finalize_expired_session(session, expired_session, now=now)

    latest = await repositories.latest_session(session, player.id, game.id)
    payload = game_config_adapter.validate_python(config.payload)
    next_play_at = None
    if latest is not None:
        available_at = latest.created_at + timedelta(seconds=payload.cooldown_seconds)
        if available_at > now:
            next_play_at = available_at

    outcome = None
    reward = None
    fairness_proof = None
    final_score = None
    if latest is not None:
        outcome = await repositories.get_outcome_by_session(session, latest.id)
        reward = await repositories.get_reward_by_session(session, latest.id)
        fairness_proof = await repositories.get_fairness_proof_by_session(session, latest.id)
        final_score = await repositories.get_final_score_by_session(session, latest.id)

    if expired_sessions:
        await session.commit()
    return PlayerGameState(
        server_time=now,
        next_play_at=next_play_at,
        game_key=game.key,
        game_session=latest,
        outcome=outcome,
        reward=reward,
        fairness_proof=fairness_proof,
        final_score=final_score,
    )


async def cancel_session(
    session: AsyncSession, session_id: uuid.UUID, owner_id: uuid.UUID
) -> GameSession:
    game_session = await _owned_session(
        session, session_id=session_id, owner_id=owner_id, for_update=True
    )
    now = utc_now()
    if await finalize_expired_session(session, game_session, now=now):
        await session.commit()
        return game_session
    cancel_active(game_session, now)
    proof = await repositories.lock_fairness_proof_by_session(session, game_session.id)
    if proof is not None:
        await end_committed_fairness_proof(
            session, proof, status=FairnessProofStatus.CANCELLED, now=now
        )
    await session.commit()
    return game_session
