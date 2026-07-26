"""Redis client lifecycle and deterministic projection-key contracts."""

from dataclasses import dataclass

from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.config import Settings, get_settings

PROJECTION_ENCODING_VERSION = "v2"


@dataclass(frozen=True, slots=True)
class ProjectionGeneration:
    """Authenticated contract for one complete leaderboard generation."""

    game_key: str
    period_start: str
    generation: str
    revision: int
    count: int
    encoding_version: str = PROJECTION_ENCODING_VERSION
    key_id: str = ""


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
    """Return the stable scope key for one game and period."""

    return _namespaced_key(
        f"leaderboard:{PROJECTION_ENCODING_VERSION}:{game_key}:{period_start}", namespace
    )


def leaderboard_pointer_key(game_key: str, period_start: str) -> str:
    """Return the atomic current-generation pointer key."""

    return f"{leaderboard_key(game_key, period_start)}:current"


@dataclass(frozen=True, slots=True)
class ProjectionKeys:
    """Redis keys belonging to one immutable projection generation."""

    members: str
    metadata: str
    players: str


def projection_generation_keys(game_key: str, period_start: str, generation: str) -> ProjectionKeys:
    base = f"{leaderboard_key(game_key, period_start)}:generation:{generation}"
    return ProjectionKeys(
        members=f"{base}:members",
        metadata=f"{base}:metadata",
        players=f"{base}:players",
    )


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
