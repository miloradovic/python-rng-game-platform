"""Optional Redis connection lifecycle for rebuildable state."""

from redis.asyncio import Redis
from redis.exceptions import ConnectionError as RedisConnectionError
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
