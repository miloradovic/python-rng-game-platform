"""Shared clocks, idempotency fingerprints, and ownership loaders."""

import hashlib
import json
import uuid
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app import repositories
from app.models import GameSession, Outcome
from app.services.errors import ForbiddenError, NotFoundError


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
