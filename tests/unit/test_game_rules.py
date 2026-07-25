"""Independent coverage for the explicit deterministic game-rules registry."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from app.game_rules import (
    DailySpinRules,
    GameKey,
    InvalidRulesInputError,
    PlayIntent,
    PredictionCardRules,
    SkillCheckRules,
    rules_for,
)
from app.models import GameConfigVersion, GameSession, Outcome
from app.rng import HmacOutcomeProvider

pytestmark = pytest.mark.unit


def _config(payload: dict[str, object]) -> GameConfigVersion:
    return GameConfigVersion(id=uuid4(), game_id=uuid4(), version=1, payload=payload)


def _session(challenge: dict[str, object] | None = None) -> GameSession:
    return GameSession(
        id=uuid4(),
        request_id=uuid4(),
        player_id=uuid4(),
        game_id=uuid4(),
        config_version_id=uuid4(),
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        expires_at=datetime(2026, 1, 2, tzinfo=UTC),
        challenge=challenge or {},
    )


def test_registry_exposes_explicit_typed_capabilities() -> None:
    assert isinstance(rules_for(GameKey.DAILY_SPIN), DailySpinRules)
    assert isinstance(rules_for(GameKey.PREDICTION_CARD), PredictionCardRules)
    assert isinstance(rules_for(GameKey.SKILL_CHECK), SkillCheckRules)
    assert rules_for(GameKey.DAILY_SPIN).capabilities.fairness
    assert rules_for(GameKey.SKILL_CHECK).capabilities.leaderboard


def test_registry_fails_closed_for_unknown_game() -> None:
    with pytest.raises(InvalidRulesInputError):
        rules_for("unknown")


def test_prediction_rules_are_deterministic() -> None:
    rules = rules_for(GameKey.PREDICTION_CARD)
    config = _config(
        {
            "game_type": "prediction_card",
            "cooldown_seconds": 0,
            "choices": ["red", "black"],
            "correct_reward": 25,
        }
    )
    game_session = _session()
    intent = PlayIntent(choice="red", actions=None)
    provider = HmacOutcomeProvider("x" * 32)
    now = datetime(2026, 1, 1, tzinfo=UTC)

    first = rules.evaluate(
        game_session=game_session, config=config, intent=intent, provider=provider, now=now
    )
    second = rules.evaluate(
        game_session=game_session, config=config, intent=intent, provider=provider, now=now
    )

    assert first == second
    outcome = Outcome(session_id=game_session.id, config_version_id=config.id, result=first)
    assert rules.reward_value(config, outcome) in (0, 25)


def test_skill_rules_derive_score_without_infrastructure() -> None:
    rules = rules_for(GameKey.SKILL_CHECK)
    config = _config(
        {
            "game_type": "skill_check",
            "cooldown_seconds": 0,
            "duration_seconds": 30,
            "max_score": 1000,
        }
    )
    game_session = _session({"sequence": [1, 2, 3]})
    result = rules.evaluate(
        game_session=game_session,
        config=config,
        intent=PlayIntent(choice=None, actions=[1, 2, 9]),
        provider=HmacOutcomeProvider("x" * 32),
        now=datetime(2026, 1, 1, tzinfo=UTC),
    )

    assert result["score"] == 666
    outcome = Outcome(session_id=game_session.id, config_version_id=config.id, result=result)
    assert rules.reward_value(config, outcome) == 666


def test_daily_spin_generic_evaluation_is_closed() -> None:
    rules = rules_for(GameKey.DAILY_SPIN)
    with pytest.raises(InvalidRulesInputError):
        rules.evaluate(
            game_session=_session(),
            config=_config(
                {
                    "game_type": "daily_spin",
                    "cooldown_seconds": 60,
                    "rewards": [
                        {"key": "a", "weight": 1, "value": 1},
                        {"key": "b", "weight": 1, "value": 2},
                    ],
                }
            ),
            intent=PlayIntent(choice=None, actions=None),
            provider=HmacOutcomeProvider("x" * 32),
            now=datetime(2026, 1, 1, tzinfo=UTC),
        )
