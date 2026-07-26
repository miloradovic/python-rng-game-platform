"""Authentication and validation for Redis leaderboard projection data."""

import hashlib
import hmac
import json
import math
from collections.abc import Mapping
from uuid import UUID

from app.config import Settings
from app.redis_cache.lifecycle import PROJECTION_ENCODING_VERSION, ProjectionGeneration


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
