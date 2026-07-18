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


# The v1 public protocol is deliberately independent from ``HmacOutcomeProvider``.
# The existing provider remains the internal gameplay seam until Phase 4 connects
# commitment/reveal persistence and HTTP adapters.
PROTOCOL_VERSION_V1 = "rng-game-platform-pvf-v1"
ALGORITHM_HMAC_SHA256 = "hmac-sha256"
DAILY_SPIN_GAME_KEY = "daily_spin"
DAILY_SPIN_MAPPING_VERSION_V1 = "daily-spin-weighted-reward-v1"
SERVER_SEED_BYTES = 32
MAX_NONCE = (1 << 64) - 1
MAX_REWARD_WEIGHT = (1 << 63) - 1
MAX_REWARD_VALUE = (1 << 31) - 1


class FairnessError(ValueError):
    """Base error for invalid v1 fairness inputs with a stable failure code."""

    code = "invalid_fairness_input"


class UnsupportedFairnessVersionError(FairnessError):
    """Raised when a stored proof cannot be dispatched to a supported protocol."""

    code = "unsupported_fairness_version"


class InvalidFairnessSeedError(FairnessError):
    """Raised for malformed server or client seed material."""

    code = "invalid_fairness_seed"


class InvalidFairnessNonceError(FairnessError):
    """Raised when a nonce is outside the v1 unsigned-64-bit range."""

    code = "invalid_fairness_nonce"


class InvalidRewardMappingError(FairnessError):
    """Raised when a daily-spin reward map cannot be evaluated safely."""

    code = "invalid_reward_mapping"


@dataclass(frozen=True)
class RewardBand:
    """One ordered, immutable daily-spin reward band used by protocol v1."""

    key: str
    weight: int
    value: int


@dataclass(frozen=True)
class DailySpinDerivation:
    """All non-secret deterministic evidence for one accepted daily-spin draw."""

    mapping_digest: str
    raw_digest_hex: str
    raw_value: int
    normalized_value: int
    attempt: int
    reward: RewardBand


@dataclass(frozen=True)
class DailySpinProof:
    """Public v1 evidence sufficient to independently verify a revealed result."""

    protocol_version: str
    algorithm: str
    server_seed_commitment: str
    server_seed_hex: str
    client_seed: str
    nonce: int
    game_key: str
    config_version_id: uuid.UUID
    session_id: uuid.UUID
    reward_bands: tuple[RewardBand, ...]
    mapping_version: str
    mapping_digest: str
    raw_digest_hex: str
    normalized_value: int
    attempt: int
    reward_key: str
    reward_value: int


@dataclass(frozen=True)
class VerificationResult:
    """A recalculated proof verdict; ``code`` is stable for callers and tests."""

    verified: bool
    code: str


def create_server_seed() -> bytes:
    """Create the exact 32 bytes committed before a v1 fairness evaluation."""

    return secrets.token_bytes(SERVER_SEED_BYTES)


def server_seed_commitment(server_seed: bytes) -> str:
    """Return the lowercase SHA-256 commitment for one valid v1 server seed."""

    _validate_server_seed(server_seed)
    return hashlib.sha256(server_seed).hexdigest()


def daily_spin_mapping_digest(reward_bands: tuple[RewardBand, ...]) -> str:
    """Hash the ordered reward map using v1's language-independent byte format."""

    return hashlib.sha256(daily_spin_mapping_bytes(reward_bands)).hexdigest()


def daily_spin_mapping_bytes(reward_bands: tuple[RewardBand, ...]) -> bytes:
    """Encode an ordered daily-spin map exactly as frozen by the v1 protocol."""

    _validate_reward_bands(reward_bands)
    lines = [
        b"RNG-GAME-PLATFORM-DAILY-SPIN-MAP/1\n",
        f"reward_count={len(reward_bands)}\n".encode(),
    ]
    for index, band in enumerate(reward_bands):
        key_bytes = band.key.encode("utf-8")
        lines.extend(
            (
                f"reward[{index}].key={len(key_bytes)}:".encode("ascii") + key_bytes + b"\n",
                f"reward[{index}].weight={band.weight}\n".encode("ascii"),
                f"reward[{index}].value={band.value}\n".encode("ascii"),
            )
        )
    return b"".join(lines)


