"""Rebuild one disposable Redis leaderboard exclusively from PostgreSQL."""

import argparse
import asyncio
import logging
from datetime import UTC, datetime
from uuid import uuid4

from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy.ext.asyncio import AsyncSession

from app import repositories
from app.cache import create_redis_client, leaderboard_key, leaderboard_member
from app.config import get_settings
from app.database import Database
from app.logging import configure_logging

logger = logging.getLogger(__name__)


def _period(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("period-start must include UTC offset")
    parsed = parsed.astimezone(UTC)
    if parsed.weekday() != 0 or any(
        (parsed.hour, parsed.minute, parsed.second, parsed.microsecond)
    ):
        raise ValueError("period-start must be Monday 00:00:00 UTC")
    return parsed


def _mapping(score: object) -> tuple[str, int]:
    from app.models import FinalScore

    if not isinstance(score, FinalScore):
        raise TypeError("score must be a FinalScore")
    member = leaderboard_member(
        completed_at_us=int(score.completed_at.timestamp() * 1_000_000),
        session_id=str(score.session_id),
        score_id=str(score.id),
        player_id=str(score.player_id),
    )
    return member, -score.final_score


async def rebuild_leaderboard(
    session: AsyncSession, client: Redis, *, game_key: str, period_start: datetime
) -> int:
    """Atomically replace a projection, then catch up scores racing the snapshot."""

    game = await repositories.get_game(session, game_key)
    if game is None:
        raise ValueError("unknown game")
    scores = await repositories.list_canonical_scores(
        session, game_id=game.id, period_start=period_start
    )
    period_key = period_start.strftime("%Y%m%dT%H%M%SZ")
    target = leaderboard_key(game_key, period_key)
    temporary = f"{target}:rebuild:{uuid4()}"
    try:
        if scores:
            await client.zadd(temporary, dict(_mapping(score) for score in scores))
            await client.rename(temporary, target)
        else:
            await client.delete(target)
        # A commit projected before rename is recovered here; one after rename
        # writes directly to the new target.
        current = await repositories.list_canonical_scores(
            session, game_id=game.id, period_start=period_start
        )
        if current:
            await client.zadd(target, dict(_mapping(score) for score in current))
        projected = await client.zrange(target, 0, -1, withscores=True)
        expected = [(member, float(value)) for member, value in map(_mapping, current)]
        if projected != expected:
            raise RuntimeError("rebuilt projection does not match PostgreSQL")
        return len(current)
    except BaseException:
        await client.delete(temporary)
        raise


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("game_key")
    parser.add_argument("period_start", help="UTC Monday, for example 2026-07-13T00:00:00Z")
    args = parser.parse_args()
    settings = get_settings()
    configure_logging(settings.log_level)
    database = Database(settings)
    client = create_redis_client(settings)
    if client is None:
        raise RuntimeError("Redis is not configured")
    try:
        async with database.session_factory() as session:
            count = await rebuild_leaderboard(
                session, client, game_key=args.game_key, period_start=_period(args.period_start)
            )
        logger.info(
            "leaderboard_rebuild_complete game_key=%s period_start=%s rows=%s",
            args.game_key,
            args.period_start,
            count,
        )
    except RedisError as error:
        logger.error("leaderboard_rebuild_failed reason=redis_unavailable")
        raise SystemExit(1) from error
    finally:
        await client.aclose()
        await database.dispose()


if __name__ == "__main__":
    asyncio.run(main())
