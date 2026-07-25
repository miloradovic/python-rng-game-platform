"""Session lifecycle commands and queries."""

import uuid
from collections.abc import Callable
from datetime import datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app import repositories
from app.game_rules import InvalidRulesInputError, rules_for
from app.models import FairnessProof, FairnessProofStatus, GameSession, PlayerStatus, SessionStatus
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
    InvalidTransitionError,
    NotFoundError,
)


async def _end_committed_fairness_proof(
    session: AsyncSession, proof: FairnessProof, *, status: FairnessProofStatus, now: datetime
) -> None:
    from app.services.fairness import end_committed_fairness_proof

    await end_committed_fairness_proof(session, proof, status=status, now=now)


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
    await repositories.expire_active_sessions(session, player.id, game.id, now)
    latest = await repositories.latest_session(session, player.id, game.id)
    if latest is not None and latest.status == SessionStatus.ACTIVE:
        raise ActiveSessionError
    payload = game_config_adapter.validate_python(config.payload)
    if latest is not None and latest.created_at + timedelta(seconds=payload.cooldown_seconds) > now:
        raise CooldownError
    duration = getattr(payload, "duration_seconds", 300)
    try:
        challenge = rules_for(game.key).create_challenge()
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
        session, session_id=session_id, owner_id=owner_id, for_update=False
    )
    expire_if_due(game_session, clock())
    return game_session


async def cancel_session(
    session: AsyncSession, session_id: uuid.UUID, owner_id: uuid.UUID
) -> GameSession:
    game_session = await _owned_session(
        session, session_id=session_id, owner_id=owner_id, for_update=True
    )
    now = utc_now()
    cancel_active(game_session, now)
    proof = await repositories.lock_fairness_proof_by_session(session, game_session.id)
    if proof is not None:
        await _end_committed_fairness_proof(
            session, proof, status=FairnessProofStatus.CANCELLED, now=now
        )
    await session.commit()
    return game_session
