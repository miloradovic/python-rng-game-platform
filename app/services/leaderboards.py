"""Scores, canonical ranks, periods, and settlement use cases."""

import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app import repositories
from app.game_rules import InvalidRulesInputError, LeaderboardScoreExtraction, leaderboard_rules_for
from app.models import (
    AuditRecord,
    FinalScore,
    Game,
    PlayerStatus,
    SessionStatus,
    SettlementRecipient,
    SettlementRun,
    SettlementStatus,
)
from app.services._common import utc_now
from app.services.errors import (
    ForbiddenError,
    InactiveGameError,
    InactivePlayerError,
    InvalidPlayError,
    InvalidTransitionError,
    LeaderboardEntryNotFoundError,
    LeaderboardGameIneligibleError,
    LeaderboardPeriodClosedError,
    LeaderboardPeriodOpenError,
    NotFoundError,
    SettlementForbiddenError,
)


def leaderboard_period(completed_at: datetime) -> tuple[datetime, datetime]:
    """Return the inclusive ISO-week start and exclusive end in UTC."""

    completed_at = completed_at.astimezone(UTC)
    start = (completed_at - timedelta(days=completed_at.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return start, start + timedelta(days=7)


async def _eligible_leaderboard_game(session: AsyncSession, game_key: str) -> Game:
    game = await repositories.get_game(session, game_key)
    if game is None:
        raise NotFoundError
    if not game.is_active:
        raise InactiveGameError
    try:
        leaderboard_rules_for(game.key)
    except InvalidRulesInputError as error:
        raise LeaderboardGameIneligibleError from error
    return game


def _leaderboard_capability(game_key: str) -> LeaderboardScoreExtraction:
    try:
        return leaderboard_rules_for(game_key)
    except InvalidRulesInputError as error:
        raise LeaderboardGameIneligibleError from error


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
    leaderboard = _leaderboard_capability(game.key)
    if game_session.status != SessionStatus.COMPLETED or game_session.ended_at is None:
        raise InvalidTransitionError
    outcome = await repositories.get_outcome_by_session(session, game_session.id)
    if outcome is None or outcome.status.value != "accepted":
        raise InvalidTransitionError
    config = await repositories.get_config_by_id(session, game_session.config_version_id)
    if config is None:
        raise NotFoundError
    try:
        value = leaderboard.leaderboard_score(config, outcome)
    except InvalidRulesInputError as error:
        raise InvalidPlayError from error
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


async def leaderboard_projection_facts(
    session: AsyncSession,
    *,
    owner_id: uuid.UUID,
    game_key: str,
    period_start: datetime,
    player_id: uuid.UUID | None = None,
) -> tuple[Game, int, int]:
    """Authorize a read and return only durable projection validation facts."""

    if player_id is not None and player_id != owner_id:
        raise ForbiddenError
    player = await repositories.get_player(session, owner_id)
    if player is None:
        raise NotFoundError
    if player.status != PlayerStatus.ACTIVE:
        raise InactivePlayerError
    game = await _eligible_leaderboard_game(session, game_key)
    count = await repositories.count_canonical_scores(
        session, game_id=game.id, period_start=period_start
    )
    revision = await repositories.leaderboard_projection_revision(
        session, game_id=game.id, period_start=period_start
    )
    return game, count, revision


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
