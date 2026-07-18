"""Run deterministic distribution checks against the daily-spin fairness core.

The simulation never opens a database or creates application records. It derives
each result through the production ``derive_daily_spin`` and
``select_daily_spin_reward`` path using deterministic, documented fixture inputs.
It is a mapping sanity check, not evidence about future random draws.
"""

import argparse
import hashlib
import json
import math
import sys
import uuid
from collections import Counter
from collections.abc import Sequence
from dataclasses import asdict, dataclass

from pydantic import ValidationError

from app.rng import (
    ALGORITHM_HMAC_SHA256,
    DAILY_SPIN_MAPPING_VERSION_V1,
    PROTOCOL_VERSION_V1,
    FairnessError,
    RewardBand,
    daily_spin_mapping_digest,
    derive_daily_spin,
)
from app.schemas import DailySpinConfig, game_config_adapter
from tools.seed import CATALOGUE

DEFAULT_RUNS = 100_000
MAX_SIMULATION_RUNS = 1_000_000
SIMULATION_VERSION = "daily-spin-distribution-simulation-v1"
CONFIG_SOURCE = "tools.seed.CATALOGUE:daily_spin:version=1"
CONFIG_VERSION = 1
SERVER_SEED_STRATEGY = "sha256('rng-game-platform-simulation-server-seed-v1\\nrun={index}')"
CLIENT_SEED_STRATEGY = "simulation-client-{index:016x}"
SESSION_ID_STRATEGY = "UUIDv5(URL, 'rng-game-platform-simulation-v1:{index}')"
NONCE_STRATEGY = "run index, beginning at 0"
STANDARD_DEVIATION_LIMIT = 5.0


class SimulationInputError(ValueError):
    """Raised when a simulation request cannot exercise the frozen v1 protocol."""


@dataclass(frozen=True)
class RewardDistribution:
    """Expected and observed evidence for one immutable configured reward band."""

    key: str
    value: int
    weight: int
    expected_probability: float
    expected_count: float
    observed_count: int
    observed_rate: float
    deviation_count: float
    standard_deviation: float
    allowed_deviation: float
    within_tolerance: bool


@dataclass(frozen=True)
class SimulationReport:
    """A serializable, repeatable daily-spin distribution sanity report."""

    simulation_version: str
    game_key: str
    runs: int
    protocol_version: str
    algorithm: str
    mapping_version: str
    mapping_digest: str
    config_source: str
    config_version: int
    config_payload_sha256: str
    seed_nonce_strategy: dict[str, str]
    criterion: dict[str, float | str]
    rewards: tuple[RewardDistribution, ...]
    passed: bool


def seeded_daily_spin_reward_bands() -> tuple[RewardBand, ...]:
    """Load the validated, version-1 daily-spin map used by the seed command."""

    config = _seeded_daily_spin_config()
    return tuple(RewardBand(reward.key, reward.weight, reward.value) for reward in config.rewards)


def _seeded_daily_spin_config() -> DailySpinConfig:
    for key, _name, _description, payload in CATALOGUE:
        if key != "daily_spin":
            continue
        try:
            config = game_config_adapter.validate_python(payload)
        except ValidationError as error:
            raise SimulationInputError("the seeded daily_spin configuration is invalid") from error
        if config.game_type != "daily_spin":
            raise SimulationInputError("the seeded daily_spin payload has the wrong game type")
        return config
    raise SimulationInputError("the seeded daily_spin configuration is unavailable")


