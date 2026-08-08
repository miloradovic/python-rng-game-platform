"""Database queries; repositories flush but never commit."""

import uuid

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
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


async def provision_player(
    session: AsyncSession,
    *,
    player_id: uuid.UUID,
    display_name: str,
    public_label: str,
) -> Player:
    """Insert one trusted, stable identity or return the row already using its ID."""

    await session.execute(
        insert(Player)
        .values(id=player_id, display_name=display_name, public_label=public_label)
        .on_conflict_do_nothing(index_elements=[Player.id])
    )
    player = await session.get(Player, player_id)
    if player is None:  # pragma: no cover - the insert/select contract makes this unreachable
        raise RuntimeError("player provisioning did not produce a row")
    return player


async def get_player(session: AsyncSession, player_id: uuid.UUID) -> Player | None:
    return await session.get(Player, player_id)


async def public_labels_by_player_ids(
    session: AsyncSession, player_ids: set[uuid.UUID]
) -> dict[uuid.UUID, str]:
    """Resolve safe persisted labels without exposing private display names."""

    if not player_ids:
        return {}
    rows = await session.execute(
        select(Player.id, Player.public_label).where(Player.id.in_(player_ids))
    )
    return {player_id: public_label for player_id, public_label in rows}


async def lock_player(session: AsyncSession, player_id: uuid.UUID) -> Player | None:
    player: Player | None = await session.scalar(
        select(Player).where(Player.id == player_id).with_for_update()
    )
    return player
