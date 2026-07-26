"""Database queries; repositories flush but never commit."""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    ConfigStatus,
    Game,
    GameConfigVersion,
)


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


async def get_game_by_id(session: AsyncSession, game_id: uuid.UUID) -> Game | None:
    return await session.get(Game, game_id)


async def get_config_by_id(
    session: AsyncSession, config_version_id: uuid.UUID
) -> GameConfigVersion | None:
    return await session.get(GameConfigVersion, config_version_id)


async def lock_game_by_id(session: AsyncSession, game_id: uuid.UUID) -> Game | None:
    game: Game | None = await session.scalar(
        select(Game).where(Game.id == game_id).with_for_update()
    )
    return game
