"""Business use cases and explicit transaction boundaries."""

import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app import repositories
from app.models import (
    Game,
    GameConfigVersion,
    GameSession,
    Outcome,
    Player,
    PlayerStatus,
    Reward,
    RewardStatus,
    SessionStatus,
)
from app.rng import OutcomeProvider, secure_challenge
from app.schemas import game_config_adapter


class DomainError(Exception):
    """Stable domain failure mapped at the HTTP boundary."""

    code = "domain_error"


class NotFoundError(DomainError):
    code = "not_found"


class ForbiddenError(DomainError):
    code = "forbidden"


class InactiveGameError(DomainError):
    code = "game_inactive"


class InactivePlayerError(DomainError):
    code = "player_inactive"


class CooldownError(DomainError):
    code = "cooldown_active"


class ActiveSessionError(DomainError):
    code = "active_session_exists"


class InvalidTransitionError(DomainError):
    code = "invalid_transition"


class InvalidPlayError(DomainError):
    code = "invalid_play"


class SessionExpiredError(DomainError):
    code = "session_expired"


class RewardUnavailableError(DomainError):
    code = "reward_unavailable"


async def create_player(session: AsyncSession, display_name: str) -> Player:
    player = await repositories.add_player(session, display_name)
    await session.commit()
    return player


async def retrieve_player(
    session: AsyncSession, player_id: uuid.UUID, owner_id: uuid.UUID
) -> Player:
    if player_id != owner_id:
        raise ForbiddenError
    player = await repositories.get_player(session, player_id)
    if player is None:
        raise NotFoundError
    return player


async def catalogue(session: AsyncSession, limit: int, offset: int) -> list[Game]:
    return await repositories.list_games(session, limit, offset)


async def active_config(session: AsyncSession, game_key: str) -> GameConfigVersion:
    game = await repositories.get_game(session, game_key)
    if game is None:
        raise NotFoundError
    if not game.is_active:
        raise InactiveGameError
    config = await repositories.get_active_config(session, game.id)
    if config is None:
        raise NotFoundError
    return config


def utc_now() -> datetime:
    return datetime.now(UTC)


def expire_if_due(game_session: GameSession, now: datetime) -> bool:
    """Move an active, elapsed session to its irreversible expired state."""

    if game_session.status != SessionStatus.ACTIVE or game_session.expires_at > now:
        return False
    game_session.status = SessionStatus.EXPIRED
    game_session.ended_at = now
    return True


def cancel_active(game_session: GameSession, now: datetime) -> None:
    """Apply the owner-driven terminal transition."""

    if game_session.status != SessionStatus.ACTIVE:
        raise InvalidTransitionError
    game_session.status = SessionStatus.CANCELLED
    game_session.ended_at = now


async def create_session(
    session: AsyncSession,
    *,
    request_id: uuid.UUID,
    player_id: uuid.UUID,
    owner_id: uuid.UUID,
    game_key: str,
    clock: Callable[[], datetime] = utc_now,
) -> GameSession:
    if player_id != owner_id:
        raise ForbiddenError
    player = await repositories.lock_player(session, player_id)
    if player is None:
        raise NotFoundError
    existing = await repositories.get_session_by_request(session, player_id, request_id)
    if existing is not None:
        await session.commit()
        return existing
    if player.status != PlayerStatus.ACTIVE:
        raise InactivePlayerError
    game = await repositories.get_game(session, game_key)
    if game is None:
        raise NotFoundError
    if not game.is_active:
        raise InactiveGameError
    config = await repositories.get_active_config(session, game.id)
    if config is None:
        raise NotFoundError
    now = clock()
    await repositories.expire_active_sessions(session, player.id, game.id, now)
    latest = await repositories.latest_session(session, player.id, game.id)
    if latest is not None and latest.status == SessionStatus.ACTIVE:
        raise ActiveSessionError
    payload = game_config_adapter.validate_python(config.payload)
    if latest is not None and latest.created_at + timedelta(seconds=payload.cooldown_seconds) > now:
        raise CooldownError
    duration = getattr(payload, "duration_seconds", 300)
    challenge: dict[str, object] = {}
    if game.key == "skill_check":
        challenge = {"sequence": secure_challenge()}
    game_session = await repositories.add_session(
        session,
        request_id=request_id,
        player_id=player.id,
        game_id=game.id,
        config_version_id=config.id,
        expires_at=now + timedelta(seconds=duration),
        challenge=challenge,
    )
    await repositories.add_session_audit(session, game_session)
    await session.commit()
    return game_session


