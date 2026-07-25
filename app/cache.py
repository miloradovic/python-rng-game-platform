"""Optional Redis lifecycle and integrity-protected leaderboard projections."""

import hashlib
import hmac
import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import cast
from uuid import UUID

from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.config import Settings, get_settings

PROJECTION_ENCODING_VERSION = "v2"


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


def _canonical_payload(payload: Mapping[str, object]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()


class ProjectionIntegrity:
    """Sign and verify projection metadata and members with rotatable HMAC keys."""

    def __init__(self, current_secret: str, previous_secret: str | None = None) -> None:
        self._current = current_secret.encode()
        self._verification_keys = {self.key_id(self._current): self._current}
        if previous_secret is not None:
            previous = previous_secret.encode()
            self._verification_keys[self.key_id(previous)] = previous

    @staticmethod
    def key_id(secret: bytes) -> str:
        """Return a non-secret identifier used to select a rotation key."""

        return hashlib.sha256(secret).hexdigest()[:16]

    @property
    def current_key_id(self) -> str:
        return self.key_id(self._current)

    @staticmethod
    def _digest(secret: bytes, payload: Mapping[str, object]) -> str:
        return hmac.new(secret, _canonical_payload(payload), hashlib.sha256).hexdigest()

    def metadata(self, contract: ProjectionGeneration) -> dict[str, str]:
        """Return Redis hash fields authenticated by the current key."""

        fields = {
            "game_key": contract.game_key,
            "period_start": contract.period_start,
            "generation": contract.generation,
            "revision": str(contract.revision),
            "count": str(contract.count),
            "encoding_version": contract.encoding_version,
            "key_id": self.current_key_id,
        }
        fields["signature"] = self._digest(self._current, fields)
        return fields

    def verify_metadata(
        self,
        fields: dict[str, str],
        *,
        game_key: str,
        period_start: str,
        generation: str,
        expected_revision: int,
        expected_count: int,
    ) -> ProjectionGeneration | None:
        """Validate scope, generation, durable facts, version, and signature."""

        try:
            signature = fields["signature"]
            unsigned = {key: value for key, value in fields.items() if key != "signature"}
            secret = self._verification_keys[unsigned["key_id"]]
            contract = ProjectionGeneration(
                game_key=unsigned["game_key"],
                period_start=unsigned["period_start"],
                generation=unsigned["generation"],
                revision=int(unsigned["revision"]),
                count=int(unsigned["count"]),
                encoding_version=unsigned["encoding_version"],
                key_id=unsigned["key_id"],
            )
        except KeyError, ValueError:
            return None
        if set(fields) != {
            "game_key",
            "period_start",
            "generation",
            "revision",
            "count",
            "encoding_version",
            "key_id",
            "signature",
        }:
            return None
        if not hmac.compare_digest(signature, self._digest(secret, unsigned)):
            return None
        if (
            contract.game_key != game_key
            or contract.period_start != period_start
            or contract.generation != generation
            or contract.revision != expected_revision
            or contract.count != expected_count
            or contract.encoding_version != PROJECTION_ENCODING_VERSION
        ):
            return None
        return contract

    def member(
        self,
        contract: ProjectionGeneration,
        *,
        completed_at_us: int,
        session_id: str,
        score_id: str,
        player_id: str,
        final_score: int,
        rank: int,
    ) -> str:
        """Encode deterministic ordering fields with a generation-bound signature."""

        base = f"{completed_at_us:020d}:{session_id}:{score_id}:{player_id}"
        signature = self._digest(
            self._current,
            {
                "encoding_version": contract.encoding_version,
                "game_key": contract.game_key,
                "period_start": contract.period_start,
                "generation": contract.generation,
                "completed_at_us": completed_at_us,
                "session_id": session_id,
                "score_id": score_id,
                "player_id": player_id,
                "final_score": final_score,
                "rank": rank,
            },
        )
        return f"{base}:{rank:020d}:{signature}"

    def parse_member(
        self, contract: ProjectionGeneration, member: str, redis_score: float
    ) -> tuple[int, str, str, str, int, int] | None:
        """Validate one member and return its response and ordering fields."""

        try:
            completed_text, session_id, score_id, player_id, rank_text, signature = member.split(
                ":", 5
            )
            completed_at_us = int(completed_text)
            if completed_text != f"{completed_at_us:020d}":
                return None
            rank = int(rank_text)
            if rank < 1 or rank_text != f"{rank:020d}":
                return None
            session_id = str(UUID(session_id))
            score_id = str(UUID(score_id))
            player_id = str(UUID(player_id))
            if not math.isfinite(redis_score) or not redis_score.is_integer():
                return None
            final_score = int(-redis_score)
            secret = self._verification_keys[contract.key_id]
        except KeyError, TypeError, ValueError:
            return None
        expected = self._digest(
            secret,
            {
                "encoding_version": contract.encoding_version,
                "game_key": contract.game_key,
                "period_start": contract.period_start,
                "generation": contract.generation,
                "completed_at_us": completed_at_us,
                "session_id": session_id,
                "score_id": score_id,
                "player_id": player_id,
                "final_score": final_score,
                "rank": rank,
            },
        )
        if not hmac.compare_digest(signature, expected):
            return None
        return completed_at_us, session_id, score_id, player_id, final_score, rank


def projection_integrity(settings: Settings) -> ProjectionIntegrity | None:
    """Build projection integrity policy without exposing configured secrets."""

    current = settings.leaderboard_projection_hmac_secret
    if current is None:
        return None
    previous = settings.leaderboard_projection_previous_hmac_secret
    return ProjectionIntegrity(
        current.get_secret_value(),
        previous.get_secret_value() if previous is not None else None,
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
