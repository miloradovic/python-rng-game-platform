"""Database queries; repositories flush but never commit."""

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import Integer, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    AnalyticsEvent,
    Game,
    GameSession,
    Outcome,
    Reward,
)


@dataclass(frozen=True, slots=True)
class GameSummaryRow:
    """One deterministic per-game analytics aggregate."""

    game_key: str
    plays: int
    rewards_issued: int
    average_reward_value: float | None


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
