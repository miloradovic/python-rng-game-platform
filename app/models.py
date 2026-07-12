"""SQLAlchemy models for the server-authoritative domain."""

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import JSON, DateTime, Enum, ForeignKey, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class PlayerStatus(StrEnum):
    ACTIVE = "active"
    INACTIVE = "inactive"


class ConfigStatus(StrEnum):
    DRAFT = "draft"
    PUBLISHED = "published"
    RETIRED = "retired"


class SessionStatus(StrEnum):
    ACTIVE = "active"
    COMPLETED = "completed"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


class OutcomeStatus(StrEnum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"


class RewardStatus(StrEnum):
    ISSUED = "issued"
    CLAIMED = "claimed"
    EXPIRED = "expired"


class Timestamped:
    """Server-owned UUID and timezone-aware creation timestamp."""

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Player(Timestamped, Base):
    __tablename__ = "players"
    display_name: Mapped[str] = mapped_column(String(50))
    status: Mapped[PlayerStatus] = mapped_column(
        Enum(PlayerStatus, name="player_status", values_callable=lambda e: [x.value for x in e]),
        default=PlayerStatus.ACTIVE,
        server_default=PlayerStatus.ACTIVE.value,
    )


class Game(Timestamped, Base):
    __tablename__ = "games"
    key: Mapped[str] = mapped_column(String(40), unique=True)
    name: Mapped[str] = mapped_column(String(80))
    description: Mapped[str] = mapped_column(String(300))
    is_active: Mapped[bool] = mapped_column(default=True, server_default="true")


class GameConfigVersion(Timestamped, Base):
    __tablename__ = "game_config_versions"
    game_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("games.id", ondelete="RESTRICT"))
    version: Mapped[int]
    status: Mapped[ConfigStatus] = mapped_column(
        Enum(ConfigStatus, name="config_status", values_callable=lambda e: [x.value for x in e])
    )
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class GameSession(Timestamped, Base):
    __tablename__ = "game_sessions"
    player_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("players.id", ondelete="RESTRICT"))
    game_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("games.id", ondelete="RESTRICT"))
    config_version_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("game_config_versions.id"))
    status: Mapped[SessionStatus] = mapped_column(
        Enum(SessionStatus, name="session_status", values_callable=lambda e: [x.value for x in e])
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Outcome(Timestamped, Base):
    __tablename__ = "outcomes"
    session_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("game_sessions.id"), unique=True)
    config_version_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("game_config_versions.id"))
    status: Mapped[OutcomeStatus] = mapped_column(
        Enum(OutcomeStatus, name="outcome_status", values_callable=lambda e: [x.value for x in e])
    )
    result: Mapped[dict[str, Any]] = mapped_column(JSON)


class Reward(Timestamped, Base):
    __tablename__ = "rewards"
    outcome_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("outcomes.id"), unique=True)
    player_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("players.id"))
    status: Mapped[RewardStatus] = mapped_column(
        Enum(RewardStatus, name="reward_status", values_callable=lambda e: [x.value for x in e])
    )
    value: Mapped[int]


class AuditRecord(Timestamped, Base):
    __tablename__ = "audit_records"
    event_type: Mapped[str] = mapped_column(String(80))
    entity_type: Mapped[str] = mapped_column(String(40))
    entity_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    evidence: Mapped[dict[str, Any]] = mapped_column(JSON)


class AnalyticsEvent(Timestamped, Base):
    __tablename__ = "analytics_events"
    event_key: Mapped[str] = mapped_column(String(120), unique=True)
    event_type: Mapped[str] = mapped_column(String(80))
    player_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("players.id"))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
