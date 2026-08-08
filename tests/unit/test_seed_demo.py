"""Deterministic command-boundary coverage for optional stakeholder data."""

import argparse
from datetime import UTC, datetime

import pytest

from tools import seed_demo

pytestmark = pytest.mark.unit


def test_demo_seed_options_require_explicit_confirmation() -> None:
    with pytest.raises(SystemExit):
        seed_demo.parse_args([])

    args = seed_demo.parse_args(["--confirm-demo-data", "--players", "3", "--skip-projection"])

    assert args.confirm_demo_data is True
    assert args.players == 3
    assert args.skip_projection is True


@pytest.mark.parametrize("value", ["0", "11", "not-a-number"])
def test_demo_seed_rejects_unsafe_player_counts(value: str) -> None:
    with pytest.raises(SystemExit):
        seed_demo.parse_args(["--confirm-demo-data", "--players", value])


def test_demo_identities_and_weekly_request_ids_are_stable() -> None:
    period = datetime(2026, 8, 3, tzinfo=UTC)

    assert seed_demo.demo_player_id(1) == seed_demo.demo_player_id(1)
    assert seed_demo.demo_public_label(1) == seed_demo.demo_public_label(1)
    assert seed_demo.demo_public_label(1).startswith("Player-")
    assert seed_demo.demo_request_id(1, period) == seed_demo.demo_request_id(1, period)
    assert seed_demo.demo_request_id(1, period) != seed_demo.demo_request_id(2, period)


def test_player_count_parser_exposes_argparse_errors() -> None:
    with pytest.raises(argparse.ArgumentTypeError, match="integer"):
        seed_demo._player_count("invalid")
