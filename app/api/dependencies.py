"""Typed FastAPI dependencies for request-scoped infrastructure and identity."""

import secrets
from typing import Annotated
from uuid import UUID

from fastapi import Depends, Header, Request
from redis.asyncio import Redis

from app.config import Settings
from app.leaderboard_projection import LeaderboardProjectionReader
from app.observability import MetricsRegistry
from app.rng import OutcomeProvider


def get_app_settings(request: Request) -> Settings:
    """Return validated process settings without leaking application state to handlers."""

    settings: Settings = request.app.state.settings
    return settings


def get_outcome_provider(request: Request) -> OutcomeProvider:
    """Return the replaceable cryptographic outcome provider."""

    provider: OutcomeProvider = request.app.state.outcome_provider
    return provider


def get_redis(request: Request) -> Redis | None:
    """Return the optional disposable projection connection."""

    client: Redis | None = request.app.state.redis
    return client


def get_metrics(request: Request) -> MetricsRegistry:
    """Return the application-scoped low-cardinality measurements."""

    metrics: MetricsRegistry = request.app.state.metrics
    return metrics


def get_projection_reader(
    client: Annotated[Redis | None, Depends(get_redis)],
    metrics: Annotated[MetricsRegistry, Depends(get_metrics)],
) -> LeaderboardProjectionReader:
    """Create the focused cache-consistency policy for one request."""

    return LeaderboardProjectionReader(client, metrics)


def get_player_id(header_player_id: Annotated[UUID, Header(alias="X-Player-ID")]) -> UUID:
    """Resolve the local demo identity boundary as one typed dependency."""

    return header_player_id


def authorize_settlement(
    settings: Annotated[Settings, Depends(get_app_settings)],
    supplied: Annotated[str | None, Header(alias="X-Settlement-Token")] = None,
) -> bool:
    """Compare settlement credentials without exposing either value to handlers."""

    configured = settings.settlement_admin_token
    return bool(
        configured is not None
        and supplied is not None
        and secrets.compare_digest(configured.get_secret_value(), supplied)
    )
