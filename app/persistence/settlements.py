"""Database queries; repositories flush but never commit."""

import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    Reward,
    RewardStatus,
    RewardTierConfig,
    SettlementRecipient,
    SettlementRun,
)


async def settlement_tier_config(
    session: AsyncSession, *, game_id: uuid.UUID, period_end: datetime
) -> RewardTierConfig | None:
    config: RewardTierConfig | None = await session.scalar(
        select(RewardTierConfig)
        .where(
            RewardTierConfig.game_id == game_id,
            RewardTierConfig.published_at <= period_end,
        )
        .order_by(RewardTierConfig.version.desc())
        .limit(1)
    )
    return config


async def settlement_run(
    session: AsyncSession, *, game_id: uuid.UUID, period_start: datetime
) -> SettlementRun | None:
    run: SettlementRun | None = await session.scalar(
        select(SettlementRun).where(
            SettlementRun.game_id == game_id,
            SettlementRun.period_start == period_start,
        )
    )
    return run


async def settlement_recipients(
    session: AsyncSession, run_id: uuid.UUID
) -> list[SettlementRecipient]:
    return list(
        await session.scalars(
            select(SettlementRecipient)
            .where(SettlementRecipient.run_id == run_id)
            .order_by(SettlementRecipient.rank)
        )
    )


async def settlement_reward(session: AsyncSession, recipient_id: uuid.UUID) -> Reward | None:
    """Return an existing settlement reward for interruption-safe resumption."""

    reward: Reward | None = await session.scalar(
        select(Reward).where(Reward.settlement_recipient_id == recipient_id)
    )
    return reward


async def add_settlement_reward(session: AsyncSession, recipient: SettlementRecipient) -> Reward:
    reward = Reward(
        outcome_id=None,
        settlement_recipient_id=recipient.id,
        player_id=recipient.player_id,
        status=RewardStatus.ISSUED,
        value=recipient.reward_value,
        claimed_at=None,
    )
    session.add(reward)
    await session.flush()
    await session.refresh(reward)
    return reward
