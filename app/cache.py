"""Stable facade for optional Redis leaderboard projections."""

from app.redis_cache.integrity import ProjectionIntegrity, projection_integrity
from app.redis_cache.lifecycle import (
    PROJECTION_ENCODING_VERSION,
    ProjectionGeneration,
    ProjectionKeys,
    clear_redis_namespace,
    create_redis_client,
    leaderboard_key,
    leaderboard_pointer_key,
    projection_generation_keys,
    redis_is_available,
)
from app.redis_cache.projections import (
    project_final_score,
    projected_page,
    projected_player_rank,
)

__all__ = [
    "PROJECTION_ENCODING_VERSION",
    "ProjectionGeneration",
    "ProjectionIntegrity",
    "ProjectionKeys",
    "clear_redis_namespace",
    "create_redis_client",
    "leaderboard_key",
    "leaderboard_pointer_key",
    "project_final_score",
    "projected_page",
    "projected_player_rank",
    "projection_generation_keys",
    "projection_integrity",
    "redis_is_available",
]