def daily_spin_derivation_message(
    *,
    client_seed: str,
    nonce: int,
    config_version_id: uuid.UUID,
    session_id: uuid.UUID,
    mapping_digest: str,
    attempt: int,
    game_key: str = DAILY_SPIN_GAME_KEY,
) -> bytes:
    """Build the exact v1 HMAC message for one rejection-sampling attempt."""

    _validate_supported_v1(game_key=game_key)
    client_seed_bytes = _validate_client_seed(client_seed)
    _validate_nonce(nonce)
    _validate_uuid(config_version_id, "config_version_id")
    _validate_uuid(session_id, "session_id")
    _validate_digest(mapping_digest, "mapping_digest")
    if not isinstance(attempt, int) or isinstance(attempt, bool) or attempt < 0:
        raise FairnessError("attempt must be an unsigned integer")
    game_key_bytes = game_key.encode("utf-8")
    return b"".join(
        (
            b"RNG-GAME-PLATFORM-PVF/1\n",
            b"algorithm=hmac-sha256\n",
            b"protocol=rng-game-platform-pvf-v1\n",
            f"game_key={len(game_key_bytes)}:".encode("ascii") + game_key_bytes + b"\n",
            f"config_version_id={config_version_id}\n".encode("ascii"),
            f"session_id={session_id}\n".encode("ascii"),
            f"client_seed={len(client_seed_bytes)}:".encode("ascii") + client_seed_bytes + b"\n",
            f"nonce={nonce}\n".encode("ascii"),
            b"purpose=daily_spin\n",
            b"mapping_version=daily-spin-weighted-reward-v1\n",
            f"mapping_digest={mapping_digest}\n".encode("ascii"),
            f"attempt={attempt}\n".encode("ascii"),
        )
    )


def derive_daily_spin(
    *,
    server_seed: bytes,
    client_seed: str,
    nonce: int,
    config_version_id: uuid.UUID,
    session_id: uuid.UUID,
    reward_bands: tuple[RewardBand, ...],
    game_key: str = DAILY_SPIN_GAME_KEY,
    protocol_version: str = PROTOCOL_VERSION_V1,
    algorithm: str = ALGORITHM_HMAC_SHA256,
    mapping_version: str = DAILY_SPIN_MAPPING_VERSION_V1,
) -> DailySpinDerivation:
    """Derive an unbiased v1 daily-spin result from complete explicit inputs."""

    _validate_server_seed(server_seed)
    _validate_supported_v1(
        game_key=game_key,
        protocol_version=protocol_version,
        algorithm=algorithm,
        mapping_version=mapping_version,
    )
    _validate_nonce(nonce)
    _validate_reward_bands(reward_bands)
    mapping_digest = daily_spin_mapping_digest(reward_bands)
    total_weight = sum(band.weight for band in reward_bands)
    limit = (1 << 256) - ((1 << 256) % total_weight)
    attempt = 0
    while True:
        message = daily_spin_derivation_message(
            client_seed=client_seed,
            nonce=nonce,
            config_version_id=config_version_id,
            session_id=session_id,
            mapping_digest=mapping_digest,
            attempt=attempt,
            game_key=game_key,
        )
        digest = hmac.new(server_seed, message, hashlib.sha256).digest()
        raw_value = int.from_bytes(digest, "big", signed=False)
        if raw_value < limit:
            normalized_value = raw_value % total_weight
            return DailySpinDerivation(
                mapping_digest=mapping_digest,
                raw_digest_hex=digest.hex(),
                raw_value=raw_value,
                normalized_value=normalized_value,
                attempt=attempt,
                reward=select_daily_spin_reward(reward_bands, normalized_value),
            )
        attempt += 1


def select_daily_spin_reward(
    reward_bands: tuple[RewardBand, ...], normalized_value: int
) -> RewardBand:
    """Map an in-range normalized v1 value to its first weighted reward band."""

    _validate_reward_bands(reward_bands)
    total_weight = sum(band.weight for band in reward_bands)
    if (
        not isinstance(normalized_value, int)
        or isinstance(normalized_value, bool)
        or not 0 <= normalized_value < total_weight
    ):
        raise InvalidRewardMappingError("normalized value is outside the reward-map range")
    remaining = normalized_value
    for band in reward_bands:
        if remaining < band.weight:
            return band
        remaining -= band.weight
    raise AssertionError("validated normalized value did not select a reward")


