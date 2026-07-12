"""Database queries; repositories flush but never commit."""

import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    AnalyticsEvent,
    AuditRecord,
    ConfigStatus,
    Game,
    GameConfigVersion,
    GameSession,
    Outcome,
    OutcomeStatus,
    Player,
    Reward,
    RewardStatus,
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
    challenge: dict[str, object],
) -> GameSession:
    game_session = GameSession(
        request_id=request_id,
        player_id=player_id,
        game_id=game_id,
        config_version_id=config_version_id,
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


async def get_game_by_id(session: AsyncSession, game_id: uuid.UUID) -> Game | None:
    return await session.get(Game, game_id)


async def get_config_by_id(
    session: AsyncSession, config_version_id: uuid.UUID
) -> GameConfigVersion | None:
    return await session.get(GameConfigVersion, config_version_id)


async def add_outcome(
    session: AsyncSession,
    game_session: GameSession,
    result: dict[str, object],
) -> Outcome:
    outcome = Outcome(
        session_id=game_session.id,
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


async def add_reward_evidence(session: AsyncSession, reward: Reward, *, event_type: str) -> None:
    """Append audit and uniquely keyed analytics evidence in the business transaction."""
    evidence = {
        "reward_id": str(reward.id),
        "outcome_id": str(reward.outcome_id),
        "player_id": str(reward.player_id),
        "value": reward.value,
        "status": reward.status.value,
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