def _daily_spin_result(
    game_session: GameSession,
    game_key: str,
    config: GameConfigVersion,
    provider: OutcomeProvider,
) -> dict[str, object]:
    payload = game_config_adapter.validate_python(config.payload)
    if payload.game_type != "daily_spin":
        raise InvalidPlayError
    total_weight = sum(reward.weight for reward in payload.rewards)
    derived = provider.uniform(
        session_id=game_session.id,
        game_key=game_key,
        config_version_id=config.id,
        purpose="daily_spin",
        upper_bound=total_weight,
    )
    cursor = derived.value
    selected = payload.rewards[-1]
    for reward in payload.rewards:
        if cursor < reward.weight:
            selected = reward
            break
        cursor -= reward.weight
    return {
        "reward_key": selected.key,
        "normalized_value": derived.value,
        "derivation_digest": derived.digest_hex,
    }


def _prediction_result(
    game_session: GameSession,
    game_key: str,
    config: GameConfigVersion,
    provider: OutcomeProvider,
    choice: str | None,
) -> dict[str, object]:
    payload = game_config_adapter.validate_python(config.payload)
    if payload.game_type != "prediction_card" or choice not in payload.choices:
        raise InvalidPlayError
    derived = provider.uniform(
        session_id=game_session.id,
        game_key=game_key,
        config_version_id=config.id,
        purpose="prediction_card",
        upper_bound=len(payload.choices),
    )
    authoritative_choice = payload.choices[derived.value]
    return {
        "player_choice": choice,
        "authoritative_choice": authoritative_choice,
        "correct": choice == authoritative_choice,
        "normalized_value": derived.value,
        "derivation_digest": derived.digest_hex,
    }


def _skill_result(
    game_session: GameSession,
    config: GameConfigVersion,
    actions: list[int] | None,
    now: datetime,
) -> dict[str, object]:
    payload = game_config_adapter.validate_python(config.payload)
    challenge = game_session.challenge.get("sequence")
    if (
        payload.game_type != "skill_check"
        or actions is None
        or not isinstance(challenge, list)
        or len(actions) != len(set(actions))
    ):
        raise InvalidPlayError
    correct_prefix = 0
    for submitted, expected in zip(actions, challenge, strict=False):
        if submitted != expected:
            break
        correct_prefix += 1
    score = (payload.max_score * correct_prefix) // len(challenge)
    elapsed_ms = max(0, int((now - game_session.created_at).total_seconds() * 1000))
    return {
        "score": score,
        "correct_actions": correct_prefix,
        "submitted_actions": len(actions),
        "elapsed_ms": elapsed_ms,
    }


def reward_value(config: GameConfigVersion, outcome: Outcome) -> int:
    """Derive an entitlement only from an accepted outcome and its immutable config."""
    payload = game_config_adapter.validate_python(config.payload)
    if payload.game_type == "daily_spin":
        reward_key = outcome.result.get("reward_key")
        for band in payload.rewards:
            if band.key == reward_key:
                return band.value
        raise InvalidPlayError
    if payload.game_type == "prediction_card":
        return payload.correct_reward if outcome.result.get("correct") is True else 0
    if payload.game_type == "skill_check":
        score = outcome.result.get("score")
        if (
            not isinstance(score, int)
            or isinstance(score, bool)
            or not 0 <= score <= payload.max_score
        ):
            raise InvalidPlayError
        return score
    raise InvalidPlayError


def claim_reward(reward: Reward, now: datetime) -> bool:
    """Apply the issued-to-claimed transition; return false for an idempotent retry."""
    if reward.status == RewardStatus.CLAIMED:
        return False
    if reward.status != RewardStatus.ISSUED:
        raise RewardUnavailableError
    reward.status = RewardStatus.CLAIMED
    reward.claimed_at = now
    return True


