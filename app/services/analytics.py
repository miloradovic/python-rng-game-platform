"""Player-owned analytics queries."""

import uuid
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app import repositories
from app.services.errors import InvalidAnalyticsRangeError, NotFoundError


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
