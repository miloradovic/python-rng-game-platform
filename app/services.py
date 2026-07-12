"""Business use cases and explicit transaction boundaries."""

import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app import repositories
from app.models import (
    Game,
    GameConfigVersion,
    GameSession,
    Player,
    PlayerStatus,
    SessionStatus,
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
    game_session = await repositories.add_session(
        session,
        request_id=request_id,
        player_id=player.id,
        game_id=game.id,
        config_version_id=config.id,
        expires_at=now + timedelta(seconds=duration),
    )
    await repositories.add_session_audit(session, game_session)
    await session.commit()
    return game_session


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
