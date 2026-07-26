"""Authenticated Redis leaderboard projection reads."""

from typing import cast
from uuid import UUID

from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.redis_cache.integrity import ProjectionIntegrity
from app.redis_cache.lifecycle import (
    ProjectionGeneration,
    ProjectionKeys,
    leaderboard_pointer_key,
    projection_generation_keys,
)


async def project_final_score(client: Redis | None, score: object, game_key: str) -> bool:
    """Acknowledge durable revision invalidation after a score commit."""

    if client is None:
        return False
    from app.models import FinalScore

    if not isinstance(score, FinalScore):
        raise TypeError("score must be a FinalScore")
    del game_key
    # The PostgreSQL insert trigger advances the authoritative revision in the
    # score transaction. Readers therefore reject the old generation without a
    # fragile local Redis patch, and the next rebuild can still clean it up.
    return True


async def _validated_generation(
    client: Redis,
    integrity: ProjectionIntegrity,
    *,
    game_key: str,
    period_start: str,
    expected_count: int,
    expected_revision: int,
) -> tuple[ProjectionGeneration, ProjectionKeys] | None:
    generation = await client.get(leaderboard_pointer_key(game_key, period_start))
    if not isinstance(generation, str):
        return None
    try:
        generation = str(UUID(generation))
    except ValueError:
        return None
    keys = projection_generation_keys(game_key, period_start, generation)
    async with client.pipeline(transaction=False) as pipeline:
        pipeline.hgetall(keys.metadata)
        pipeline.zcard(keys.members)
        metadata, cardinality = await pipeline.execute()
    contract = integrity.verify_metadata(
        cast(dict[str, str], metadata),
        game_key=game_key,
        period_start=period_start,
        generation=generation,
        expected_revision=expected_revision,
        expected_count=expected_count,
    )
    if contract is None or cardinality != expected_count:
        return None
    return contract, keys


async def projected_page(
    client: Redis | None,
    integrity: ProjectionIntegrity | None,
    *,
    game_key: str,
    period_start: str,
    offset: int,
    limit: int,
    expected_count: int,
    expected_revision: int,
) -> list[tuple[int, str, str, str, int, int]] | None:
    """Read and authenticate only one requested projection page."""

    if client is None or integrity is None:
        return None
    try:
        validated = await _validated_generation(
            client,
            integrity,
            game_key=game_key,
            period_start=period_start,
            expected_count=expected_count,
            expected_revision=expected_revision,
        )
        if validated is None:
            return None
        contract, keys = validated
        rows = cast(
            list[tuple[str, float]],
            await client.zrange(keys.members, offset, offset + limit - 1, withscores=True),
        )
        expected_rows = min(limit, max(0, expected_count - offset))
        if len(rows) != expected_rows:
            return None
        result: list[tuple[int, str, str, str, int, int]] = []
        for index, (member, score) in enumerate(rows):
            parsed = integrity.parse_member(contract, member, score)
            expected_rank = offset + index + 1
            if parsed is None or parsed[5] != expected_rank:
                return None
            result.append(parsed)
        return result
    except RedisError, TypeError, ValueError:
        return None


async def projected_player_rank(
    client: Redis | None,
    integrity: ProjectionIntegrity | None,
    *,
    game_key: str,
    period_start: str,
    player_id: str,
    expected_count: int,
    expected_revision: int,
) -> tuple[int, str, str, str, int, int] | None:
    """Read one authenticated player member and its rank without scanning Redis."""

    if client is None or integrity is None:
        return None
    try:
        validated = await _validated_generation(
            client,
            integrity,
            game_key=game_key,
            period_start=period_start,
            expected_count=expected_count,
            expected_revision=expected_revision,
        )
        if validated is None:
            return None
        contract, keys = validated
        member = await client.hget(keys.players, player_id)
        if not isinstance(member, str):
            return None
        async with client.pipeline(transaction=False) as pipeline:
            pipeline.zscore(keys.members, member)
            pipeline.zrank(keys.members, member)
            score, rank = await pipeline.execute()
        if score is None or rank is None:
            return None
        parsed = integrity.parse_member(contract, member, float(score))
        if parsed is None or parsed[3] != player_id or parsed[5] != int(rank) + 1:
            return None
        return parsed
    except RedisError, TypeError, ValueError:
        return None
