"""Pydantic response schemas shared by HTTP adapters."""

from enum import StrEnum

from pydantic import BaseModel, ConfigDict


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
