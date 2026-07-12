"""Pydantic response schemas shared by HTTP adapters."""

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from app.models import ConfigStatus, PlayerStatus


class ServiceAvailability(StrEnum):
    """Public readiness state for an application dependency."""

    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    NOT_CONFIGURED = "not_configured"


class LivenessResponse(BaseModel):
    """Process liveness response."""

    model_config = ConfigDict(frozen=True)
    status: str = "alive"


class ReadinessResponse(BaseModel):
    """Dependency readiness response."""

    model_config = ConfigDict(frozen=True)
    status: str = "ready"
    database: ServiceAvailability
    redis: ServiceAvailability


class ApiInfoResponse(BaseModel):
    """Versioned API boundary metadata."""

    model_config = ConfigDict(frozen=True)
    name: str
    version: str


class PlayerCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    display_name: Annotated[str, Field(min_length=2, max_length=50, pattern=r"^[\w .''-]+$")]


class PlayerResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True, frozen=True)
    id: UUID
    display_name: str
    status: PlayerStatus
    created_at: datetime


class RewardBand(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    key: Annotated[str, Field(min_length=1, max_length=30)]
    weight: Annotated[int, Field(gt=0)]
    value: Annotated[int, Field(ge=0)]


class DailySpinConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    game_type: Literal["daily_spin"]
    cooldown_seconds: Annotated[int, Field(ge=60, le=604800)]
    rewards: Annotated[list[RewardBand], Field(min_length=2)]


class PredictionCardConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    game_type: Literal["prediction_card"]
    cooldown_seconds: Annotated[int, Field(ge=0, le=604800)]
    choices: Annotated[list[str], Field(min_length=2, max_length=10)]
    correct_reward: Annotated[int, Field(ge=0)]


class SkillCheckConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    game_type: Literal["skill_check"]
    cooldown_seconds: Annotated[int, Field(ge=0, le=604800)]
    duration_seconds: Annotated[int, Field(ge=5, le=300)]
    max_score: Annotated[int, Field(gt=0)]


GameConfigPayload = Annotated[
    DailySpinConfig | PredictionCardConfig | SkillCheckConfig,
    Field(discriminator="game_type"),
]
game_config_adapter: TypeAdapter[GameConfigPayload] = TypeAdapter(GameConfigPayload)


class GameConfigResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True, frozen=True)
    id: UUID
    version: int
    status: ConfigStatus
    payload: GameConfigPayload
    published_at: datetime


class GameResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True, frozen=True)
    key: str
    name: str
    description: str
    is_active: bool


class GameListResponse(BaseModel):
    model_config = ConfigDict(frozen=True)
    items: list[GameResponse]
    limit: int
    offset: int
