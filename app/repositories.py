"""Database queries; repositories flush but never commit."""

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import Integer, and_, cast, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    AnalyticsEvent,
    AuditRecord,
    ConfigStatus,
    FairnessProof,
    FairnessProofEvent,
    FairnessSeedCustody,
    FinalScore,
    Game,
    GameConfigVersion,
    GameSession,
    LeaderboardProjectionRevision,
    Outcome,
    OutcomeStatus,
    Player,
    Reward,
    RewardStatus,
    RewardTierConfig,
    SessionStatus,
    SettlementRecipient,
    SettlementRun,
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
    session: AsyncSession, request_id: uuid.UUID
) -> GameSession | None:
    game_session: GameSession | None = await session.scalar(
        select(GameSession).where(GameSession.request_id == request_id)
    )
    return game_session


async def lock_session_request_id(session: AsyncSession, request_id: uuid.UUID) -> None:
    """Serialize globally scoped session idempotency keys across different players."""

    lock_key = int.from_bytes(request_id.bytes[:8], byteorder="big", signed=True)
    await session.scalar(select(func.pg_advisory_xact_lock(lock_key)))


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
    request_fingerprint: str,
    expires_at: datetime,
    challenge: dict[str, object],
) -> GameSession:
    game_session = GameSession(
        request_id=request_id,
        player_id=player_id,
        game_id=game_id,
        config_version_id=config_version_id,
        request_fingerprint=request_fingerprint,
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


async def get_outcome_by_session(session: AsyncSession, session_id: uuid.UUID) -> Outcome | None:
    outcome: Outcome | None = await session.scalar(
        select(Outcome).where(Outcome.session_id == session_id)
    )
    return outcome


async def get_final_score_by_session(
    session: AsyncSession, session_id: uuid.UUID
) -> FinalScore | None:
    score: FinalScore | None = await session.scalar(
        select(FinalScore).where(FinalScore.session_id == session_id)
    )
    return score


async def add_final_score(session: AsyncSession, score: FinalScore) -> FinalScore:
    session.add(score)
    await session.flush()
    await session.refresh(score)
    return score


async def add_final_score_evidence(
    session: AsyncSession, score: FinalScore, *, game_key: str
) -> None:
    """Append score audit and analytics evidence in the score transaction."""
    evidence = {
        "score_id": str(score.id),
        "session_id": str(score.session_id),
        "outcome_id": str(score.outcome_id),
        "player_id": str(score.player_id),
        "game_key": game_key,
        "config_version_id": str(score.config_version_id),
        "period_start": score.period_start.isoformat(),
        "completed_at": score.completed_at.isoformat(),
        "final_score": score.final_score,
    }
    session.add(
        AuditRecord(
            event_type="final_score_submitted",
            entity_type="final_score",
            entity_id=score.id,
            evidence=evidence,
        )
    )
    session.add(
        AnalyticsEvent(
            event_key=f"final_score_submitted:{score.id}",
            event_type="final_score_submitted",
            player_id=score.player_id,
            payload=evidence,
        )
    )


def _ranking_order() -> tuple[object, ...]:
    return (
        FinalScore.final_score.desc(),
        FinalScore.completed_at.asc(),
        FinalScore.session_id.asc(),
    )


async def list_canonical_scores(
    session: AsyncSession,
    *,
    game_id: uuid.UUID,
    period_start: datetime,
    limit: int | None = None,
    offset: int = 0,
    after: tuple[int, datetime, uuid.UUID] | None = None,
) -> list[FinalScore]:
    statement = select(FinalScore).where(
        FinalScore.game_id == game_id, FinalScore.period_start == period_start
    )
    if after is not None:
        after_score, after_completed, after_session = after
        statement = statement.where(
            or_(
                FinalScore.final_score < after_score,
                and_(
                    FinalScore.final_score == after_score,
                    FinalScore.completed_at > after_completed,
                ),
                and_(
                    FinalScore.final_score == after_score,
                    FinalScore.completed_at == after_completed,
                    FinalScore.session_id > after_session,
                ),
            )
        )
    statement = statement.order_by(
        FinalScore.final_score.desc(),
        FinalScore.completed_at.asc(),
        FinalScore.session_id.asc(),
    )
    if offset:
        statement = statement.offset(offset)
    if limit is not None:
        statement = statement.limit(limit)
    return list(await session.scalars(statement))


async def count_canonical_scores(
    session: AsyncSession, *, game_id: uuid.UUID, period_start: datetime
) -> int:
    count = await session.scalar(
        select(func.count())
        .select_from(FinalScore)
        .where(FinalScore.game_id == game_id, FinalScore.period_start == period_start)
    )
    return int(count or 0)


async def leaderboard_projection_revision(
    session: AsyncSession, *, game_id: uuid.UUID, period_start: datetime
) -> int:
    """Return the durable generation for one game period."""

    revision = await session.scalar(
        select(LeaderboardProjectionRevision.revision).where(
            LeaderboardProjectionRevision.game_id == game_id,
            LeaderboardProjectionRevision.period_start == period_start,
        )
    )
    return int(revision or 0)


async def player_canonical_rank(
    session: AsyncSession,
    *,
    player_id: uuid.UUID,
    game_id: uuid.UUID,
    period_start: datetime,
) -> tuple[FinalScore, int] | None:
    ranked = (
        select(
            FinalScore.id.label("score_id"),
            func.row_number()
            .over(
                order_by=(
                    FinalScore.final_score.desc(),
                    FinalScore.completed_at.asc(),
                    FinalScore.session_id.asc(),
                )
            )
            .label("rank"),
        )
        .where(FinalScore.game_id == game_id, FinalScore.period_start == period_start)
        .subquery()
    )
    row = (
        await session.execute(
            select(FinalScore, ranked.c.rank)
            .join(ranked, ranked.c.score_id == FinalScore.id)
            .where(FinalScore.player_id == player_id)
            .order_by(ranked.c.rank)
            .limit(1)
        )
    ).first()
    if row is None:
        return None
    return row[0], int(row[1])


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


async def list_fairness_proof_events(
    session: AsyncSession, proof_id: uuid.UUID
) -> list[FairnessProofEvent]:
    """Load a proof's append-only lifecycle chain in verification order."""

    events = await session.scalars(
        select(FairnessProofEvent)
        .where(FairnessProofEvent.proof_id == proof_id)
        .order_by(FairnessProofEvent.sequence)
    )
    return list(events)


async def lock_fairness_proof(session: AsyncSession, proof_id: uuid.UUID) -> FairnessProof | None:
    """Lock one proof by its public identifier."""

    proof: FairnessProof | None = await session.scalar(
        select(FairnessProof).where(FairnessProof.id == proof_id).with_for_update()
    )
    return proof


async def get_fairness_proof(
    session: AsyncSession, proof_id: uuid.UUID
) -> tuple[uuid.UUID, uuid.UUID] | None:
    """Load one proof for authorization before acquiring lifecycle locks in order."""

    row = (
        await session.execute(
            select(FairnessProof.player_id, FairnessProof.session_id).where(
                FairnessProof.id == proof_id
            )
        )
    ).one_or_none()
    if row is None:
        return None
    return row.player_id, row.session_id


async def get_fairness_proof_by_outcome(
    session: AsyncSession, outcome_id: uuid.UUID
) -> FairnessProof | None:
    """Load the unique proof bound to an authoritative outcome."""

    proof: FairnessProof | None = await session.scalar(
        select(FairnessProof).where(FairnessProof.outcome_id == outcome_id)
    )
    return proof


async def lock_game_by_id(session: AsyncSession, game_id: uuid.UUID) -> Game | None:
    game: Game | None = await session.scalar(
        select(Game).where(Game.id == game_id).with_for_update()
    )
    return game


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
