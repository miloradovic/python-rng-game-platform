"""Validated selection between canonical and disposable leaderboard reads."""

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app import services
from app.cache import (
    ProjectionIntegrity,
    projected_page,
    projected_player_rank,
    projection_integrity,
)
from app.config import Settings
from app.models import FinalScore
from app.observability import MetricsRegistry
from app.schemas import LeaderboardEntry


@dataclass(frozen=True, slots=True)
class LeaderboardPage:
    """One transport-ready page selected by the projection policy."""

    source: str
    items: list[LeaderboardEntry]
    has_more: bool


@dataclass(frozen=True, slots=True)
class LeaderboardRank:
    """One transport-ready player rank selected by the projection policy."""

    source: str
    entry: LeaderboardEntry


def _database_entry(
    score: FinalScore, rank: int, public_label: str, owner_id: UUID
) -> LeaderboardEntry:
    return LeaderboardEntry(
        rank=rank,
        score_id=score.id,
        session_id=score.session_id,
        public_label=public_label,
        is_current_player=score.player_id == owner_id,
        final_score=score.final_score,
        completed_at=score.completed_at,
    )


def _projected_entry(
    row: tuple[int, str, str, str, int, int],
    public_label: str,
    owner_id: UUID,
) -> LeaderboardEntry:
    completed_us, session_id, score_id, player_id, score, rank = row
    return LeaderboardEntry(
        rank=rank,
        score_id=UUID(score_id),
        session_id=UUID(session_id),
        public_label=public_label,
        is_current_player=UUID(player_id) == owner_id,
        final_score=score,
        completed_at=datetime.fromtimestamp(completed_us / 1_000_000, UTC),
    )


class LeaderboardProjectionReader:
    """Use Redis only after PostgreSQL validates its complete generation."""

    def __init__(
        self,
        client: Redis | None,
        integrity: ProjectionIntegrity | None,
        metrics: MetricsRegistry | None = None,
    ) -> None:
        self._client = client
        self._integrity = integrity
        self._metrics = metrics

    @classmethod
    def from_settings(
        cls,
        client: Redis | None,
        settings: Settings,
        metrics: MetricsRegistry | None = None,
    ) -> LeaderboardProjectionReader:
        return cls(client, projection_integrity(settings), metrics)

    def _record_source(self, source: str) -> None:
        if self._metrics is not None:
            self._metrics.increment("leaderboard_projection_reads_total", source=source)
            if source == "postgresql" and self._client is not None:
                self._metrics.increment("redis_fallback_total", operation="leaderboard_read")

    async def page(
        self,
        session: AsyncSession,
        *,
        owner_id: UUID,
        game_key: str,
        period_start: datetime,
        offset: int,
        limit: int,
    ) -> LeaderboardPage:
        _, count, revision = await services.leaderboard_projection_facts(
            session,
            owner_id=owner_id,
            game_key=game_key,
            period_start=period_start,
        )
        projected = await projected_page(
            self._client,
            self._integrity,
            game_key=game_key,
            period_start=period_start.strftime("%Y%m%dT%H%M%SZ"),
            offset=offset,
            limit=limit + 1,
            expected_count=count,
            expected_revision=revision,
        )
        if projected is not None:
            try:
                labels = await services.public_leaderboard_labels(
                    session, {UUID(row[3]) for row in projected}
                )
                selected = [
                    _projected_entry(row, labels[UUID(row[3])], owner_id) for row in projected
                ]
            except OverflowError, TypeError, ValueError:
                selected = []
                projected = None
        if projected is None:
            _, scores = await services.canonical_leaderboard(
                session,
                owner_id=owner_id,
                game_key=game_key,
                period_start=period_start,
                limit=limit + 1,
                offset=offset,
            )
            labels = await services.public_leaderboard_labels(
                session, {score.player_id for score in scores}
            )
            selected = [
                _database_entry(score, offset + index + 1, labels[score.player_id], owner_id)
                for index, score in enumerate(scores)
            ]
            source = "postgresql"
        else:
            source = "redis"
        self._record_source(source)
        return LeaderboardPage(
            source=source,
            items=selected[:limit],
            has_more=len(selected) > limit,
        )

    async def player_rank(
        self,
        session: AsyncSession,
        *,
        player_id: UUID,
        owner_id: UUID,
        game_key: str,
        period_start: datetime,
    ) -> LeaderboardRank:
        _, count, revision = await services.leaderboard_projection_facts(
            session,
            owner_id=owner_id,
            player_id=player_id,
            game_key=game_key,
            period_start=period_start,
        )
        projected = await projected_player_rank(
            self._client,
            self._integrity,
            game_key=game_key,
            period_start=period_start.strftime("%Y%m%dT%H%M%SZ"),
            player_id=str(player_id),
            expected_count=count,
            expected_revision=revision,
        )
        if projected is not None:
            try:
                projected_player_id = UUID(projected[3])
                labels = await services.public_leaderboard_labels(session, {projected_player_id})
                entry = _projected_entry(projected, labels[projected_player_id], owner_id)
            except OverflowError, TypeError, ValueError:
                projected = None
        if projected is None:
            score, rank = await services.canonical_player_rank(
                session,
                player_id=player_id,
                owner_id=owner_id,
                game_key=game_key,
                period_start=period_start,
            )
            labels = await services.public_leaderboard_labels(session, {score.player_id})
            entry = _database_entry(score, rank, labels[score.player_id], owner_id)
            source = "postgresql"
        else:
            source = "redis"
        self._record_source(source)
        return LeaderboardRank(source=source, entry=entry)
