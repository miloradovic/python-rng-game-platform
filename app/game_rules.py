"""Deterministic game rules and the explicit supported-game registry."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Literal, Protocol

from app.models import GameConfigVersion, GameSession, Outcome
from app.rng import OutcomeProvider, RewardBand, secure_challenge
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
class PlayIntent:
    choice: str | None
    actions: list[int] | None


class InvalidRulesInputError(ValueError):
    """Raised when deterministic rules cannot accept a play or outcome."""


class GameDefinition(Protocol):
    """Behavior shared by every registered game."""

    @property
    def key(self) -> GameKey: ...

    def create_challenge(self) -> dict[str, object]: ...

    def reward_value(self, config: GameConfigVersion, outcome: Outcome) -> int: ...


class DirectPlayEvaluation(Protocol):
    """Deterministic evaluation supported by direct-play games only."""

    def evaluate(
        self,
        *,
        game_session: GameSession,
        config: GameConfigVersion,
        intent: PlayIntent,
        provider: OutcomeProvider,
        now: datetime,
    ) -> dict[str, object]: ...


class FairnessConfiguration(Protocol):
    """Provably-fair configuration supported by fairness games only."""

    def fairness_reward_bands(self, config: GameConfigVersion) -> tuple[RewardBand, ...]: ...


class LeaderboardScoreExtraction(Protocol):
    """Final-score extraction supported by leaderboard games only."""

    def leaderboard_score(self, config: GameConfigVersion, outcome: Outcome) -> int: ...


@dataclass(frozen=True)
class DirectGameEntry:
    """Registry entry whose outcome is evaluated during direct play."""

    definition: GameDefinition
    direct_play: DirectPlayEvaluation
    leaderboard: LeaderboardScoreExtraction | None = None
    mode: Literal["direct"] = "direct"


@dataclass(frozen=True)
class FairnessGameEntry:
    """Registry entry whose outcome uses commitment/reveal orchestration."""

    definition: GameDefinition
    fairness: FairnessConfiguration
    mode: Literal["fairness"] = "fairness"


def _validated_config(config: GameConfigVersion) -> GameConfigPayload:
    return game_config_adapter.validate_python(config.payload)


@dataclass(frozen=True)
class DailySpinRules:
    key: GameKey = GameKey.DAILY_SPIN

    def create_challenge(self) -> dict[str, object]:
        return {}

    def reward_value(self, config: GameConfigVersion, outcome: Outcome) -> int:
        payload = _validated_config(config)
        if not isinstance(payload, DailySpinConfig):
            raise InvalidRulesInputError("configuration does not match daily spin")
        reward_key = outcome.result.get("reward_key")
        for band in payload.rewards:
            if band.key == reward_key:
                return band.value
        raise InvalidRulesInputError("outcome has no configured reward")

    def fairness_reward_bands(self, config: GameConfigVersion) -> tuple[RewardBand, ...]:
        payload = _validated_config(config)
        if not isinstance(payload, DailySpinConfig):
            raise InvalidRulesInputError("configuration does not match daily spin")
        return tuple(RewardBand(band.key, band.weight, band.value) for band in payload.rewards)


@dataclass(frozen=True)
class PredictionCardRules:
    key: GameKey = GameKey.PREDICTION_CARD

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
        return self.leaderboard_score(config, outcome)

    def leaderboard_score(self, config: GameConfigVersion, outcome: Outcome) -> int:
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


type RegisteredGame = DirectGameEntry | FairnessGameEntry

_DAILY_SPIN_RULES = DailySpinRules()
_PREDICTION_CARD_RULES = PredictionCardRules()
_SKILL_CHECK_RULES = SkillCheckRules()
_RULES: dict[GameKey, RegisteredGame] = {
    GameKey.DAILY_SPIN: FairnessGameEntry(
        definition=_DAILY_SPIN_RULES,
        fairness=_DAILY_SPIN_RULES,
    ),
    GameKey.PREDICTION_CARD: DirectGameEntry(
        definition=_PREDICTION_CARD_RULES,
        direct_play=_PREDICTION_CARD_RULES,
    ),
    GameKey.SKILL_CHECK: DirectGameEntry(
        definition=_SKILL_CHECK_RULES,
        direct_play=_SKILL_CHECK_RULES,
        leaderboard=_SKILL_CHECK_RULES,
    ),
}


def rules_for(game_key: str) -> RegisteredGame:
    """Resolve a supported game and fail closed for unknown identifiers."""

    try:
        return _RULES[GameKey(game_key)]
    except (KeyError, ValueError) as error:
        raise InvalidRulesInputError("unsupported game") from error


def fairness_rules_for(game_key: str) -> FairnessConfiguration:
    """Resolve fairness configuration and reject games without that capability."""

    registered_game = rules_for(game_key)
    if registered_game.mode != "fairness":
        raise InvalidRulesInputError("game does not support fairness")
    return registered_game.fairness


def leaderboard_rules_for(game_key: str) -> LeaderboardScoreExtraction:
    """Resolve score extraction and reject games without that capability."""

    registered_game = rules_for(game_key)
    if registered_game.mode != "direct" or registered_game.leaderboard is None:
        raise InvalidRulesInputError("game does not support leaderboards")
    return registered_game.leaderboard


def registered_game_keys() -> frozenset[GameKey]:
    """Return the complete immutable set of explicitly registered games."""

    return frozenset(_RULES)