def simulate_daily_spin(runs: int = DEFAULT_RUNS) -> SimulationReport:
    """Exercise the v1 production derivation path with deterministic fixture inputs."""

    if not isinstance(runs, int) or isinstance(runs, bool) or not 1 <= runs <= MAX_SIMULATION_RUNS:
        raise SimulationInputError(f"runs must be an integer between 1 and {MAX_SIMULATION_RUNS}")

    reward_bands = seeded_daily_spin_reward_bands()
    try:
        mapping_digest = daily_spin_mapping_digest(reward_bands)
    except FairnessError as error:
        raise SimulationInputError("the seeded daily_spin reward map is invalid") from error
    payload = _seeded_daily_spin_payload()
    config_payload_sha256 = _canonical_json_sha256(payload)
    config_version_id = uuid.uuid5(uuid.NAMESPACE_URL, CONFIG_SOURCE)
    observed = Counter[str]()
    for index in range(runs):
        result = derive_daily_spin(
            server_seed=_simulation_server_seed(index),
            client_seed=CLIENT_SEED_STRATEGY.format(index=index),
            nonce=index,
            config_version_id=config_version_id,
            session_id=uuid.uuid5(uuid.NAMESPACE_URL, f"rng-game-platform-simulation-v1:{index}"),
            reward_bands=reward_bands,
        )
        observed[result.reward.key] += 1

    total_weight = sum(band.weight for band in reward_bands)
    distributions = tuple(
        _reward_distribution(
            band, total_weight=total_weight, runs=runs, observed=observed[band.key]
        )
        for band in reward_bands
    )
    return SimulationReport(
        simulation_version=SIMULATION_VERSION,
        game_key="daily_spin",
        runs=runs,
        protocol_version=PROTOCOL_VERSION_V1,
        algorithm=ALGORITHM_HMAC_SHA256,
        mapping_version=DAILY_SPIN_MAPPING_VERSION_V1,
        mapping_digest=mapping_digest,
        config_source=CONFIG_SOURCE,
        config_version=CONFIG_VERSION,
        config_payload_sha256=config_payload_sha256,
        seed_nonce_strategy={
            "server_seed": SERVER_SEED_STRATEGY,
            "client_seed": CLIENT_SEED_STRATEGY,
            "session_id": SESSION_ID_STRATEGY,
            "nonce": NONCE_STRATEGY,
        },
        criterion={
            "name": "per-band binomial count is within five standard deviations of expectation",
            "standard_deviation_limit": STANDARD_DEVIATION_LIMIT,
        },
        rewards=distributions,
        passed=all(distribution.within_tolerance for distribution in distributions),
    )


def _seeded_daily_spin_payload() -> dict[str, object]:
    return _seeded_daily_spin_config().model_dump(mode="json")


def _simulation_server_seed(index: int) -> bytes:
    material = f"rng-game-platform-simulation-server-seed-v1\nrun={index}".encode("ascii")
    return hashlib.sha256(material).digest()


def _canonical_json_sha256(payload: dict[str, object]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode(
        "ascii"
    )
    return hashlib.sha256(encoded).hexdigest()


def _reward_distribution(
    band: RewardBand, *, total_weight: int, runs: int, observed: int
) -> RewardDistribution:
    probability = band.weight / total_weight
    expected = runs * probability
    standard_deviation = math.sqrt(runs * probability * (1 - probability))
    allowed_deviation = STANDARD_DEVIATION_LIMIT * standard_deviation
    deviation = observed - expected
    return RewardDistribution(
        key=band.key,
        value=band.value,
        weight=band.weight,
        expected_probability=probability,
        expected_count=expected,
        observed_count=observed,
        observed_rate=observed / runs,
        deviation_count=deviation,
        standard_deviation=standard_deviation,
        allowed_deviation=allowed_deviation,
        within_tolerance=abs(deviation) <= allowed_deviation,
    )


def build_parser() -> argparse.ArgumentParser:
    """Build the narrow command-line interface required by the roadmap."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("game_key", choices=("daily_spin",))
    parser.add_argument("--runs", type=int, default=DEFAULT_RUNS, help="number of derived outcomes")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Print one JSON evidence report and return a meaningful process status."""

    args = build_parser().parse_args(argv)
    try:
        report = simulate_daily_spin(args.runs)
    except SimulationInputError as error:
        build_parser().error(str(error))
    print(json.dumps(asdict(report), indent=2, sort_keys=True))
    return 0 if report.passed else 1


if __name__ == "__main__":
    sys.exit(main())
