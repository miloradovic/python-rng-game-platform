"""Validated selection between canonical and disposable leaderboard reads."""

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app import repositories, services
from app.cache import parse_leaderboard_member, projected_page, projected_player_rank
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


def _database_entry(score: FinalScore, rank: int) -> LeaderboardEntry:
    return LeaderboardEntry(
        rank=rank,
        score_id=score.id,
        player_id=score.player_id,
        session_id=score.session_id,
        final_score=score.final_score,
        completed_at=score.completed_at,
    )


def _projected_entry(member: str, score: int, rank: int) -> LeaderboardEntry:
    completed_us, session_id, score_id, player_id = parse_leaderboard_member(member)
    return LeaderboardEntry(
        rank=rank,
        score_id=UUID(score_id),
        player_id=UUID(player_id),
        session_id=UUID(session_id),
        final_score=score,
        completed_at=datetime.fromtimestamp(completed_us / 1_000_000, UTC),
    )


class LeaderboardProjectionReader:
    """Use Redis only when PostgreSQL proves the requested result is identical."""

    def __init__(self, client: Redis | None, metrics: MetricsRegistry | None = None) -> None:
        self._client = client
        self._metrics = metrics

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
        game, scores = await services.canonical_leaderboard(
            session,
            owner_id=owner_id,
            game_key=game_key,
            period_start=period_start,
            limit=limit + 1,
            offset=offset,
        )
        canonical = [
            _database_entry(score, offset + index + 1) for index, score in enumerate(scores)
        ]
        count = await repositories.count_canonical_scores(
            session, game_id=game.id, period_start=period_start
        )
        revision = await repositories.leaderboard_projection_revision(
            session, game_id=game.id, period_start=period_start
        )
        projected = await projected_page(
            self._client,
            game_key=game_key,
            period_start=period_start.strftime("%Y%m%dT%H%M%SZ"),
            offset=offset,
            limit=limit + 1,
            expected_count=count,
            expected_revision=revision,
        )
        try:
            projected_entries = (
                [_projected_entry(member, score, rank) for member, score, rank in projected]
                if projected is not None
                else None
            )
        except OverflowError, TypeError, ValueError:
            projected_entries = None
        use_projection = projected_entries == canonical
        selected = (
            projected_entries if use_projection and projected_entries is not None else canonical
        )
        source = "redis" if use_projection else "postgresql"
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
        score, rank = await services.canonical_player_rank(
            session,
            player_id=player_id,
            owner_id=owner_id,
            game_key=game_key,
            period_start=period_start,
        )
        canonical = _database_entry(score, rank)
        count = await repositories.count_canonical_scores(
            session, game_id=score.game_id, period_start=period_start
        )
        revision = await repositories.leaderboard_projection_revision(
            session, game_id=score.game_id, period_start=period_start
        )
        projected = await projected_player_rank(
            self._client,
            game_key=game_key,
            period_start=period_start.strftime("%Y%m%dT%H%M%SZ"),
            player_id=str(player_id),
            expected_count=count,
            expected_revision=revision,
        )
        try:
            candidate = _projected_entry(*projected) if projected is not None else None
        except OverflowError, TypeError, ValueError:
            candidate = None
        source = "redis" if candidate == canonical else "postgresql"
        self._record_source(source)
        return LeaderboardRank(
            source=source,
            entry=candidate if candidate == canonical and candidate is not None else canonical,
        )
