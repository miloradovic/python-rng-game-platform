"""Optional Redis connection lifecycle for rebuildable state."""

from typing import Any, cast

from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.config import Settings, get_settings


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
    except RedisError:
        return False


def _namespaced_key(key: str, namespace: str | None = None) -> str:
    resolved = get_settings().redis_key_namespace if namespace is None else namespace
    return f"{resolved}:{key}" if resolved else key


def leaderboard_key(game_key: str, period_start: str, *, namespace: str | None = None) -> str:
    """Return the isolated sorted-set key for one game and ISO-week."""
    return _namespaced_key(f"leaderboard:v1:{game_key}:{period_start}", namespace)


def leaderboard_metadata_key(
    game_key: str, period_start: str, *, namespace: str | None = None
) -> str:
    """Return projection metadata isolated with the sorted set generation."""

    return f"{leaderboard_key(game_key, period_start, namespace=namespace)}:metadata"


async def clear_redis_namespace(client: Redis, namespace: str, *, batch_size: int = 100) -> int:
    """Delete only keys owned by one nonempty namespace."""

    if not namespace:
        raise ValueError("a nonempty Redis namespace is required for cleanup")
    deleted = 0
    keys: list[str] = []
    async for key in client.scan_iter(match=f"{namespace}:*", count=batch_size):
        keys.append(key)
        if len(keys) >= batch_size:
            deleted += int(await client.delete(*keys))
            keys.clear()
    if keys:
        deleted += int(await client.delete(*keys))
    return deleted


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
        async with client.pipeline(transaction=True) as pipeline:
            pipeline.zadd(leaderboard_key(game_key, period), {member: -score.final_score})
            pipeline.delete(leaderboard_metadata_key(game_key, period))
            await pipeline.execute()
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
    expected_revision: int,
) -> list[tuple[str, int, int]] | None:
    """Read a projection only when its generation matches PostgreSQL."""
    if client is None:
        return None
    key = leaderboard_key(game_key, period_start)
    try:
        async with client.pipeline(transaction=False) as pipeline:
            pipeline.hgetall(leaderboard_metadata_key(game_key, period_start))
            pipeline.zcard(key)
            metadata, cardinality = await pipeline.execute()
        if (
            metadata
            != {
                "revision": str(expected_revision),
                "count": str(expected_count),
            }
            or cardinality != expected_count
        ):
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
    expected_revision: int,
) -> tuple[str, int, int] | None:
    """Return a projected member only when the complete projection is current."""
    if client is None:
        return None
    key = leaderboard_key(game_key, period_start)
    try:
        async with client.pipeline(transaction=False) as pipeline:
            pipeline.hgetall(leaderboard_metadata_key(game_key, period_start))
            pipeline.zcard(key)
            metadata, cardinality = await pipeline.execute()
        if (
            metadata
            != {
                "revision": str(expected_revision),
                "count": str(expected_count),
            }
            or cardinality != expected_count
        ):
            return None
        async for member, score in client.zscan_iter(key):
            if parse_leaderboard_member(member)[3] == player_id:
                rank = cast(Any, await client.zrank(key, member))
                if rank is not None:
                    return member, int(-float(score)), rank + 1
    except RedisError:
        return None
    return None