async def play_session(
    session: AsyncSession,
    *,
    session_id: uuid.UUID,
    owner_id: uuid.UUID,
    choice: str | None,
    actions: list[int] | None,
    provider: OutcomeProvider,
    clock: Callable[[], datetime] = utc_now,
) -> Outcome:
    game_session = await repositories.lock_session(session, session_id)
    if game_session is None:
        raise NotFoundError
    if game_session.player_id != owner_id:
        raise ForbiddenError
    now = clock()
    if expire_if_due(game_session, now):
        await session.commit()
        raise SessionExpiredError
    if game_session.status != SessionStatus.ACTIVE:
        raise InvalidTransitionError
    game = await repositories.get_game_by_id(session, game_session.game_id)
    config = await repositories.get_config_by_id(session, game_session.config_version_id)
    if game is None or config is None or config.game_id != game.id:
        raise NotFoundError
    if game.key == "daily_spin":
        if choice is not None or actions is not None:
            raise InvalidPlayError
        result = _daily_spin_result(game_session, game.key, config, provider)
    elif game.key == "prediction_card":
        if actions is not None:
            raise InvalidPlayError
        result = _prediction_result(game_session, game.key, config, provider, choice)
    elif game.key == "skill_check":
        if choice is not None:
            raise InvalidPlayError
        result = _skill_result(game_session, config, actions, now)
    else:
        raise InvalidPlayError
    outcome = await repositories.add_outcome(session, game_session, result)
    reward = await repositories.add_reward(
        session, outcome, player_id=owner_id, value=reward_value(config, outcome)
    )
    game_session.status = SessionStatus.COMPLETED
    game_session.ended_at = now
    await repositories.add_outcome_audit(session, outcome, player_id=owner_id, game_key=game.key)
    await repositories.add_reward_evidence(session, reward, event_type="reward_issued")
    await session.commit()
    return outcome


async def claim_session_reward(
    session: AsyncSession,
    *,
    session_id: uuid.UUID,
    owner_id: uuid.UUID,
    clock: Callable[[], datetime] = utc_now,
) -> Reward:
    reward = await repositories.lock_reward_by_session(session, session_id)
    if reward is None:
        game_session = await repositories.get_session(session, session_id)
        if game_session is None:
            raise NotFoundError
        if game_session.player_id != owner_id:
            raise ForbiddenError
        raise RewardUnavailableError
    if reward.player_id != owner_id:
        raise ForbiddenError
    changed = claim_reward(reward, clock())
    if changed:
        await repositories.add_reward_evidence(session, reward, event_type="reward_claimed")
    await session.commit()
    return reward


async def player_rewards(
    session: AsyncSession, *, player_id: uuid.UUID, owner_id: uuid.UUID, limit: int, offset: int
) -> list[Reward]:
    if player_id != owner_id:
        raise ForbiddenError
    if await repositories.get_player(session, player_id) is None:
        raise NotFoundError
    return await repositories.list_player_rewards(session, player_id, limit, offset)


async def retrieve_outcome_audit(
    session: AsyncSession, outcome_id: uuid.UUID, owner_id: uuid.UUID
) -> tuple[Outcome, dict[str, object]]:
    outcome = await repositories.get_outcome(session, outcome_id)
    if outcome is None:
        raise NotFoundError
    game_session = await repositories.get_session(session, outcome.session_id)
    if game_session is None:
        raise NotFoundError
    if game_session.player_id != owner_id:
        raise ForbiddenError
    audit = await repositories.get_outcome_audit(session, outcome_id)
    if audit is None:
        raise NotFoundError
    return outcome, audit.evidence


async def retrieve_session(
    session: AsyncSession,
    session_id: uuid.UUID,
    owner_id: uuid.UUID,
    clock: Callable[[], datetime] = utc_now,
) -> GameSession:
    game_session = await repositories.get_session(session, session_id)
    if game_session is None:
        raise NotFoundError
    if game_session.player_id != owner_id:
        raise ForbiddenError
    if expire_if_due(game_session, clock()):
        await session.commit()
    return game_session


async def cancel_session(
    session: AsyncSession, session_id: uuid.UUID, owner_id: uuid.UUID
) -> GameSession:
    game_session = await repositories.get_session(session, session_id)
    if game_session is None:
        raise NotFoundError
    if game_session.player_id != owner_id:
        raise ForbiddenError
    cancel_active(game_session, utc_now())
    await session.commit()
    return game_session
