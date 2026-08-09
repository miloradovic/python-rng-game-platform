"""Command-boundary checks for the all-game demo leaderboard tool."""

from datetime import UTC, datetime

import pytest

from app.game_rules import GameKey
from tools import seed_demo_leaderboards

pytestmark = pytest.mark.unit


def test_all_game_demo_requires_confirmation_and_has_stable_ids() -> None:
    with pytest.raises(SystemExit):
        seed_demo_leaderboards.parse_args([])

    args = seed_demo_leaderboards.parse_args(["--confirm-demo-data", "--skip-projection"])
    period = datetime(2026, 8, 3, tzinfo=UTC)
    assert args.confirm_demo_data is True
    assert args.skip_projection is True
    assert len({seed_demo_leaderboards.demo_player_id(key) for key in GameKey}) == len(GameKey)
    assert seed_demo_leaderboards.demo_request_id(GameKey.DAILY_SPIN, period) == (
        seed_demo_leaderboards.demo_request_id(GameKey.DAILY_SPIN, period)
    )
