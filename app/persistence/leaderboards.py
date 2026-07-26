"""Database queries; repositories flush but never commit."""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from app.models import (
    AnalyticsEvent,
    AuditRecord,
    FinalScore,
    LeaderboardProjectionRevision,
)


def _ranking_order() -> tuple[ColumnElement[Any], ...]:
    return (
        FinalScore.final_score.desc(),
        FinalScore.completed_at.asc(),
        FinalScore.session_id.asc(),
    )


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


async def list_settlement_scores(
    session: AsyncSession,
    *,
    game_id: uuid.UUID,
    period_start: datetime,
) -> list[FinalScore]:
    """Return each player's deterministic best score in canonical rank order."""

    ranked_for_player = (
        select(
            FinalScore.id.label("score_id"),
            func.row_number()
            .over(
                partition_by=FinalScore.player_id,
                order_by=_ranking_order(),
            )
            .label("player_score_rank"),
        )
        .where(FinalScore.game_id == game_id, FinalScore.period_start == period_start)
        .subquery()
    )
    return list(
        await session.scalars(
            select(FinalScore)
            .join(ranked_for_player, ranked_for_player.c.score_id == FinalScore.id)
            .where(ranked_for_player.c.player_score_rank == 1)
            .order_by(*_ranking_order())
        )
    )


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
