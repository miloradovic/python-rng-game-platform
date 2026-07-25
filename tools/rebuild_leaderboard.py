"""Rebuild one disposable Redis leaderboard exclusively from PostgreSQL."""

import argparse
import asyncio
import logging
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import cast
from uuid import UUID, uuid4

from redis.asyncio import Redis
from redis.exceptions import RedisError
from redis.typing import EncodableT
from sqlalchemy.ext.asyncio import AsyncSession

from app import repositories
from app.cache import (
    ProjectionGeneration,
    ProjectionIntegrity,
    create_redis_client,
    leaderboard_pointer_key,
    projection_generation_keys,
    projection_integrity,
)
from app.config import get_settings
from app.database import Database
from app.logging import configure_logging

logger = logging.getLogger(__name__)
STALE_GENERATION_TTL_SECONDS = 300


class RebuildVerificationError(RuntimeError):
    """Raised when a disposable projection cannot be proven current."""


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


def _member(
    score: object,
    integrity: ProjectionIntegrity,
    contract: ProjectionGeneration,
    rank: int,
) -> tuple[str, int, str]:
    from app.models import FinalScore

    if not isinstance(score, FinalScore):
        raise TypeError("score must be a FinalScore")
    player_id = str(score.player_id)
    member = integrity.member(
        contract,
        completed_at_us=int(score.completed_at.timestamp() * 1_000_000),
        session_id=str(score.session_id),
        score_id=str(score.id),
        player_id=player_id,
        final_score=score.final_score,
        rank=rank,
    )
    return member, -score.final_score, player_id


async def rebuild_leaderboard(
    session: AsyncSession,
    client: Redis,
    *,
    game_key: str,
    period_start: datetime,
    integrity: ProjectionIntegrity | None = None,
) -> int:
    """Build an immutable generation and atomically publish its pointer."""

    resolved_integrity = integrity or projection_integrity(get_settings())
    if resolved_integrity is None:
        raise RuntimeError("leaderboard projection integrity is not configured")
    game = await repositories.get_game(session, game_key)
    if game is None:
        raise ValueError("unknown game")
    period_key = period_start.strftime("%Y%m%dT%H%M%SZ")
    generation = str(uuid4())
    keys = projection_generation_keys(game_key, period_key, generation)
    pointer = leaderboard_pointer_key(game_key, period_key)
    previous_generation = await client.get(pointer)
    try:
        scores = await repositories.list_canonical_scores(
            session, game_id=game.id, period_start=period_start
        )
        revision = await repositories.leaderboard_projection_revision(
            session, game_id=game.id, period_start=period_start
        )
        contract = ProjectionGeneration(
            game_key=game_key,
            period_start=period_key,
            generation=generation,
            revision=revision,
            count=len(scores),
            key_id=resolved_integrity.current_key_id,
        )
        encoded = [
            _member(score, resolved_integrity, contract, rank)
            for rank, score in enumerate(scores, start=1)
        ]
        members = {member: score for member, score, _ in encoded}
        players: dict[str, str] = {}
        for member, _, player_id in encoded:
            players.setdefault(player_id, member)

        async with client.pipeline(transaction=True) as pipeline:
            if members:
                pipeline.zadd(keys.members, members)
            if players:
                pipeline.hset(
                    keys.players,
                    mapping=cast(Mapping[EncodableT, EncodableT], players),
                )
            pipeline.hset(
                keys.metadata,
                mapping=cast(
                    Mapping[EncodableT, EncodableT],
                    resolved_integrity.metadata(contract),
                ),
            )
            pipeline.set(pointer, generation)
            await pipeline.execute()

        projected = await client.zrange(keys.members, 0, -1, withscores=True)
        expected = [(member, float(score)) for member, score, _ in encoded]
        if projected != expected:
            raise RebuildVerificationError("rebuilt projection does not match PostgreSQL")

        if isinstance(previous_generation, str):
            try:
                previous_generation = str(UUID(previous_generation))
            except ValueError:
                previous_generation = None
            if previous_generation is not None and previous_generation != generation:
                previous_keys = projection_generation_keys(
                    game_key, period_key, previous_generation
                )
                try:
                    async with client.pipeline(transaction=True) as pipeline:
                        pipeline.expire(previous_keys.members, STALE_GENERATION_TTL_SECONDS)
                        pipeline.expire(previous_keys.metadata, STALE_GENERATION_TTL_SECONDS)
                        pipeline.expire(previous_keys.players, STALE_GENERATION_TTL_SECONDS)
                        await pipeline.execute()
                except RedisError:
                    logger.warning("leaderboard_generation_cleanup_deferred")
        return len(scores)
    except BaseException:
        await client.delete(keys.members, keys.metadata, keys.players)
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
                session,
                client,
                game_key=args.game_key,
                period_start=_period(args.period_start),
            )
        logger.info(
            "leaderboard_rebuild_complete game_key=%s period_start=%s rows=%s",
            args.game_key,
            args.period_start,
            count,
        )
    except RebuildVerificationError as error:
        logger.error(
            "leaderboard_rebuild_mismatch game_key=%s period_start=%s",
            args.game_key,
            args.period_start,
        )
        raise SystemExit(2) from error
    except RedisError as error:
        logger.error("leaderboard_rebuild_failed reason=redis_unavailable")
        raise SystemExit(1) from error
    finally:
        await client.aclose()
        await database.dispose()


if __name__ == "__main__":
    asyncio.run(main())
