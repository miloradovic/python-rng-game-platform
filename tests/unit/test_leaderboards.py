"""Deterministic leaderboard period and Redis encoding tests."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from app.cache import leaderboard_member, parse_leaderboard_member
from app.services import leaderboard_period

pytestmark = pytest.mark.unit


def test_iso_week_period_boundaries_are_utc() -> None:
    start, end = leaderboard_period(datetime(2026, 7, 19, 23, 59, tzinfo=UTC))
    assert start == datetime(2026, 7, 13, tzinfo=UTC)
    assert end == datetime(2026, 7, 20, tzinfo=UTC)


def test_projection_member_round_trips_total_order_fields() -> None:
    session_id, score_id, player_id = (str(uuid4()) for _ in range(3))
    member = leaderboard_member(
        completed_at_us=123, session_id=session_id, score_id=score_id, player_id=player_id
    )
    assert parse_leaderboard_member(member) == (123, session_id, score_id, player_id)
