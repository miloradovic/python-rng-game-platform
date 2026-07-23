"""Deterministic leaderboard period and Redis encoding tests."""

from datetime import UTC, datetime
from typing import Any, cast
from uuid import uuid4

import pytest
from redis.exceptions import TimeoutError as RedisTimeoutError

from app.cache import leaderboard_member, parse_leaderboard_member, projected_page
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


class _TimeoutPipeline:
    async def __aenter__(self) -> _TimeoutPipeline:
        return self

    async def __aexit__(self, *args: object) -> None:
        del args

    def hgetall(self, key: str) -> None:
        del key

    def zcard(self, key: str) -> None:
        del key

    async def execute(self) -> list[object]:
        raise RedisTimeoutError


class _TimeoutRedis:
    def pipeline(self, *, transaction: bool) -> _TimeoutPipeline:
        del transaction
        return _TimeoutPipeline()


async def test_projection_timeout_falls_back_without_raising() -> None:
    """A Redis timeout is a cache miss, never an API correctness failure."""

    result = await projected_page(
        cast(Any, _TimeoutRedis()),
        game_key="skill_check",
        period_start="20260720T000000Z",
        offset=0,
        limit=10,
        expected_count=1,
        expected_revision=1,
    )

    assert result is None