def verify_daily_spin_proof(proof: DailySpinProof) -> VerificationResult:
    """Recalculate every public v1 proof field instead of trusting a stored verdict."""

    try:
        _validate_supported_v1(
            game_key=proof.game_key,
            protocol_version=proof.protocol_version,
            algorithm=proof.algorithm,
            mapping_version=proof.mapping_version,
        )
    except UnsupportedFairnessVersionError:
        return VerificationResult(verified=False, code="unsupported_fairness_version")
    try:
        server_seed = _server_seed_from_hex(proof.server_seed_hex)
        if not hmac.compare_digest(
            server_seed_commitment(server_seed), proof.server_seed_commitment
        ):
            return VerificationResult(verified=False, code="commitment_mismatch")
        derivation = derive_daily_spin(
            server_seed=server_seed,
            client_seed=proof.client_seed,
            nonce=proof.nonce,
            config_version_id=proof.config_version_id,
            session_id=proof.session_id,
            reward_bands=proof.reward_bands,
            game_key=proof.game_key,
            protocol_version=proof.protocol_version,
            algorithm=proof.algorithm,
            mapping_version=proof.mapping_version,
        )
    except FairnessError as error:
        return VerificationResult(verified=False, code=error.code)
    if not hmac.compare_digest(derivation.mapping_digest, proof.mapping_digest):
        return VerificationResult(verified=False, code="mapping_digest_mismatch")
    if not hmac.compare_digest(derivation.raw_digest_hex, proof.raw_digest_hex):
        return VerificationResult(verified=False, code="raw_digest_mismatch")
    if derivation.normalized_value != proof.normalized_value:
        return VerificationResult(verified=False, code="normalized_value_mismatch")
    if derivation.attempt != proof.attempt:
        return VerificationResult(verified=False, code="attempt_mismatch")
    if derivation.reward.key != proof.reward_key or derivation.reward.value != proof.reward_value:
        return VerificationResult(verified=False, code="reward_mismatch")
    return VerificationResult(verified=True, code="verified")


def _validate_server_seed(server_seed: bytes) -> None:
    if not isinstance(server_seed, bytes) or len(server_seed) != SERVER_SEED_BYTES:
        raise InvalidFairnessSeedError("server seed must contain exactly 32 bytes")


def _server_seed_from_hex(server_seed_hex: str) -> bytes:
    _validate_digest(server_seed_hex, "server_seed_hex")
    return bytes.fromhex(server_seed_hex)


def _validate_client_seed(client_seed: str) -> bytes:
    if not isinstance(client_seed, str):
        raise InvalidFairnessSeedError("client seed must be text")
    try:
        encoded = client_seed.encode("utf-8")
    except UnicodeEncodeError as error:
        raise InvalidFairnessSeedError("client seed must be valid UTF-8") from error
    if not 1 <= len(encoded) <= 64 or any(not 0x21 <= byte <= 0x7E for byte in encoded):
        raise InvalidFairnessSeedError("client seed must contain 1-64 printable ASCII bytes")
    return encoded


def _validate_nonce(nonce: int) -> None:
    if not isinstance(nonce, int) or isinstance(nonce, bool) or not 0 <= nonce <= MAX_NONCE:
        raise InvalidFairnessNonceError("nonce must be an unsigned 64-bit integer")


def _validate_reward_bands(reward_bands: tuple[RewardBand, ...]) -> None:
    if not isinstance(reward_bands, tuple) or not reward_bands:
        raise InvalidRewardMappingError("reward map must be a non-empty tuple")
    total_weight = 0
    keys: set[str] = set()
    for band in reward_bands:
        if not isinstance(band, RewardBand) or not isinstance(band.key, str) or not band.key:
            raise InvalidRewardMappingError("reward bands require non-empty text keys")
        try:
            band.key.encode("utf-8")
        except UnicodeEncodeError as error:
            raise InvalidRewardMappingError("reward key must be valid UTF-8") from error
        if band.key in keys:
            raise InvalidRewardMappingError("reward keys must be unique")
        keys.add(band.key)
        if (
            not isinstance(band.weight, int)
            or isinstance(band.weight, bool)
            or not 0 < band.weight <= MAX_REWARD_WEIGHT
        ):
            raise InvalidRewardMappingError(
                "reward weights must be positive signed-64-bit integers"
            )
        if (
            not isinstance(band.value, int)
            or isinstance(band.value, bool)
            or not 0 <= band.value <= MAX_REWARD_VALUE
        ):
            raise InvalidRewardMappingError("reward values must be non-negative database integers")
        total_weight += band.weight
        if total_weight > MAX_REWARD_WEIGHT:
            raise InvalidRewardMappingError("total reward weight overflows persisted range")


def _validate_uuid(value: uuid.UUID, name: str) -> None:
    if not isinstance(value, uuid.UUID):
        raise FairnessError(f"{name} must be a UUID")


def _validate_digest(value: str, name: str) -> None:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise FairnessError(f"{name} must be 64 lowercase hexadecimal characters")


def _validate_supported_v1(
    *,
    game_key: str,
    protocol_version: str = PROTOCOL_VERSION_V1,
    algorithm: str = ALGORITHM_HMAC_SHA256,
    mapping_version: str = DAILY_SPIN_MAPPING_VERSION_V1,
) -> None:
    if (
        game_key != DAILY_SPIN_GAME_KEY
        or protocol_version != PROTOCOL_VERSION_V1
        or algorithm != ALGORITHM_HMAC_SHA256
        or mapping_version != DAILY_SPIN_MAPPING_VERSION_V1
    ):
        raise UnsupportedFairnessVersionError(
            "unsupported fairness protocol, algorithm, game, or map"
        )
