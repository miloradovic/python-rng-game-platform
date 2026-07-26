"""Database queries; repositories flush but never commit."""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    Player,
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
