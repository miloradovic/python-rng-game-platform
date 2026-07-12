"""Database queries; repositories flush but never commit."""

import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    AuditRecord,
    ConfigStatus,
    Game,
    GameConfigVersion,
    GameSession,
    Player,
    SessionStatus,
)


async def add_player(session: AsyncSession, display_name: str) -> Player:
    player = Player(display_name=display_name)
    session.add(player)
    await session.flush()
    await session.refresh(player)
    return player


async def get_player(session: AsyncSession, player_id: uuid.UUID) -> Player | None:
    return await session.get(Player, player_id)


async def lock_player(session: AsyncSession, player_id: uuid.UUID) -> Player | None:
    player: Player | None = await session.scalar(
        select(Player).where(Player.id == player_id).with_for_update()
    )
    return player


async def list_games(session: AsyncSession, limit: int, offset: int) -> list[Game]:
    result = await session.scalars(select(Game).order_by(Game.key).limit(limit).offset(offset))
    return list(result)


async def get_game(session: AsyncSession, game_key: str) -> Game | None:
    game: Game | None = await session.scalar(select(Game).where(Game.key == game_key))
    return game


async def get_active_config(session: AsyncSession, game_id: uuid.UUID) -> GameConfigVersion | None:
    config: GameConfigVersion | None = await session.scalar(
        select(GameConfigVersion).where(
            GameConfigVersion.game_id == game_id,
            GameConfigVersion.status == ConfigStatus.PUBLISHED,
        )
    )
    return config


async def get_session_by_request(
    session: AsyncSession, player_id: uuid.UUID, request_id: uuid.UUID
) -> GameSession | None:
    game_session: GameSession | None = await session.scalar(
        select(GameSession).where(
            GameSession.player_id == player_id, GameSession.request_id == request_id
        )
    )
    return game_session


async def expire_active_sessions(
    session: AsyncSession, player_id: uuid.UUID, game_id: uuid.UUID, now: datetime
) -> None:
    sessions = await session.scalars(
        select(GameSession).where(
            GameSession.player_id == player_id,
            GameSession.game_id == game_id,
            GameSession.status == SessionStatus.ACTIVE,
            GameSession.expires_at <= now,
        )
    )
    for game_session in sessions:
        game_session.status = SessionStatus.EXPIRED
        game_session.ended_at = now


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
    expires_at: datetime,
) -> GameSession:
    game_session = GameSession(
        request_id=request_id,
        player_id=player_id,
        game_id=game_id,
        config_version_id=config_version_id,
        status=SessionStatus.ACTIVE,
        expires_at=expires_at,
        ended_at=None,
    )
    session.add(game_session)
    await session.flush()
    await session.refresh(game_session)
    return game_session


async def get_session(session: AsyncSession, session_id: uuid.UUID) -> GameSession | None:
    return await session.get(GameSession, session_id)


async def add_session_audit(session: AsyncSession, game_session: GameSession) -> None:
    session.add(
        AuditRecord(
            event_type="session_started",
            entity_type="session",
            entity_id=game_session.id,
            evidence={
                "player_id": str(game_session.player_id),
                "game_id": str(game_session.game_id),
                "config_version_id": str(game_session.config_version_id),
                "request_id": str(game_session.request_id),
                "expires_at": game_session.expires_at.isoformat(),
            },
        )
    )
