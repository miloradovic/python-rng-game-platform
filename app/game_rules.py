"""Deterministic game rules and the explicit supported-game registry."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol

from app.models import GameConfigVersion, GameSession, Outcome
from app.rng import OutcomeProvider, secure_challenge
from app.schemas import (
    DailySpinConfig,
    GameConfigPayload,
    PredictionCardConfig,
    SkillCheckConfig,
    game_config_adapter,
)


class GameKey(StrEnum):
    """Canonical identifiers for the supported catalogue."""

    DAILY_SPIN = "daily_spin"
    PREDICTION_CARD = "prediction_card"
    SKILL_CHECK = "skill_check"


@dataclass(frozen=True)
class GameCapabilities:
    fairness: bool
    leaderboard: bool


@dataclass(frozen=True)
class PlayIntent:
    choice: str | None
    actions: list[int] | None


class InvalidRulesInputError(ValueError):
    """Raised when deterministic rules cannot accept a play or outcome."""


class GameRules(Protocol):
    """Narrow deterministic behavior required by game orchestration."""

    key: GameKey
    capabilities: GameCapabilities

    def create_challenge(self) -> dict[str, object]: ...

    def evaluate(
        self,
        *,
        game_session: GameSession,
        config: GameConfigVersion,
        intent: PlayIntent,
        provider: OutcomeProvider,
        now: datetime,
    ) -> dict[str, object]: ...

    def reward_value(self, config: GameConfigVersion, outcome: Outcome) -> int: ...


def _validated_config(config: GameConfigVersion) -> GameConfigPayload:
    return game_config_adapter.validate_python(config.payload)


@dataclass(frozen=True)
class DailySpinRules:
    key: GameKey = GameKey.DAILY_SPIN
    capabilities: GameCapabilities = GameCapabilities(fairness=True, leaderboard=False)

    def create_challenge(self) -> dict[str, object]:
        return {}

    def evaluate(
        self,
        *,
        game_session: GameSession,
        config: GameConfigVersion,
        intent: PlayIntent,
        provider: OutcomeProvider,
        now: datetime,
    ) -> dict[str, object]:
        del game_session, config, intent, provider, now
        raise InvalidRulesInputError("daily spin requires the fairness lifecycle")

    def reward_value(self, config: GameConfigVersion, outcome: Outcome) -> int:
        payload = _validated_config(config)
        if not isinstance(payload, DailySpinConfig):
            raise InvalidRulesInputError("configuration does not match daily spin")
        reward_key = outcome.result.get("reward_key")
        for band in payload.rewards:
            if band.key == reward_key:
                return band.value
        raise InvalidRulesInputError("outcome has no configured reward")


@dataclass(frozen=True)
class PredictionCardRules:
    key: GameKey = GameKey.PREDICTION_CARD
    capabilities: GameCapabilities = GameCapabilities(fairness=False, leaderboard=False)

    def create_challenge(self) -> dict[str, object]:
        return {}

    def evaluate(
        self,
        *,
        game_session: GameSession,
        config: GameConfigVersion,
        intent: PlayIntent,
        provider: OutcomeProvider,
        now: datetime,
    ) -> dict[str, object]:
        del now
        payload = _validated_config(config)
        if (
            not isinstance(payload, PredictionCardConfig)
            or intent.choice not in payload.choices
            or intent.actions is not None
        ):
            raise InvalidRulesInputError("invalid prediction-card play")
        derived = provider.uniform(
            session_id=game_session.id,
            game_key=self.key,
            config_version_id=config.id,
            purpose=self.key,
            upper_bound=len(payload.choices),
        )
        authoritative_choice = payload.choices[derived.value]
        return {
            "player_choice": intent.choice,
            "authoritative_choice": authoritative_choice,
            "correct": intent.choice == authoritative_choice,
            "normalized_value": derived.value,
            "derivation_digest": derived.digest_hex,
        }

    def reward_value(self, config: GameConfigVersion, outcome: Outcome) -> int:
        payload = _validated_config(config)
        if not isinstance(payload, PredictionCardConfig):
            raise InvalidRulesInputError("configuration does not match prediction card")
        return payload.correct_reward if outcome.result.get("correct") is True else 0


@dataclass(frozen=True)
class SkillCheckRules:
    key: GameKey = GameKey.SKILL_CHECK
    capabilities: GameCapabilities = GameCapabilities(fairness=False, leaderboard=True)

    def create_challenge(self) -> dict[str, object]:
        return {"sequence": secure_challenge()}

    def evaluate(
        self,
        *,
        game_session: GameSession,
        config: GameConfigVersion,
        intent: PlayIntent,
        provider: OutcomeProvider,
        now: datetime,
    ) -> dict[str, object]:
        del provider
        payload = _validated_config(config)
        challenge = game_session.challenge.get("sequence")
        if (
            not isinstance(payload, SkillCheckConfig)
            or intent.actions is None
            or intent.choice is not None
            or not isinstance(challenge, list)
            or len(intent.actions) != len(set(intent.actions))
        ):
            raise InvalidRulesInputError("invalid skill-check play")
        correct_prefix = 0
        for submitted, expected in zip(intent.actions, challenge, strict=False):
            if submitted != expected:
                break
            correct_prefix += 1
        score = (payload.max_score * correct_prefix) // len(challenge)
        elapsed_ms = max(0, int((now - game_session.created_at).total_seconds() * 1000))
        return {
            "score": score,
            "correct_actions": correct_prefix,
            "submitted_actions": len(intent.actions),
            "elapsed_ms": elapsed_ms,
        }

    def reward_value(self, config: GameConfigVersion, outcome: Outcome) -> int:
        payload = _validated_config(config)
        if not isinstance(payload, SkillCheckConfig):
            raise InvalidRulesInputError("configuration does not match skill check")
        score = outcome.result.get("score")
        if (
            not isinstance(score, int)
            or isinstance(score, bool)
            or not 0 <= score <= payload.max_score
        ):
            raise InvalidRulesInputError("skill-check score is invalid")
        return score


type RegisteredRules = DailySpinRules | PredictionCardRules | SkillCheckRules
_RULES: dict[GameKey, RegisteredRules] = {
    GameKey.DAILY_SPIN: DailySpinRules(),
    GameKey.PREDICTION_CARD: PredictionCardRules(),
    GameKey.SKILL_CHECK: SkillCheckRules(),
}


def rules_for(game_key: str) -> RegisteredRules:
    """Resolve a supported game and fail closed for unknown identifiers."""

    try:
        return _RULES[GameKey(game_key)]
    except (KeyError, ValueError) as error:
        raise InvalidRulesInputError("unsupported game") from error
