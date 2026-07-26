"""Database queries; repositories flush but never commit."""

import uuid
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    GameSession,
    SessionStatus,
)


async def get_session_by_request(
    session: AsyncSession, request_id: uuid.UUID
) -> GameSession | None:
    game_session: GameSession | None = await session.scalar(
        select(GameSession).where(GameSession.request_id == request_id)
    )
    return game_session


async def lock_session_request_id(session: AsyncSession, request_id: uuid.UUID) -> None:
    """Serialize globally scoped session idempotency keys across different players."""

    lock_key = int.from_bytes(request_id.bytes[:8], byteorder="big", signed=True)
    await session.scalar(select(func.pg_advisory_xact_lock(lock_key)))


async def lock_expired_active_sessions(
    session: AsyncSession, player_id: uuid.UUID, game_id: uuid.UUID, now: datetime
) -> list[GameSession]:
    """Lock elapsed active sessions so services can finalize dependent state atomically."""

    sessions = await session.scalars(
        select(GameSession)
        .where(
            GameSession.player_id == player_id,
            GameSession.game_id == game_id,
            GameSession.status == SessionStatus.ACTIVE,
            GameSession.expires_at <= now,
        )
        .with_for_update()
    )
    return list(sessions)


async def latest_session(
    session: AsyncSession, player_id: uuid.UUID, game_id: uuid.UUID
) -> GameSession | None:
    game_session: GameSession | None = await session.scalar(
        select(GameSession)
        .where(GameSession.player_id == player_id, GameSession.game_id == game_id)
        .order_by(GameSession.created_at.desc())
        .limit(1)
    )
    return game_session


async def add_session(
    session: AsyncSession,
    *,
    request_id: uuid.UUID,
    player_id: uuid.UUID,
    game_id: uuid.UUID,
    config_version_id: uuid.UUID,
    request_fingerprint: str,
    expires_at: datetime,
    challenge: dict[str, object],
) -> GameSession:
    game_session = GameSession(
        request_id=request_id,
        player_id=player_id,
        game_id=game_id,
        config_version_id=config_version_id,
        request_fingerprint=request_fingerprint,
        status=SessionStatus.ACTIVE,
        expires_at=expires_at,
        ended_at=None,
        challenge=challenge,
    )
    session.add(game_session)
    await session.flush()
    await session.refresh(game_session)
    return game_session


async def get_session(session: AsyncSession, session_id: uuid.UUID) -> GameSession | None:
    return await session.get(GameSession, session_id)


async def lock_session(session: AsyncSession, session_id: uuid.UUID) -> GameSession | None:
    game_session: GameSession | None = await session.scalar(
        select(GameSession).where(GameSession.id == session_id).with_for_update()
    )
    return game_session
