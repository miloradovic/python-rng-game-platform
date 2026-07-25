"""Player and game-catalogue use cases."""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app import repositories
from app.models import Game, GameConfigVersion, Player
from app.services.errors import ForbiddenError, InactiveGameError, NotFoundError


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
