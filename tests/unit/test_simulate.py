"""Repeatable checks for the daily-spin distribution simulation tool."""

import json

import pytest

from tools import simulate

pytestmark = pytest.mark.unit


def test_small_fixture_is_repeatable_and_uses_expected_probabilities() -> None:
    first = simulate.simulate_daily_spin(100)
    second = simulate.simulate_daily_spin(100)

    assert first == second
    assert first.passed
    assert first.runs == 100
    assert (
        first.mapping_digest == "759a58aa1263a2c7a33e85f7192f1a51d67ad32ef4e95439762fe5717eba4a33"
    )
    assert [(reward.key, reward.expected_probability) for reward in first.rewards] == [
        ("coins_10", 0.8),
        ("coins_50", 0.2),
    ]
    assert sum(reward.expected_probability for reward in first.rewards) == pytest.approx(1.0)
    assert sum(reward.observed_count for reward in first.rewards) == 100
    assert all(reward.within_tolerance for reward in first.rewards)


@pytest.mark.parametrize("runs", [0, -1, True, 1 << 64])
def test_invalid_run_count_is_rejected(runs: int) -> None:
    with pytest.raises(simulate.SimulationInputError):
        _ = simulate.simulate_daily_spin(runs)


def test_cli_prints_machine_readable_evidence(capsys: pytest.CaptureFixture[str]) -> None:
    assert simulate.main(["daily_spin", "--runs", "10"]) == 0

    report = json.loads(capsys.readouterr().out)
    assert report["game_key"] == "daily_spin"
    assert report["runs"] == 10
    assert report["passed"] is True
    assert report["config_source"] == "tools.seed.CATALOGUE:daily_spin:version=1"
    assert report["config_version"] == 1


def test_configuration_validation_detects_missing_daily_spin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(simulate, "CATALOGUE", ())

    with pytest.raises(simulate.SimulationInputError):
        _ = simulate.seeded_daily_spin_reward_bands()


def test_configuration_validation_detects_malformed_daily_spin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        simulate,
        "CATALOGUE",
        (("daily_spin", "Daily Spin", "Invalid", {"game_type": "daily_spin"}),),
    )

    with pytest.raises(simulate.SimulationInputError):
        _ = simulate.seeded_daily_spin_reward_bands()


def test_configuration_validation_detects_invalid_reward_mapping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        simulate,
        "CATALOGUE",
        (
            (
                "daily_spin",
                "Daily Spin",
                "Invalid",
                {
                    "game_type": "daily_spin",
                    "cooldown_seconds": 60,
                    "rewards": [
                        {"key": "duplicate", "weight": 1, "value": 1},
                        {"key": "duplicate", "weight": 1, "value": 2},
                    ],
                },
            ),
        ),
    )

    with pytest.raises(simulate.SimulationInputError):
        _ = simulate.simulate_daily_spin(1)
