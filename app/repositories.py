"""Database queries; repositories flush but never commit."""

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import Integer, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    AnalyticsEvent,
    AuditRecord,
    ConfigStatus,
    FairnessProof,
    FairnessProofEvent,
    FairnessSeedCustody,
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


@dataclass(frozen=True, slots=True)
class GameSummaryRow:
    """One deterministic per-game analytics aggregate."""

    game_key: str
    plays: int
    rewards_issued: int
    average_reward_value: float | None


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
        "outcome_id": str(reward.outcome_id),
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


async def add_game_played_event(
    session: AsyncSession, outcome: Outcome, *, player_id: uuid.UUID, game_key: str
) -> None:
    """Record a deduplicated accepted-play event in the outcome transaction."""
    session.add(
        AnalyticsEvent(
            event_key=f"game_played:{outcome.id}",
            event_type="game_played",
            player_id=player_id,
            payload={
                "outcome_id": str(outcome.id),
                "session_id": str(outcome.session_id),
                "game_key": game_key,
                "config_version_id": str(outcome.config_version_id),
            },
        )
    )


async def game_key_for_reward(session: AsyncSession, reward_id: uuid.UUID) -> str | None:
    game_key: str | None = await session.scalar(
        select(Game.key)
        .join(GameSession, GameSession.game_id == Game.id)
        .join(Outcome, Outcome.session_id == GameSession.id)
        .join(Reward, Reward.outcome_id == Outcome.id)
        .where(Reward.id == reward_id)
    )
    return game_key


async def add_session_started_event(
    session: AsyncSession, game_session: GameSession, *, game_key: str
) -> None:
    """Record a deduplicated start event without challenge or sensitive payload data."""
    session.add(
        AnalyticsEvent(
            event_key=f"session_started:{game_session.id}",
            event_type="session_started",
            player_id=game_session.player_id,
            payload={
                "session_id": str(game_session.id),
                "game_key": game_key,
                "config_version_id": str(game_session.config_version_id),
            },
        )
    )


async def game_summary(
    session: AsyncSession,
    *,
    player_id: uuid.UUID,
    start_at: datetime | None,
    end_at: datetime | None,
    game_key: str | None,
) -> list[GameSummaryRow]:
    """Aggregate authoritative player events using [start, end) time boundaries."""
    event_game = AnalyticsEvent.payload["game_key"].as_string()
    reward_value = cast(AnalyticsEvent.payload["value"].as_string(), Integer)
    statement = (
        select(
            event_game.label("game_key"),
            func.count().filter(AnalyticsEvent.event_type == "game_played").label("plays"),
            func.count()
            .filter(AnalyticsEvent.event_type == "reward_issued")
            .label("rewards_issued"),
            func.avg(reward_value)
            .filter(AnalyticsEvent.event_type == "reward_issued")
            .label("average_reward_value"),
        )
        .where(
            AnalyticsEvent.player_id == player_id,
            AnalyticsEvent.event_type.in_(("game_played", "reward_issued")),
        )
        .group_by(event_game)
        .order_by(event_game)
    )
    if start_at is not None:
        statement = statement.where(AnalyticsEvent.created_at >= start_at)
    if end_at is not None:
        statement = statement.where(AnalyticsEvent.created_at < end_at)
    if game_key is not None:
        statement = statement.where(event_game == game_key)
    rows = (await session.execute(statement)).all()
    return [
        GameSummaryRow(
            row.game_key,
            row.plays,
            row.rewards_issued,
            float(row.average_reward_value) if row.average_reward_value is not None else None,
        )
        for row in rows
    ]


async def lock_fairness_proof_by_session(
    session: AsyncSession, session_id: uuid.UUID
) -> FairnessProof | None:
    """Lock the one proof bound to a session for replay-safe evaluation."""

    proof: FairnessProof | None = await session.scalar(
        select(FairnessProof).where(FairnessProof.session_id == session_id).with_for_update()
    )
    return proof


async def add_fairness_proof(session: AsyncSession, proof: FairnessProof) -> FairnessProof:
    session.add(proof)
    await session.flush()
    await session.refresh(proof)
    return proof


async def add_fairness_seed_custody(
    session: AsyncSession, proof_id: uuid.UUID, server_seed: bytes
) -> FairnessSeedCustody:
    custody = FairnessSeedCustody(proof_id=proof_id, server_seed_material=server_seed)
    session.add(custody)
    await session.flush()
    return custody


async def get_fairness_seed_custody(
    session: AsyncSession, proof_id: uuid.UUID
) -> FairnessSeedCustody | None:
    custody: FairnessSeedCustody | None = await session.scalar(
        select(FairnessSeedCustody).where(FairnessSeedCustody.proof_id == proof_id)
    )
    return custody


async def delete_fairness_seed_custody(session: AsyncSession, proof_id: uuid.UUID) -> None:
    """Remove unrevealed seed material after a terminal non-reveal transition."""

    custody = await get_fairness_seed_custody(session, proof_id)
    if custody is not None:
        await session.delete(custody)


async def add_fairness_proof_event(session: AsyncSession, event: FairnessProofEvent) -> None:
    session.add(event)
    await session.flush()


async def lock_latest_fairness_proof_event(
    session: AsyncSession, proof_id: uuid.UUID
) -> FairnessProofEvent | None:
    """Lock the latest append-only event to extend its durable hash chain."""

    event: FairnessProofEvent | None = await session.scalar(
        select(FairnessProofEvent)
        .where(FairnessProofEvent.proof_id == proof_id)
        .order_by(FairnessProofEvent.sequence.desc())
        .limit(1)
        .with_for_update()
    )
    return event


async def lock_fairness_proof(session: AsyncSession, proof_id: uuid.UUID) -> FairnessProof | None:
    """Lock one proof by its public identifier."""

    proof: FairnessProof | None = await session.scalar(
        select(FairnessProof).where(FairnessProof.id == proof_id).with_for_update()
    )
    return proof


async def get_fairness_proof_by_outcome(
    session: AsyncSession, outcome_id: uuid.UUID
) -> FairnessProof | None:
    """Load the unique proof bound to an authoritative outcome."""

    proof: FairnessProof | None = await session.scalar(
        select(FairnessProof).where(FairnessProof.outcome_id == outcome_id)
    )
    return proof
