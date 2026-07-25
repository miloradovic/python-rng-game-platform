"""Deterministic leaderboard period and Redis integrity tests."""

from datetime import UTC, datetime
from typing import Any, cast
from uuid import uuid4

import pytest
from redis.exceptions import TimeoutError as RedisTimeoutError

from app.cache import (
    ProjectionGeneration,
    ProjectionIntegrity,
    projected_page,
)
from app.services import leaderboard_period

pytestmark = pytest.mark.unit


def test_iso_week_period_boundaries_are_utc() -> None:
    start, end = leaderboard_period(datetime(2026, 7, 19, 23, 59, tzinfo=UTC))
    assert start == datetime(2026, 7, 13, tzinfo=UTC)
    assert end == datetime(2026, 7, 20, tzinfo=UTC)


def test_projection_member_round_trips_authenticated_order_fields() -> None:
    integrity = ProjectionIntegrity("p" * 32)
    contract = ProjectionGeneration(
        game_key="skill_check",
        period_start="20260720T000000Z",
        generation=str(uuid4()),
        revision=1,
        count=1,
        key_id=integrity.current_key_id,
    )
    session_id, score_id, player_id = (str(uuid4()) for _ in range(3))
    member = integrity.member(
        contract,
        completed_at_us=123,
        session_id=session_id,
        score_id=score_id,
        player_id=player_id,
        final_score=99,
        rank=1,
    )

    assert integrity.parse_member(contract, member, -99.0) == (
        123,
        session_id,
        score_id,
        player_id,
        99,
        1,
    )
    assert integrity.parse_member(contract, member, -100.0) is None


def test_projection_metadata_supports_previous_key_during_rotation() -> None:
    old = ProjectionIntegrity("o" * 32)
    verifier = ProjectionIntegrity("n" * 32, "o" * 32)
    generation = str(uuid4())
    contract = ProjectionGeneration(
        game_key="skill_check",
        period_start="20260720T000000Z",
        generation=generation,
        revision=3,
        count=2,
        key_id=old.current_key_id,
    )

    assert (
        verifier.verify_metadata(
            old.metadata(contract),
            game_key=contract.game_key,
            period_start=contract.period_start,
            generation=generation,
            expected_revision=3,
            expected_count=2,
        )
        == contract
    )


@pytest.mark.parametrize(
    ("expected_revision", "expected_count"),
    [(4, 2), (3, 3)],
)
def test_projection_metadata_rejects_durable_fact_mismatch(
    expected_revision: int, expected_count: int
) -> None:
    integrity = ProjectionIntegrity("p" * 32)
    generation = str(uuid4())
    contract = ProjectionGeneration(
        game_key="skill_check",
        period_start="20260720T000000Z",
        generation=generation,
        revision=3,
        count=2,
        key_id=integrity.current_key_id,
    )

    assert (
        integrity.verify_metadata(
            integrity.metadata(contract),
            game_key=contract.game_key,
            period_start=contract.period_start,
            generation=generation,
            expected_revision=expected_revision,
            expected_count=expected_count,
        )
        is None
    )


class _TimeoutRedis:
    async def get(self, key: str) -> str | None:
        del key
        raise RedisTimeoutError


async def test_projection_timeout_falls_back_without_raising() -> None:
    """A Redis timeout is a cache miss, never an API correctness failure."""

    result = await projected_page(
        cast(Any, _TimeoutRedis()),
        ProjectionIntegrity("p" * 32),
        game_key="skill_check",
        period_start="20260720T000000Z",
        offset=0,
        limit=10,
        expected_count=1,
        expected_revision=1,
    )

    assert result is None
