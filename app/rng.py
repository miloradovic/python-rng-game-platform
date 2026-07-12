"""Cryptographic outcome-provider seam for player-visible results.

This HMAC-SHA256 provider gives rules an unbiased cryptographic integer.
Replace key-only internal reproducibility with commitment/reveal and
independent public proof semantics without changing the rule-facing interface.
"""

import hashlib
import hmac
import secrets
import uuid
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class DerivedValue:
    """Unbiased bounded value and non-secret derivation evidence."""

    value: int
    digest_hex: str


class OutcomeProvider(Protocol):
    """Cryptographic input boundary consumed by deterministic game rules."""

    def uniform(
        self,
        *,
        session_id: uuid.UUID,
        game_key: str,
        config_version_id: uuid.UUID,
        purpose: str,
        upper_bound: int,
    ) -> DerivedValue: ...


class HmacOutcomeProvider:
    """Derive unbiased bounded integers using HMAC-SHA256 rejection sampling."""

    def __init__(self, secret: str) -> None:
        if len(secret.encode()) < 32:
            raise ValueError("OUTCOME_HMAC_SECRET must contain at least 32 bytes")
        self._key = secret.encode()

    def uniform(
        self,
        *,
        session_id: uuid.UUID,
        game_key: str,
        config_version_id: uuid.UUID,
        purpose: str,
        upper_bound: int,
    ) -> DerivedValue:
        if upper_bound <= 0:
            raise ValueError("upper_bound must be positive")
        base = f"{session_id}:{game_key}:{config_version_id}:{purpose}".encode()
        limit = (1 << 256) - ((1 << 256) % upper_bound)
        counter = 0
        while True:
            digest = hmac.new(self._key, base + counter.to_bytes(4, "big"), hashlib.sha256).digest()
            raw = int.from_bytes(digest, "big")
            if raw < limit:
                return DerivedValue(raw % upper_bound, digest.hex())
            counter += 1


def secure_challenge(size: int = 5) -> list[int]:
    """Create a unique server-owned skill sequence with OS cryptographic entropy."""

    return secrets.SystemRandom().sample(range(10), size)
