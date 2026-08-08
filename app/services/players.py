"""Player and game-catalogue use cases."""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app import repositories
from app.models import Game, GameConfigVersion, Player
from app.services.errors import (
    ForbiddenError,
    IdempotencyConflictError,
    InactiveGameError,
    NotFoundError,
)


async def create_player(session: AsyncSession, display_name: str) -> Player:
    player = await repositories.add_player(session, display_name)
    await session.commit()
    return player


async def provision_player(
    session: AsyncSession,
    *,
    player_id: uuid.UUID,
    display_name: str,
    public_label: str,
) -> tuple[Player, bool]:
    """Idempotently provision an explicitly identified player for trusted tooling."""

    existing = await repositories.get_player(session, player_id)
    player = await repositories.provision_player(
        session,
        player_id=player_id,
        display_name=display_name,
        public_label=public_label,
    )
    if player.display_name != display_name or player.public_label != public_label:
        await session.rollback()
        raise IdempotencyConflictError
    await session.commit()
    return player, existing is None


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
