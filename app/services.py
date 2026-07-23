"""Business use cases and explicit transaction boundaries."""

import hashlib
import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app import repositories
from app.models import (
    AuditRecord,
    FairnessProof,
    FairnessProofEvent,
    FairnessProofStatus,
    FinalScore,
    Game,
    GameConfigVersion,
    GameSession,
    Outcome,
    Player,
    PlayerStatus,
    Reward,
    RewardStatus,
    SessionStatus,
    SettlementRecipient,
    SettlementRun,
    SettlementStatus,
)
from app.rng import (
    ALGORITHM_HMAC_SHA256,
    DAILY_SPIN_MAPPING_VERSION_V1,
    PROTOCOL_VERSION_V1,
    DailySpinProof,
    FairnessError,
    OutcomeProvider,
    create_server_seed,
    daily_spin_mapping_digest,
    derive_daily_spin,
    secure_challenge,
    server_seed_commitment,
    verify_daily_spin_proof,
)
from app.rng import (
    RewardBand as FairnessRewardBand,
)
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


class DailySpinFairnessRequiredError(DomainError):
    code = "daily_spin_fairness_required"


class IdempotencyConflictError(DomainError):
    code = "idempotency_conflict"


class SessionExpiredError(DomainError):
    code = "session_expired"


class RewardUnavailableError(DomainError):
    code = "reward_unavailable"


class InvalidAnalyticsRangeError(DomainError):
    code = "invalid_analytics_range"


def utc_now() -> datetime:
    return datetime.now(UTC)


