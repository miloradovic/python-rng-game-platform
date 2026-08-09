"""Leaderboard, score, rank, and settlement HTTP adapters."""

from datetime import UTC, datetime, timedelta
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response, status
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import (
    authorize_settlement,
    get_metrics,
    get_player_id,
    get_projection_reader,
    get_redis,
)
from app.cache import project_final_score
from app.database import get_session as get_database_session
from app.leaderboard_projection import LeaderboardProjectionReader
from app.observability import MetricsRegistry
from app.schemas import (
    FinalScoreCreate,
    FinalScoreResponse,
    LeaderboardResponse,
    PlayerRankResponse,
    SettlementRecipientResponse,
    SettlementResponse,
)
from app.services import errors as service_errors
from app.services import leaderboards as leaderboard_services

router = APIRouter()
Session = Annotated[AsyncSession, Depends(get_database_session)]
OwnerId = Annotated[UUID, Depends(get_player_id)]
ProjectionReader = Annotated[LeaderboardProjectionReader, Depends(get_projection_reader)]
RedisClient = Annotated[Redis | None, Depends(get_redis)]
SettlementAuthorized = Annotated[bool, Depends(authorize_settlement)]
Metrics = Annotated[MetricsRegistry, Depends(get_metrics)]


def _period_start(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise service_errors.InvalidPlayError
    value = value.astimezone(UTC)
    if value.weekday() != 0 or any((value.hour, value.minute, value.second, value.microsecond)):
        raise service_errors.InvalidPlayError
    return value


@router.post(
    "/leaderboards/{game_key}/settle",
    response_model=SettlementResponse,
    description=(
        "Settle a closed ISO week using one deterministic best score per player. "
        "Unique players receive contiguous ranks; equal scores use earliest completion "
        "time and then session ID as tie-breakers. Each recipient's score_id identifies "
        "the exact score that established the rank."
    ),
)
async def settle_leaderboard(
    game_key: str,
    session: Session,
    authorized: SettlementAuthorized,
    metrics: Metrics,
    period_start: Annotated[datetime, Query()],
) -> SettlementResponse:
    run, recipients = await leaderboard_services.settle_leaderboard(
        session,
        game_key=game_key,
        period_start=_period_start(period_start),
        authorized=authorized,
    )
    if run.completed_at is None:
        raise service_errors.InvalidTransitionError
    metrics.increment("settlement_outcomes_total", status=run.status.value)
    return SettlementResponse(
        id=run.id,
        game_key=game_key,
        period_start=run.period_start,
        period_end=run.period_end,
        tier_config_id=run.tier_config_id,
        status=run.status,
        completed_at=run.completed_at,
        recipients=[
            SettlementRecipientResponse.model_validate(recipient) for recipient in recipients
        ],
    )


@router.post(
    "/scores",
    response_model=FinalScoreResponse,
    status_code=status.HTTP_201_CREATED,
    description=(
        "Create the completed session's server-derived score for any supported game. "
        "The client supplies only the session ID; the pinned immutable configuration "
        "and accepted outcome determine the value. A retry returns the same score."
    ),
)
async def submit_score(
    body: FinalScoreCreate,
    session: Session,
    redis: RedisClient,
    response: Response,
    owner_id: OwnerId,
) -> FinalScoreResponse:
    score, created, game_key = await leaderboard_services.submit_final_score(
        session, session_id=body.session_id, owner_id=owner_id
    )
    if not created:
        response.status_code = status.HTTP_200_OK
    else:
        await project_final_score(redis, score, game_key)
    return FinalScoreResponse.model_validate(score)


@router.get(
    "/leaderboards/{game_key}",
    response_model=LeaderboardResponse,
    description=(
        "Return the requested canonical score-entry ordering, which may contain multiple "
        "scores from one player and intentionally differs from unique-player settlement "
        "ranking. Entries expose only a generated public label and an owner-safe current "
        "player marker, never the player UUID or private display name. A Redis source is "
        "reported only after the generation matches PostgreSQL "
        "revision/count facts and passes keyed integrity validation; every other condition "
        "falls back to PostgreSQL."
    ),
)
async def get_leaderboard(
    game_key: str,
    session: Session,
    projection: ProjectionReader,
    owner_id: OwnerId,
    period_start: Annotated[datetime, Query()],
    cursor: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> LeaderboardResponse:
    start = _period_start(period_start)
    page = await projection.page(
        session,
        owner_id=owner_id,
        game_key=game_key,
        period_start=start,
        offset=cursor,
        limit=limit,
    )
    return LeaderboardResponse(
        game_key=game_key,
        period_start=start,
        period_end=start + timedelta(days=7),
        source=page.source,
        items=page.items,
        next_cursor=str(cursor + limit) if page.has_more else None,
    )


@router.get(
    "/players/{player_id}/rank",
    response_model=PlayerRankResponse,
    description=(
        "Return the authenticated player's canonical rank. Redis is optional and is used "
        "only for a current, integrity-validated generation. The entry uses the same safe "
        "public-label contract as the score-entry board."
    ),
)
async def get_player_rank(
    player_id: UUID,
    session: Session,
    projection: ProjectionReader,
    owner_id: OwnerId,
    game_key: Annotated[str, Query(min_length=1, max_length=40)],
    period_start: Annotated[datetime, Query()],
) -> PlayerRankResponse:
    start = _period_start(period_start)
    result = await projection.player_rank(
        session,
        player_id=player_id,
        owner_id=owner_id,
        game_key=game_key,
        period_start=start,
    )
    return PlayerRankResponse(
        game_key=game_key,
        period_start=start,
        period_end=start + timedelta(days=7),
        source=result.source,
        entry=result.entry,
    )
