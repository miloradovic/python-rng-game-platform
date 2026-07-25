"""Server-authoritative gameplay orchestration."""

import uuid
from collections.abc import Callable
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app import repositories
from app.game_rules import InvalidRulesInputError, PlayIntent, rules_for
from app.models import Outcome, SessionStatus
from app.rng import OutcomeProvider
from app.services._common import _owned_outcome, utc_now
from app.services.errors import (
    DailySpinFairnessRequiredError,
    ForbiddenError,
    InvalidPlayError,
    InvalidTransitionError,
    NotFoundError,
    SessionExpiredError,
)
from app.services.rewards import reward_value
from app.services.session_termination import finalize_expired_session


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
    if await finalize_expired_session(session, game_session, now=now):
        await session.commit()
        raise SessionExpiredError
    if game_session.status != SessionStatus.ACTIVE:
        raise InvalidTransitionError
    game = await repositories.get_game_by_id(session, game_session.game_id)
    config = await repositories.get_config_by_id(session, game_session.config_version_id)
    if game is None or config is None or config.game_id != game.id:
        raise NotFoundError
    try:
        registered_game = rules_for(game.key)
    except InvalidRulesInputError as error:
        raise InvalidPlayError from error
    if registered_game.mode == "fairness":
        raise DailySpinFairnessRequiredError
    try:
        result = registered_game.direct_play.evaluate(
            game_session=game_session,
            config=config,
            intent=PlayIntent(choice=choice, actions=actions),
            provider=provider,
            now=now,
        )
    except InvalidRulesInputError as error:
        raise InvalidPlayError from error
    outcome = await repositories.add_outcome(session, game_session, result)
    reward = await repositories.add_reward(
        session, outcome, player_id=owner_id, value=reward_value(config, outcome, game.key)
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


async def retrieve_outcome_audit(
    session: AsyncSession, outcome_id: uuid.UUID, owner_id: uuid.UUID
) -> tuple[Outcome, dict[str, object]]:
    outcome, _ = await _owned_outcome(session, outcome_id=outcome_id, owner_id=owner_id)
    audit = await repositories.get_outcome_audit(session, outcome_id)
    if audit is None:
        raise NotFoundError
    return outcome, audit.evidence