def _request_fingerprint(operation: str, material_input: dict[str, object]) -> str:
    """Hash canonical material intent for durable idempotency-key binding."""

    canonical = json.dumps(
        {"operation": operation, "input": material_input},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return hashlib.sha256(canonical.encode("ascii")).hexdigest()


class LeaderboardGameIneligibleError(DomainError):
    code = "leaderboard_game_ineligible"


class LeaderboardPeriodClosedError(DomainError):
    code = "leaderboard_period_closed"


class LeaderboardEntryNotFoundError(DomainError):
    code = "leaderboard_entry_not_found"


def leaderboard_period(completed_at: datetime) -> tuple[datetime, datetime]:
    """Return the inclusive ISO-week start and exclusive end in UTC."""

    completed_at = completed_at.astimezone(UTC)
    start = (completed_at - timedelta(days=completed_at.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return start, start + timedelta(days=7)


async def _owned_session(
    session: AsyncSession,
    *,
    session_id: uuid.UUID,
    owner_id: uuid.UUID,
    for_update: bool,
) -> GameSession:
    """Load a session and preserve the public not-found-before-forbidden contract."""

    loader = repositories.lock_session if for_update else repositories.get_session
    game_session = await loader(session, session_id)
    if game_session is None:
        raise NotFoundError
    if game_session.player_id != owner_id:
        raise ForbiddenError
    return game_session


async def _owned_outcome(
    session: AsyncSession, *, outcome_id: uuid.UUID, owner_id: uuid.UUID
) -> tuple[Outcome, GameSession]:
    """Load an outcome through its authoritative owner-bound session."""

    outcome = await repositories.get_outcome(session, outcome_id)
    if outcome is None:
        raise NotFoundError
    game_session = await _owned_session(
        session,
        session_id=outcome.session_id,
        owner_id=owner_id,
        for_update=False,
    )
    return outcome, game_session


async def _eligible_leaderboard_game(session: AsyncSession, game_key: str) -> Game:
    game = await repositories.get_game(session, game_key)
    if game is None:
        raise NotFoundError
    if not game.is_active:
        raise InactiveGameError
    if game.key != "skill_check":
        raise LeaderboardGameIneligibleError
    return game


async def submit_final_score(
    session: AsyncSession,
    *,
    session_id: uuid.UUID,
    owner_id: uuid.UUID,
    clock: Callable[[], datetime] = utc_now,
) -> tuple[FinalScore, bool]:
    """Create one server-derived score and its evidence in one durable transaction."""

    game_session = await repositories.lock_session(session, session_id)
    if game_session is None:
        raise NotFoundError
    if game_session.player_id != owner_id:
        raise ForbiddenError
    existing = await repositories.get_final_score_by_session(session, session_id)
    if existing is not None:
        await session.commit()
        return existing, False
    player = await repositories.get_player(session, owner_id)
    if player is None:
        raise NotFoundError
    if player.status != PlayerStatus.ACTIVE:
        raise InactivePlayerError
    game = await repositories.get_game_by_id(session, game_session.game_id)
    if game is None:
        raise NotFoundError
    if game.key != "skill_check":
        raise LeaderboardGameIneligibleError
    if game_session.status != SessionStatus.COMPLETED or game_session.ended_at is None:
        raise InvalidTransitionError
    outcome = await repositories.get_outcome_by_session(session, game_session.id)
    if outcome is None or outcome.status.value != "accepted":
        raise InvalidTransitionError
    config = await repositories.get_config_by_id(session, game_session.config_version_id)
    if config is None:
        raise NotFoundError
    payload = game_config_adapter.validate_python(config.payload)
    if payload.game_type != "skill_check":
        raise LeaderboardGameIneligibleError
    value = outcome.result.get("score")
    if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= payload.max_score:
        raise InvalidPlayError
    period_start, period_end = leaderboard_period(game_session.ended_at)
    if clock().astimezone(UTC) >= period_end:
        raise LeaderboardPeriodClosedError
    score = FinalScore(
        player_id=owner_id,
        game_id=game.id,
        session_id=game_session.id,
        outcome_id=outcome.id,
        config_version_id=config.id,
        period_start=period_start,
        completed_at=game_session.ended_at,
        final_score=value,
    )
    await repositories.add_final_score(session, score)
    await repositories.add_final_score_evidence(session, score, game_key=game.key)
    await session.commit()
    return score, True


async def canonical_leaderboard(
    session: AsyncSession,
    *,
    owner_id: uuid.UUID,
    game_key: str,
    period_start: datetime,
    limit: int,
    offset: int = 0,
    after: tuple[int, datetime, uuid.UUID] | None = None,
) -> tuple[Game, list[FinalScore]]:
    player = await repositories.get_player(session, owner_id)
    if player is None:
        raise NotFoundError
    if player.status != PlayerStatus.ACTIVE:
        raise InactivePlayerError
    game = await _eligible_leaderboard_game(session, game_key)
    scores = await repositories.list_canonical_scores(
        session,
        game_id=game.id,
        period_start=period_start,
        limit=limit,
        offset=offset,
        after=after,
    )
    return game, scores


async def canonical_player_rank(
    session: AsyncSession,
    *,
    player_id: uuid.UUID,
    owner_id: uuid.UUID,
    game_key: str,
    period_start: datetime,
) -> tuple[FinalScore, int]:
    if player_id != owner_id:
        raise ForbiddenError
    player = await repositories.get_player(session, player_id)
    if player is None:
        raise NotFoundError
    if player.status != PlayerStatus.ACTIVE:
        raise InactivePlayerError
    game = await _eligible_leaderboard_game(session, game_key)
    row = await repositories.player_canonical_rank(
        session, player_id=player_id, game_id=game.id, period_start=period_start
    )
    if row is None:
        raise LeaderboardEntryNotFoundError
    return row


class LeaderboardPeriodOpenError(DomainError):
    code = "leaderboard_period_open"


class SettlementForbiddenError(DomainError):
    code = "settlement_forbidden"


async def settle_leaderboard(
    session: AsyncSession,
    *,
    game_key: str,
    period_start: datetime,
    authorized: bool,
    clock: Callable[[], datetime] = utc_now,
) -> tuple[SettlementRun, list[SettlementRecipient]]:
    """Settle canonical PostgreSQL ranks exactly once for a closed period."""

    if not authorized:
        raise SettlementForbiddenError
    period_start = period_start.astimezone(UTC)
    if period_start.weekday() != 0 or any(
        (period_start.hour, period_start.minute, period_start.second, period_start.microsecond)
    ):
        raise InvalidPlayError
    game = await _eligible_leaderboard_game(session, game_key)
    await repositories.lock_game_by_id(session, game.id)
    existing = await repositories.settlement_run(
        session, game_id=game.id, period_start=period_start
    )
    if existing is not None and existing.status == SettlementStatus.COMPLETED:
        recipients = await repositories.settlement_recipients(session, existing.id)
        await session.commit()
        return existing, recipients
    period_end = period_start + timedelta(days=7)
    now = clock().astimezone(UTC)
    if now < period_end:
        raise LeaderboardPeriodOpenError
    run = existing
    if run is None:
        config = await repositories.settlement_tier_config(
            session, game_id=game.id, period_end=period_end
        )
        if config is None:
            raise NotFoundError
        tiers = config.payload.get("tiers")
        if not isinstance(tiers, list):
            raise InvalidPlayError
        run = SettlementRun(
            game_id=game.id,
            period_start=period_start,
            period_end=period_end,
            tier_config_id=config.id,
            tier_snapshot=config.payload,
            status=SettlementStatus.PROCESSING,
            completed_at=None,
        )
        session.add(run)
        await session.flush()
    else:
        tiers = run.tier_snapshot.get("tiers")
        if not isinstance(tiers, list):
            raise InvalidPlayError
    scores = await repositories.list_canonical_scores(
        session, game_id=game.id, period_start=period_start
    )
    existing_recipients = await repositories.settlement_recipients(session, run.id)
    recipients_by_score = {recipient.score_id: recipient for recipient in existing_recipients}
    seen_players: set[uuid.UUID] = set()
    for rank, score in enumerate(scores, start=1):
        if score.player_id in seen_players:
            continue
        tier = next(
            (
                item
                for item in tiers
                if isinstance(item, dict)
                and isinstance(item.get("min_rank"), int)
                and isinstance(item.get("max_rank"), int)
                and item["min_rank"] <= rank <= item["max_rank"]
            ),
            None,
        )
        if tier is None:
            continue
        key, value = tier.get("key"), tier.get("reward_value")
        if not isinstance(key, str) or not isinstance(value, int) or value < 0:
            raise InvalidPlayError
        recipient = recipients_by_score.get(score.id)
        if recipient is None:
            recipient = SettlementRecipient(
                run_id=run.id,
                player_id=score.player_id,
                score_id=score.id,
                game_id=score.game_id,
                period_start=score.period_start,
                rank=rank,
                tier_key=key,
                reward_value=value,
            )
            session.add(recipient)
            await session.flush()
            recipients_by_score[score.id] = recipient
        reward = await repositories.settlement_reward(session, recipient.id)
        if reward is None:
            reward = await repositories.add_settlement_reward(session, recipient)
            await repositories.add_reward_evidence(
                session, reward, event_type="settlement_reward_issued", game_key=game.key
            )
        seen_players.add(score.player_id)
    run.status = SettlementStatus.COMPLETED
    run.completed_at = now
    recipients = await repositories.settlement_recipients(session, run.id)
    session.add(
        AuditRecord(
            event_type="leaderboard_settled",
            entity_type="settlement_run",
            entity_id=run.id,
            evidence={
                "game_key": game.key,
                "period_start": period_start.isoformat(),
                "period_end": period_end.isoformat(),
                "tier_config_id": str(run.tier_config_id),
                "recipient_count": len(recipients),
            },
        )
    )
    await session.commit()
    return run, recipients


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
    request_fingerprint = _request_fingerprint(
        "create_session", {"player_id": str(player_id), "game_key": game_key}
    )
    await repositories.lock_session_request_id(session, request_id)
    player = await repositories.lock_player(session, player_id)
    if player is None:
        raise NotFoundError
    existing = await repositories.get_session_by_request(session, request_id)
    if existing is not None:
        if existing.request_fingerprint != request_fingerprint:
            raise IdempotencyConflictError
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
        request_fingerprint=request_fingerprint,
        expires_at=now + timedelta(seconds=duration),
        challenge=challenge,
    )
    await repositories.add_session_audit(session, game_session)
    await repositories.add_session_started_event(session, game_session, game_key=game.key)
    await session.commit()
    return game_session


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
        existing = await repositories.lock_fairness_proof_by_session(session, game_session.id)
        if existing is not None:
            await _end_committed_fairness_proof(
                session, existing, status=FairnessProofStatus.EXPIRED, now=now
            )
        await session.commit()
        raise SessionExpiredError
    if game_session.status != SessionStatus.ACTIVE:
        raise InvalidTransitionError
    game = await repositories.get_game_by_id(session, game_session.game_id)
    config = await repositories.get_config_by_id(session, game_session.config_version_id)
    if game is None or config is None or config.game_id != game.id:
        raise NotFoundError
    if game.key == "daily_spin":
        raise DailySpinFairnessRequiredError
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
    await repositories.add_game_played_event(
        session, outcome, player_id=owner_id, game_key=game.key
    )
    await repositories.add_reward_evidence(
        session, reward, event_type="reward_issued", game_key=game.key
    )
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
        game_key = await repositories.game_key_for_reward(session, reward.id)
        if game_key is None:
            raise NotFoundError
        await repositories.add_reward_evidence(
            session, reward, event_type="reward_claimed", game_key=game_key
        )
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


def validate_analytics_range(start_at: datetime | None, end_at: datetime | None) -> None:
    """Require aware timestamps and a non-empty [start, end) interval."""
    for boundary in (start_at, end_at):
        if boundary is not None and boundary.utcoffset() is None:
            raise InvalidAnalyticsRangeError
    if start_at is not None and end_at is not None and start_at >= end_at:
        raise InvalidAnalyticsRangeError


async def analytics_game_summary(
    session: AsyncSession,
    *,
    owner_id: uuid.UUID,
    start_at: datetime | None,
    end_at: datetime | None,
    game_key: str | None,
) -> list[repositories.GameSummaryRow]:
    validate_analytics_range(start_at, end_at)
    if await repositories.get_player(session, owner_id) is None:
        raise NotFoundError
    return await repositories.game_summary(
        session, player_id=owner_id, start_at=start_at, end_at=end_at, game_key=game_key
    )


async def retrieve_outcome_audit(
    session: AsyncSession, outcome_id: uuid.UUID, owner_id: uuid.UUID
) -> tuple[Outcome, dict[str, object]]:
    outcome, _ = await _owned_outcome(session, outcome_id=outcome_id, owner_id=owner_id)
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
    game_session = await _owned_session(
        session, session_id=session_id, owner_id=owner_id, for_update=False
    )
    expire_if_due(game_session, clock())
    return game_session


async def cancel_session(
    session: AsyncSession, session_id: uuid.UUID, owner_id: uuid.UUID
) -> GameSession:
    game_session = await _owned_session(
        session, session_id=session_id, owner_id=owner_id, for_update=True
    )
    now = utc_now()
    cancel_active(game_session, now)
    proof = await repositories.lock_fairness_proof_by_session(session, game_session.id)
    if proof is not None:
        await _end_committed_fairness_proof(
            session, proof, status=FairnessProofStatus.CANCELLED, now=now
        )
    await session.commit()
    return game_session


def _fairness_event_hash_v1(
    *,
    proof_id: uuid.UUID,
    sequence: int,
    event_type: str,
    status: FairnessProofStatus,
    created_at: datetime,
    previous_evidence_hash: str | None,
) -> str:
    """Produce the frozen v1 append-only event hash from explicit UTC evidence."""

    timestamp = created_at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    evidence = (
        "RNG-GAME-PLATFORM-PVF-EVENT/1\n"
        f"proof_id={proof_id}\n"
        f"sequence={sequence}\n"
        f"event_type={event_type}\n"
        f"status={status.value}\n"
        f"created_at={timestamp}\n"
        f"previous_evidence_hash={previous_evidence_hash or ''}\n"
    )
    return hashlib.sha256(evidence.encode("ascii")).hexdigest()


def _fairness_event_hash(
    *,
    proof_id: uuid.UUID,
    sequence: int,
    event_type: str,
    status: FairnessProofStatus,
    created_at: datetime,
    previous_evidence_hash: str | None,
    payload: dict[str, object],
) -> str:
    """Bind the complete v2 lifecycle payload using canonical JSON evidence."""

    evidence = {
        "created_at": created_at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        "event_type": event_type,
        "payload": payload,
        "previous_evidence_hash": previous_evidence_hash,
        "proof_id": str(proof_id),
        "sequence": sequence,
        "status": status.value,
    }
    encoded = json.dumps(evidence, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(("RNG-GAME-PLATFORM-PVF-EVENT/2\n" + encoded).encode("ascii")).hexdigest()


def _commit_event_payload(proof: FairnessProof) -> dict[str, object]:
    return {
        "algorithm": proof.algorithm,
        "config_version_id": str(proof.config_version_id),
        "game_key": proof.game_key,
        "mapping_digest": proof.mapping_digest,
        "mapping_version": proof.mapping_version,
        "nonce": proof.nonce,
        "protocol_version": proof.protocol_version,
        "server_seed_commitment": proof.server_seed_commitment,
        "session_id": str(proof.session_id),
    }


def _terminal_event_payload(proof: FairnessProof, now: datetime) -> dict[str, object]:
    return {
        **_commit_event_payload(proof),
        "terminal_at": now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
    }


def _revealed_event_payload(proof: FairnessProof) -> dict[str, object]:
    return {
        **_commit_event_payload(proof),
        "client_seed": proof.client_seed,
        "derivation_attempt": proof.derivation_attempt,
        "evaluated_at": _optional_timestamp(proof.evaluated_at),
        "evaluation_fingerprint": proof.evaluation_fingerprint,
        "normalized_value": proof.normalized_value,
        "outcome_id": str(proof.outcome_id) if proof.outcome_id is not None else None,
        "raw_random_value": proof.raw_random_value,
        "revealed_at": _optional_timestamp(proof.revealed_at),
        "reward_key": proof.reward_key,
        "reward_value": proof.reward_value,
        "server_seed_revealed": proof.server_seed_revealed,
    }


def _optional_timestamp(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _daily_spin_fairness_bands(config: GameConfigVersion) -> tuple[FairnessRewardBand, ...]:
    payload = game_config_adapter.validate_python(config.payload)
    if payload.game_type != "daily_spin":
        raise InvalidPlayError
    return tuple(
        FairnessRewardBand(reward.key, reward.weight, reward.value) for reward in payload.rewards
    )


async def _end_committed_fairness_proof(
    session: AsyncSession,
    proof: FairnessProof,
    *,
    status: FairnessProofStatus,
    now: datetime,
) -> None:
    """Terminally retire an unrevealed proof and its seed custody evidence."""

    if proof.status != FairnessProofStatus.COMMITTED:
        return
    if status not in (FairnessProofStatus.EXPIRED, FairnessProofStatus.CANCELLED):
        raise InvalidTransitionError
    previous_event = await repositories.lock_latest_fairness_proof_event(session, proof.id)
    if previous_event is None:
        raise InvalidTransitionError
    proof.status = status
    await repositories.delete_fairness_seed_custody(session, proof.id)
    sequence = previous_event.sequence + 1
    event_type = status.value
    payload = _terminal_event_payload(proof, now)
    await repositories.add_fairness_proof_event(
        session,
        FairnessProofEvent(
            proof_id=proof.id,
            sequence=sequence,
            event_type=event_type,
            evidence_version=2,
            payload=payload,
            status=status,
            previous_evidence_hash=previous_event.evidence_hash,
            evidence_hash=_fairness_event_hash(
                proof_id=proof.id,
                sequence=sequence,
                event_type=event_type,
                status=status,
                created_at=now,
                previous_evidence_hash=previous_event.evidence_hash,
                payload=payload,
            ),
            created_at=now,
        ),
    )


async def commit_fairness(
    session: AsyncSession,
    *,
    session_id: uuid.UUID,
    owner_id: uuid.UUID,
    clock: Callable[[], datetime] = utc_now,
) -> FairnessProof:
    """Durably commit a server seed before any daily-spin outcome is evaluated."""

    game_session = await repositories.lock_session(session, session_id)
    if game_session is None:
        raise NotFoundError
    if game_session.player_id != owner_id:
        raise ForbiddenError
    now = clock()
    if expire_if_due(game_session, now):
        existing = await repositories.lock_fairness_proof_by_session(session, game_session.id)
        if existing is not None:
            await _end_committed_fairness_proof(
                session, existing, status=FairnessProofStatus.EXPIRED, now=now
            )
        await session.commit()
        raise SessionExpiredError
    if game_session.status != SessionStatus.ACTIVE:
        raise InvalidTransitionError
    game = await repositories.get_game_by_id(session, game_session.game_id)
    config = await repositories.get_config_by_id(session, game_session.config_version_id)
    if game is None or config is None or game.key != "daily_spin" or config.game_id != game.id:
        raise InvalidPlayError
    existing = await repositories.lock_fairness_proof_by_session(session, game_session.id)
    if existing is not None:
        await session.commit()
        return existing
    bands = _daily_spin_fairness_bands(config)
    seed = create_server_seed()
    proof = FairnessProof(
        session_id=game_session.id,
        player_id=owner_id,
        game_id=game.id,
        game_key=game.key,
        config_version_id=config.id,
        outcome_id=None,
        status=FairnessProofStatus.COMMITTED,
        protocol_version=PROTOCOL_VERSION_V1,
        algorithm=ALGORITHM_HMAC_SHA256,
        server_seed_commitment=server_seed_commitment(seed),
        nonce=0,
        client_seed=None,
        evaluation_fingerprint=None,
        mapping_version=DAILY_SPIN_MAPPING_VERSION_V1,
        mapping_digest=daily_spin_mapping_digest(bands),
        raw_random_value=None,
        normalized_value=None,
        derivation_attempt=None,
        reward_key=None,
        reward_value=None,
        evaluated_at=None,
        server_seed_revealed=None,
        revealed_at=None,
    )
    proof = await repositories.add_fairness_proof(session, proof)
    await repositories.add_fairness_seed_custody(session, proof.id, seed)
    payload = _commit_event_payload(proof)
    event = FairnessProofEvent(
        proof_id=proof.id,
        sequence=0,
        event_type="committed",
        evidence_version=2,
        payload=payload,
        status=FairnessProofStatus.COMMITTED,
        previous_evidence_hash=None,
        evidence_hash=_fairness_event_hash(
            proof_id=proof.id,
            sequence=0,
            event_type="committed",
            status=FairnessProofStatus.COMMITTED,
            created_at=now,
            previous_evidence_hash=None,
            payload=payload,
        ),
        created_at=now,
    )
    await repositories.add_fairness_proof_event(session, event)
    await session.commit()
    return proof


async def evaluate_fairness(
    session: AsyncSession,
    *,
    proof_id: uuid.UUID,
    owner_id: uuid.UUID,
    client_seed: str,
    clock: Callable[[], datetime] = utc_now,
) -> tuple[Outcome, Reward, FairnessProof]:
    """Atomically reveal a committed daily-spin outcome and its one reward entitlement."""

    evaluation_fingerprint = _request_fingerprint(
        "evaluate_fairness", {"proof_id": str(proof_id), "client_seed": client_seed}
    )
    visible_proof = await repositories.get_fairness_proof(session, proof_id)
    if visible_proof is None:
        raise NotFoundError
    proof_owner_id, proof_session_id = visible_proof
    if proof_owner_id != owner_id:
        raise ForbiddenError
    game_session = await repositories.lock_session(session, proof_session_id)
    if game_session is None:
        raise NotFoundError
    proof = await repositories.lock_fairness_proof(session, proof_id)
    if proof is None:
        raise NotFoundError
    if proof.status == FairnessProofStatus.REVEALED:
        if proof.evaluation_fingerprint != evaluation_fingerprint:
            raise IdempotencyConflictError
        if proof.outcome_id is None:
            raise InvalidTransitionError
        outcome = await repositories.get_outcome(session, proof.outcome_id)
        reward = await repositories.lock_reward_by_session(session, proof.session_id)
        if outcome is None or reward is None:
            raise NotFoundError
        await session.commit()
        return outcome, reward, proof
    now = clock()
    if expire_if_due(game_session, now):
        await _end_committed_fairness_proof(
            session, proof, status=FairnessProofStatus.EXPIRED, now=now
        )
        await session.commit()
        raise SessionExpiredError
    if proof.status != FairnessProofStatus.COMMITTED or game_session.status != SessionStatus.ACTIVE:
        raise InvalidTransitionError
    game = await repositories.get_game_by_id(session, proof.game_id)
    config = await repositories.get_config_by_id(session, proof.config_version_id)
    custody = await repositories.get_fairness_seed_custody(session, proof.id)
    if game is None or config is None or custody is None or game.key != "daily_spin":
        raise NotFoundError
    try:
        derivation = derive_daily_spin(
            server_seed=custody.server_seed_material,
            client_seed=client_seed,
            nonce=proof.nonce,
            config_version_id=proof.config_version_id,
            session_id=proof.session_id,
            reward_bands=_daily_spin_fairness_bands(config),
            game_key=proof.game_key,
            protocol_version=proof.protocol_version,
            algorithm=proof.algorithm,
            mapping_version=proof.mapping_version,
        )
    except FairnessError as error:
        raise InvalidPlayError from error
    outcome = await repositories.add_outcome(
        session,
        game_session,
        {
            "reward_key": derivation.reward.key,
            "normalized_value": derivation.normalized_value,
            "derivation_digest": derivation.raw_digest_hex,
        },
    )
    reward = await repositories.add_reward(
        session, outcome, player_id=owner_id, value=derivation.reward.value
    )
    proof.outcome_id = outcome.id
    proof.status = FairnessProofStatus.REVEALED
    proof.client_seed = client_seed
    proof.evaluation_fingerprint = evaluation_fingerprint
    proof.raw_random_value = derivation.raw_digest_hex
    proof.normalized_value = derivation.normalized_value
    proof.derivation_attempt = derivation.attempt
    proof.reward_key = derivation.reward.key
    proof.reward_value = derivation.reward.value
    proof.evaluated_at = now
    proof.server_seed_revealed = custody.server_seed_material.hex()
    proof.revealed_at = now
    previous_event = await repositories.lock_latest_fairness_proof_event(session, proof.id)
    if previous_event is None:
        raise InvalidTransitionError
    payload = _revealed_event_payload(proof)
    event = FairnessProofEvent(
        proof_id=proof.id,
        sequence=1,
        event_type="revealed",
        evidence_version=2,
        payload=payload,
        status=FairnessProofStatus.REVEALED,
        previous_evidence_hash=previous_event.evidence_hash,
        evidence_hash="0" * 64,
        created_at=now,
    )
    event.evidence_hash = _fairness_event_hash(
        proof_id=proof.id,
        sequence=1,
        event_type="revealed",
        status=FairnessProofStatus.REVEALED,
        created_at=now,
        previous_evidence_hash=event.previous_evidence_hash,
        payload=payload,
    )
    await repositories.add_fairness_proof_event(session, event)
    game_session.status = SessionStatus.COMPLETED
    game_session.ended_at = now
    await repositories.add_outcome_audit(session, outcome, player_id=owner_id, game_key=game.key)
    await repositories.add_game_played_event(
        session, outcome, player_id=owner_id, game_key=game.key
    )
    await repositories.add_reward_evidence(
        session, reward, event_type="reward_issued", game_key=game.key
    )
    await session.commit()
    return outcome, reward, proof


@dataclass(frozen=True)
class RevealedFairnessProof:
    """Immutable, non-null finalized proof evidence for response and verification use."""

    proof_id: uuid.UUID
    outcome_id: uuid.UUID
    status: FairnessProofStatus
    protocol_version: str
    algorithm: str
    server_seed_commitment: str
    server_seed_revealed: str
    client_seed: str
    nonce: int
    game_key: str
    session_id: uuid.UUID
    config_version_id: uuid.UUID
    mapping_version: str
    mapping_digest: str
    raw_random_value: str
    normalized_value: int
    derivation_attempt: int
    reward_key: str
    reward_value: int
    evaluated_at: datetime
    revealed_at: datetime


def _as_revealed_proof(proof: FairnessProof) -> RevealedFairnessProof:
    material = (
        proof.outcome_id,
        proof.server_seed_revealed,
        proof.client_seed,
        proof.raw_random_value,
        proof.normalized_value,
        proof.derivation_attempt,
        proof.reward_key,
        proof.reward_value,
        proof.evaluated_at,
        proof.revealed_at,
    )
    if proof.status != FairnessProofStatus.REVEALED or any(value is None for value in material):
        raise InvalidTransitionError
    outcome_id, seed, client_seed, raw, normalized, attempt, key, value, evaluated, revealed = (
        material
    )
    if not (
        isinstance(outcome_id, uuid.UUID)
        and isinstance(seed, str)
        and isinstance(client_seed, str)
        and isinstance(raw, str)
        and isinstance(normalized, int)
        and isinstance(attempt, int)
        and isinstance(key, str)
        and isinstance(value, int)
        and isinstance(evaluated, datetime)
        and isinstance(revealed, datetime)
    ):
        raise InvalidTransitionError
    return RevealedFairnessProof(
        proof_id=proof.id,
        outcome_id=outcome_id,
        status=proof.status,
        protocol_version=proof.protocol_version,
        algorithm=proof.algorithm,
        server_seed_commitment=proof.server_seed_commitment,
        server_seed_revealed=seed,
        client_seed=client_seed,
        nonce=proof.nonce,
        game_key=proof.game_key,
        session_id=proof.session_id,
        config_version_id=proof.config_version_id,
        mapping_version=proof.mapping_version,
        mapping_digest=proof.mapping_digest,
        raw_random_value=raw,
        normalized_value=normalized,
        derivation_attempt=attempt,
        reward_key=key,
        reward_value=value,
        evaluated_at=evaluated,
        revealed_at=revealed,
    )


def verify_fairness_event_chain(
    proof: FairnessProof, events: list[FairnessProofEvent]
) -> tuple[bool, str]:
    """Validate ordering, linkage, hashes, and the material proof snapshot."""

    if not events:
        return False, "event_chain_missing"
    previous: str | None = None
    for sequence, event in enumerate(events):
        if event.sequence != sequence or event.previous_evidence_hash != previous:
            return False, "event_chain_link_mismatch"
        if event.evidence_version == 1:
            expected = _fairness_event_hash_v1(
                proof_id=event.proof_id,
                sequence=event.sequence,
                event_type=event.event_type,
                status=event.status,
                created_at=event.created_at,
                previous_evidence_hash=event.previous_evidence_hash,
            )
        elif event.evidence_version == 2:
            expected = _fairness_event_hash(
                proof_id=event.proof_id,
                sequence=event.sequence,
                event_type=event.event_type,
                status=event.status,
                created_at=event.created_at,
                previous_evidence_hash=event.previous_evidence_hash,
                payload=event.payload,
            )
        else:
            return False, "unsupported_event_version"
        if expected != event.evidence_hash:
            return False, "event_hash_mismatch"
        previous = event.evidence_hash
    if events[0].evidence_version == 2 and events[0].payload != _commit_event_payload(proof):
        return False, "commit_payload_mismatch"
    if proof.status == FairnessProofStatus.REVEALED:
        final = events[-1]
        if final.status != FairnessProofStatus.REVEALED:
            return False, "event_terminal_state_mismatch"
        if final.evidence_version == 2 and final.payload != _revealed_event_payload(proof):
            return False, "revealed_payload_mismatch"
    return True, "verified"


async def retrieve_fairness_proof(
    session: AsyncSession, *, outcome_id: uuid.UUID, owner_id: uuid.UUID
) -> tuple[RevealedFairnessProof, tuple[FairnessRewardBand, ...]]:
    """Return immutable finalized proof evidence owned by the requesting player."""

    outcome, _ = await _owned_outcome(session, outcome_id=outcome_id, owner_id=owner_id)
    proof = await repositories.get_fairness_proof_by_outcome(session, outcome.id)
    if proof is None:
        raise NotFoundError
    revealed = _as_revealed_proof(proof)
    config = await repositories.get_config_by_id(session, proof.config_version_id)
    if config is None:
        raise NotFoundError
    return revealed, _daily_spin_fairness_bands(config)


async def verify_fairness_proof(
    session: AsyncSession, *, outcome_id: uuid.UUID, owner_id: uuid.UUID
) -> tuple[RevealedFairnessProof, tuple[FairnessRewardBand, ...], str, bool]:
    """Recalculate proof derivation and validate its append-only lifecycle chain."""

    proof_model = await repositories.get_fairness_proof_by_outcome(session, outcome_id)
    proof, bands = await retrieve_fairness_proof(session, outcome_id=outcome_id, owner_id=owner_id)
    if proof_model is None:
        raise NotFoundError
    result = verify_daily_spin_proof(
        DailySpinProof(
            protocol_version=proof.protocol_version,
            algorithm=proof.algorithm,
            server_seed_commitment=proof.server_seed_commitment,
            server_seed_hex=proof.server_seed_revealed,
            client_seed=proof.client_seed,
            nonce=proof.nonce,
            game_key=proof.game_key,
            config_version_id=proof.config_version_id,
            session_id=proof.session_id,
            reward_bands=bands,
            mapping_version=proof.mapping_version,
            mapping_digest=proof.mapping_digest,
            raw_digest_hex=proof.raw_random_value,
            normalized_value=proof.normalized_value,
            attempt=proof.derivation_attempt,
            reward_key=proof.reward_key,
            reward_value=proof.reward_value,
        )
    )
    if not result.verified:
        return proof, bands, result.code, False
    events = await repositories.list_fairness_proof_events(session, proof.proof_id)
    chain_verified, chain_code = verify_fairness_event_chain(proof_model, events)
    return proof, bands, chain_code, chain_verified
