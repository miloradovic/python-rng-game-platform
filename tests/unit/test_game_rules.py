"""Independent coverage for the explicit deterministic game-rules registry."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from app.game_rules import (
    DailySpinRules,
    DirectGameEntry,
    FairnessGameEntry,
    GameKey,
    InvalidRulesInputError,
    PlayIntent,
    PredictionCardRules,
    SkillCheckRules,
    fairness_rules_for,
    leaderboard_rules_for,
    registered_game_keys,
    rules_for,
    settlement_rules_for,
)
from app.models import GameConfigVersion, GameSession, Outcome
from app.rng import HmacOutcomeProvider, daily_spin_mapping_digest

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


def test_registry_exposes_only_supported_typed_capabilities() -> None:
    daily_spin = rules_for(GameKey.DAILY_SPIN)
    prediction_card = rules_for(GameKey.PREDICTION_CARD)
    skill_check = rules_for(GameKey.SKILL_CHECK)

    assert isinstance(daily_spin, FairnessGameEntry)
    assert daily_spin.mode == "fairness"
    assert isinstance(daily_spin.definition, DailySpinRules)
    assert isinstance(daily_spin.fairness, DailySpinRules)
    assert isinstance(daily_spin.leaderboard, DailySpinRules)
    assert daily_spin.settlement is False
    assert not hasattr(daily_spin.definition, "evaluate")
    assert not hasattr(daily_spin, "direct_play")

    assert isinstance(prediction_card, DirectGameEntry)
    assert prediction_card.mode == "direct"
    assert isinstance(prediction_card.definition, PredictionCardRules)
    assert isinstance(prediction_card.direct_play, PredictionCardRules)
    assert isinstance(prediction_card.leaderboard, PredictionCardRules)
    assert prediction_card.settlement is False

    assert isinstance(skill_check, DirectGameEntry)
    assert skill_check.mode == "direct"
    assert isinstance(skill_check.definition, SkillCheckRules)
    assert isinstance(skill_check.direct_play, SkillCheckRules)
    assert isinstance(skill_check.leaderboard, SkillCheckRules)
    assert skill_check.settlement is True


def test_registry_is_complete_for_every_defined_game_key() -> None:
    assert registered_game_keys() == frozenset(GameKey)
    for game_key in GameKey:
        assert rules_for(game_key).definition.key is game_key


def test_registry_fails_closed_for_unknown_game() -> None:
    with pytest.raises(InvalidRulesInputError):
        rules_for("unknown")


@pytest.mark.parametrize("game_key", list(GameKey))
def test_every_registered_game_exposes_leaderboard_capability(game_key: GameKey) -> None:
    assert leaderboard_rules_for(game_key) is rules_for(game_key).leaderboard


def test_fairness_capability_rejects_direct_play_game() -> None:
    with pytest.raises(InvalidRulesInputError):
        fairness_rules_for(GameKey.PREDICTION_CARD)


@pytest.mark.parametrize("game_key", [GameKey.DAILY_SPIN, GameKey.PREDICTION_CARD])
def test_settlement_capability_preserves_skill_check_only_policy(game_key: GameKey) -> None:
    with pytest.raises(InvalidRulesInputError):
        settlement_rules_for(game_key)

    assert settlement_rules_for(GameKey.SKILL_CHECK) is leaderboard_rules_for(GameKey.SKILL_CHECK)


def test_prediction_rules_are_deterministic() -> None:
    entry = rules_for(GameKey.PREDICTION_CARD)
    assert entry.mode == "direct"
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

    first = entry.direct_play.evaluate(
        game_session=game_session, config=config, intent=intent, provider=provider, now=now
    )
    second = entry.direct_play.evaluate(
        game_session=game_session, config=config, intent=intent, provider=provider, now=now
    )

    assert first == second
    outcome = Outcome(session_id=game_session.id, config_version_id=config.id, result=first)
    assert entry.definition.reward_value(config, outcome) in (0, 25)


@pytest.mark.parametrize(
    ("correct", "expected"),
    [(True, 25), (False, 0)],
)
def test_prediction_leaderboard_score_uses_configured_reward(correct: bool, expected: int) -> None:
    config = _config(
        {
            "game_type": "prediction_card",
            "cooldown_seconds": 60,
            "choices": ["red", "black"],
            "correct_reward": 25,
        }
    )
    outcome = Outcome(session_id=uuid4(), config_version_id=config.id, result={"correct": correct})

    assert (
        leaderboard_rules_for(GameKey.PREDICTION_CARD).leaderboard_score(config, outcome)
        == expected
    )


def test_skill_rules_derive_score_without_infrastructure() -> None:
    entry = rules_for(GameKey.SKILL_CHECK)
    assert entry.mode == "direct"
    config = _config(
        {
            "game_type": "skill_check",
            "cooldown_seconds": 0,
            "duration_seconds": 30,
            "max_score": 1000,
        }
    )
    game_session = _session({"sequence": [1, 2, 3]})
    result = entry.direct_play.evaluate(
        game_session=game_session,
        config=config,
        intent=PlayIntent(choice=None, actions=[1, 2, 9]),
        provider=HmacOutcomeProvider("x" * 32),
        now=datetime(2026, 1, 1, tzinfo=UTC),
    )

    assert result["score"] == 666
    outcome = Outcome(session_id=game_session.id, config_version_id=config.id, result=result)
    assert entry.definition.reward_value(config, outcome) == 666
    assert entry.leaderboard is not None
    assert entry.leaderboard.leaderboard_score(config, outcome) == 666


@pytest.mark.parametrize("score", [-1, 1001, True, "100"])
def test_skill_leaderboard_score_rejects_invalid_outcome(score: object) -> None:
    config = _config(
        {
            "game_type": "skill_check",
            "cooldown_seconds": 0,
            "duration_seconds": 30,
            "max_score": 1000,
        }
    )
    outcome = Outcome(session_id=uuid4(), config_version_id=config.id, result={"score": score})

    with pytest.raises(InvalidRulesInputError):
        leaderboard_rules_for(GameKey.SKILL_CHECK).leaderboard_score(config, outcome)


def test_daily_spin_exposes_real_fairness_configuration_without_direct_play() -> None:
    entry = rules_for(GameKey.DAILY_SPIN)
    assert entry.mode == "fairness"
    config = _config(
        {
            "game_type": "daily_spin",
            "cooldown_seconds": 60,
            "rewards": [
                {"key": "a", "weight": 1, "value": 1},
                {"key": "b", "weight": 2, "value": 2},
            ],
        }
    )

    bands = entry.fairness.fairness_reward_bands(config)

    assert [(band.key, band.weight, band.value) for band in bands] == [
        ("a", 1, 1),
        ("b", 2, 2),
    ]

    outcome = Outcome(session_id=uuid4(), config_version_id=config.id, result={"reward_key": "b"})
    assert leaderboard_rules_for(GameKey.DAILY_SPIN).leaderboard_score(config, outcome) == 2


def test_daily_spin_leaderboard_score_rejects_unknown_reward_key() -> None:
    config = _config(
        {
            "game_type": "daily_spin",
            "cooldown_seconds": 3600,
            "rewards": [
                {"key": "coins_10", "weight": 1, "value": 10},
                {"key": "coins_50", "weight": 1, "value": 50},
            ],
        }
    )
    outcome = Outcome(
        session_id=uuid4(), config_version_id=config.id, result={"reward_key": "unknown"}
    )

    with pytest.raises(InvalidRulesInputError):
        leaderboard_rules_for(GameKey.DAILY_SPIN).leaderboard_score(config, outcome)


def test_seeded_fairness_reward_bands_preserve_mapping_fixed_vector() -> None:
    config = _config(
        {
            "game_type": "daily_spin",
            "cooldown_seconds": 86400,
            "rewards": [
                {"key": "coins_10", "weight": 80, "value": 10},
                {"key": "coins_50", "weight": 20, "value": 50},
            ],
        }
    )

    bands = fairness_rules_for(GameKey.DAILY_SPIN).fairness_reward_bands(config)

    assert daily_spin_mapping_digest(bands) == (
        "759a58aa1263a2c7a33e85f7192f1a51d67ad32ef4e95439762fe5717eba4a33"
    )
