"""Database queries; repositories flush but never commit."""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    AnalyticsEvent,
    AuditRecord,
    GameSession,
    Outcome,
    OutcomeStatus,
    Reward,
    RewardStatus,
)


async def get_outcome_by_session(session: AsyncSession, session_id: uuid.UUID) -> Outcome | None:
    outcome: Outcome | None = await session.scalar(
        select(Outcome).where(Outcome.session_id == session_id)
    )
    return outcome


async def add_outcome(
    session: AsyncSession,
    game_session: GameSession,
    result: dict[str, object],
) -> Outcome:
    outcome = Outcome(
        session_id=game_session.id,
        player_id=game_session.player_id,
        game_id=game_session.game_id,
        config_version_id=game_session.config_version_id,
        status=OutcomeStatus.ACCEPTED,
        result=result,
    )
    session.add(outcome)
    await session.flush()
    await session.refresh(outcome)
    return outcome


async def add_reward(
    session: AsyncSession, outcome: Outcome, *, player_id: uuid.UUID, value: int
) -> Reward:
    """Issue exactly one durable entitlement for an accepted outcome."""
    reward = Reward(
        outcome_id=outcome.id,
        player_id=player_id,
        status=RewardStatus.ISSUED,
        value=value,
        claimed_at=None,
    )
    session.add(reward)
    await session.flush()
    await session.refresh(reward)
    return reward


async def lock_reward_by_session(session: AsyncSession, session_id: uuid.UUID) -> Reward | None:
    reward: Reward | None = await session.scalar(
        select(Reward)
        .join(Outcome, Outcome.id == Reward.outcome_id)
        .where(Outcome.session_id == session_id)
        .with_for_update()
    )
    return reward


async def get_reward_by_session(session: AsyncSession, session_id: uuid.UUID) -> Reward | None:
    """Load the unique outcome reward for a session without taking a write lock."""

    reward: Reward | None = await session.scalar(
        select(Reward)
        .join(Outcome, Outcome.id == Reward.outcome_id)
        .where(Outcome.session_id == session_id)
    )
    return reward


async def list_player_rewards(
    session: AsyncSession, player_id: uuid.UUID, limit: int, offset: int
) -> list[Reward]:
    rewards = await session.scalars(
        select(Reward)
        .where(Reward.player_id == player_id)
        .order_by(Reward.created_at.desc(), Reward.id.desc())
        .limit(limit)
        .offset(offset)
    )
    return list(rewards)


async def add_outcome_audit(
    session: AsyncSession,
    outcome: Outcome,
    *,
    player_id: uuid.UUID,
    game_key: str,
) -> None:
    session.add(
        AuditRecord(
            event_type="outcome_accepted",
            entity_type="outcome",
            entity_id=outcome.id,
            evidence={
                "session_id": str(outcome.session_id),
                "player_id": str(player_id),
                "game_key": game_key,
                "config_version_id": str(outcome.config_version_id),
                "result": outcome.result,
            },
        )
    )


async def get_outcome(session: AsyncSession, outcome_id: uuid.UUID) -> Outcome | None:
    return await session.get(Outcome, outcome_id)


async def get_outcome_audit(session: AsyncSession, outcome_id: uuid.UUID) -> AuditRecord | None:
    audit: AuditRecord | None = await session.scalar(
        select(AuditRecord).where(
            AuditRecord.entity_type == "outcome",
            AuditRecord.entity_id == outcome_id,
            AuditRecord.event_type == "outcome_accepted",
        )
    )
    return audit


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


async def add_reward_evidence(
    session: AsyncSession,
    reward: Reward,
    *,
    event_type: str,
    game_key: str,
) -> None:
    """Append audit and uniquely keyed analytics evidence in the business transaction."""
    evidence = {
        "reward_id": str(reward.id),
        "outcome_id": str(reward.outcome_id) if reward.outcome_id is not None else None,
        "settlement_recipient_id": (
            str(reward.settlement_recipient_id)
            if reward.settlement_recipient_id is not None
            else None
        ),
        "player_id": str(reward.player_id),
        "value": reward.value,
        "status": reward.status.value,
        "game_key": game_key,
    }
    session.add(
        AuditRecord(
            event_type=event_type, entity_type="reward", entity_id=reward.id, evidence=evidence
        )
    )
    session.add(
        AnalyticsEvent(
            event_key=f"{event_type}:{reward.id}",
            event_type=event_type,
            player_id=reward.player_id,
            payload=evidence,
        )
    )
