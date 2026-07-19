"""Optional Redis connection lifecycle for rebuildable state."""

from typing import Any, cast

from redis.asyncio import Redis
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import RedisError
from redis.exceptions import TimeoutError as RedisTimeoutError

from app.config import Settings


def create_redis_client(settings: Settings) -> Redis | None:
    """Create an async Redis client when the optional URL is configured."""

    if settings.redis_url is None:
        return None
    return Redis.from_url(
        settings.redis_url.get_secret_value(),
        socket_connect_timeout=settings.redis_connect_timeout_seconds,
        socket_timeout=settings.redis_connect_timeout_seconds,
        decode_responses=True,
    )


async def redis_is_available(client: Redis | None) -> bool | None:
    """Return Redis availability, or ``None`` when Redis is not configured."""

    if client is None:
        return None
    try:
        return bool(await client.ping())
    except RedisConnectionError, RedisTimeoutError:
        return False


def leaderboard_key(game_key: str, period_start: str) -> str:
    """Return the isolated sorted-set key for one game and ISO-week."""
    return f"leaderboard:v1:{game_key}:{period_start}"


def leaderboard_member(
    *, completed_at_us: int, session_id: str, score_id: str, player_id: str
) -> str:
    """Encode deterministic tie breakers and durable identifiers."""
    return f"{completed_at_us:020d}:{session_id}:{score_id}:{player_id}"


def parse_leaderboard_member(member: str) -> tuple[int, str, str, str]:
    completed_at_us, session_id, score_id, player_id = member.split(":", 3)
    return int(completed_at_us), session_id, score_id, player_id


async def project_final_score(client: Redis | None, score: object, game_key: str) -> bool:
    """Idempotently project one already-committed durable score."""
    if client is None:
        return False
    from app.models import FinalScore

    if not isinstance(score, FinalScore):
        raise TypeError("score must be a FinalScore")
    period = score.period_start.strftime("%Y%m%dT%H%M%SZ")
    completed_us = int(score.completed_at.timestamp() * 1_000_000)
    member = leaderboard_member(
        completed_at_us=completed_us,
        session_id=str(score.session_id),
        score_id=str(score.id),
        player_id=str(score.player_id),
    )
    try:
        await client.zadd(leaderboard_key(game_key, period), {member: -score.final_score})
    except RedisError:
        return False
    return True


async def projected_page(
    client: Redis | None,
    *,
    game_key: str,
    period_start: str,
    offset: int,
    limit: int,
    expected_count: int,
) -> list[tuple[str, int, int]] | None:
    """Read a projection only when its member count matches PostgreSQL."""
    if client is None:
        return None
    key = leaderboard_key(game_key, period_start)
    try:
        if await client.zcard(key) != expected_count:
            return None
        rows = cast(
            list[tuple[str, float]],
            await client.zrange(key, offset, offset + limit - 1, withscores=True),
        )
    except RedisError:
        return None
    return [(member, int(-score), offset + index + 1) for index, (member, score) in enumerate(rows)]


async def projected_player_rank(
    client: Redis | None,
    *,
    game_key: str,
    period_start: str,
    player_id: str,
    expected_count: int,
) -> tuple[str, int, int] | None:
    """Return a projected member only when the complete projection is current."""
    if client is None:
        return None
    key = leaderboard_key(game_key, period_start)
    try:
        if await client.zcard(key) != expected_count:
            return None
        async for member, score in client.zscan_iter(key):
            if parse_leaderboard_member(member)[3] == player_id:
                rank = cast(Any, await client.zrank(key, member))
                if rank is not None:
                    return member, int(-float(score)), rank + 1
    except RedisError:
        return None
    return None
