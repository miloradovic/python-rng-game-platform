"""Deterministic reward derivation and transition coverage."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from app.models import GameConfigVersion, Outcome, Reward, RewardStatus
from app.services import InvalidPlayError, RewardUnavailableError, claim_reward, reward_value

pytestmark = pytest.mark.unit


def config(payload: dict[str, object]) -> GameConfigVersion:
    return GameConfigVersion(id=uuid4(), game_id=uuid4(), version=1, payload=payload)


@pytest.mark.parametrize(
    ("payload", "result", "expected"),
    [
        (
            {
                "game_type": "daily_spin",
                "cooldown_seconds": 60,
                "rewards": [
                    {"key": "a", "weight": 1, "value": 10},
                    {"key": "b", "weight": 1, "value": 50},
                ],
            },
            {"reward_key": "b"},
            50,
        ),
        (
            {
                "game_type": "prediction_card",
                "cooldown_seconds": 0,
                "choices": ["red", "black"],
                "correct_reward": 25,
            },
            {"correct": True},
            25,
        ),
        (
            {
                "game_type": "prediction_card",
                "cooldown_seconds": 0,
                "choices": ["red", "black"],
                "correct_reward": 25,
            },
            {"correct": False},
            0,
        ),
        (
            {
                "game_type": "skill_check",
                "cooldown_seconds": 0,
                "duration_seconds": 30,
                "max_score": 1000,
            },
            {"score": 750},
            750,
        ),
    ],
)
def test_reward_value_uses_bound_config(
    payload: dict[str, object], result: dict[str, object], expected: int
) -> None:
    outcome = Outcome(id=uuid4(), session_id=uuid4(), config_version_id=uuid4(), result=result)
    assert reward_value(config(payload), outcome) == expected


def test_unknown_daily_reward_is_rejected() -> None:
    payload = {
        "game_type": "daily_spin",
        "cooldown_seconds": 60,
        "rewards": [{"key": "a", "weight": 1, "value": 10}, {"key": "b", "weight": 1, "value": 50}],
    }
    outcome = Outcome(
        id=uuid4(), session_id=uuid4(), config_version_id=uuid4(), result={"reward_key": "forged"}
    )
    with pytest.raises(InvalidPlayError):
        reward_value(config(payload), outcome)


def test_unknown_game_fails_closed_with_stable_service_error() -> None:
    game_config = config(
        {
            "game_type": "prediction_card",
            "cooldown_seconds": 0,
            "choices": ["red", "black"],
            "correct_reward": 25,
        }
    )
    outcome = Outcome(
        id=uuid4(),
        session_id=uuid4(),
        config_version_id=game_config.id,
        result={"correct": True},
    )

    with pytest.raises(InvalidPlayError) as captured:
        reward_value(game_config, outcome, game_key="unknown")

    assert captured.value.code == "invalid_play"


def test_claim_transition_is_idempotent_and_expired_is_unavailable() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    reward = Reward(
        id=uuid4(), outcome_id=uuid4(), player_id=uuid4(), status=RewardStatus.ISSUED, value=10
    )
    assert claim_reward(reward, now) is True
    assert reward.status == RewardStatus.CLAIMED
    assert reward.claimed_at == now
    assert claim_reward(reward, datetime(2027, 1, 1, tzinfo=UTC)) is False
    assert reward.claimed_at == now
    reward.status = RewardStatus.EXPIRED
    with pytest.raises(RewardUnavailableError):
        claim_reward(reward, now)
