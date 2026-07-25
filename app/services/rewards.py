"""Reward derivation, reads, and claims."""

import uuid
from collections.abc import Callable
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app import repositories
from app.game_rules import InvalidRulesInputError, rules_for
from app.models import GameConfigVersion, Outcome, Reward, RewardStatus
from app.schemas import game_config_adapter
from app.services._common import utc_now
from app.services.errors import (
    ForbiddenError,
    InvalidPlayError,
    NotFoundError,
    RewardUnavailableError,
)


def reward_value(config: GameConfigVersion, outcome: Outcome, game_key: str | None = None) -> int:
    """Derive an entitlement only from an accepted outcome and its immutable config."""
    payload = game_config_adapter.validate_python(config.payload)
    try:
        return rules_for(game_key or payload.game_type).reward_value(config, outcome)
    except InvalidRulesInputError as error:
        raise InvalidPlayError from error


def claim_reward(reward: Reward, now: datetime) -> bool:
    """Apply the issued-to-claimed transition; return false for an idempotent retry."""
    if reward.status == RewardStatus.CLAIMED:
        return False
    if reward.status != RewardStatus.ISSUED:
        raise RewardUnavailableError
    reward.status = RewardStatus.CLAIMED
    reward.claimed_at = now
    return True


async def claim_session_reward(
    session: AsyncSession,
    *,
    session_id: uuid.UUID,
    owner_id: uuid.UUID,
    clock: Callable[[], datetime] = utc_now,
) -> Reward:
    reward = await repositories.lock_reward_by_session(session, session_id)
    if reward is None:
        game_session = await repositories.get_session(session, session_id)
        if game_session is None:
            raise NotFoundError
        if game_session.player_id != owner_id:
            raise ForbiddenError
        raise RewardUnavailableError
    if reward.player_id != owner_id:
        raise ForbiddenError
    changed = claim_reward(reward, clock())
    if changed:
        game_key = await repositories.game_key_for_reward(session, reward.id)
        if game_key is None:
            raise NotFoundError
        await repositories.add_reward_evidence(
            session, reward, event_type="reward_claimed", game_key=game_key
        )
    await session.commit()
    return reward


async def player_rewards(
    session: AsyncSession, *, player_id: uuid.UUID, owner_id: uuid.UUID, limit: int, offset: int
) -> list[Reward]:
    if player_id != owner_id:
        raise ForbiddenError
    if await repositories.get_player(session, player_id) is None:
        raise NotFoundError
    return await repositories.list_player_rewards(session, player_id, limit, offset)
