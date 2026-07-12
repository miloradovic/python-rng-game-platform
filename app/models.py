"""SQLAlchemy models for the server-authoritative domain."""

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    ForeignKeyConstraint,
    LargeBinary,
    String,
    UniqueConstraint,
    func,
)
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


class FairnessProofStatus(StrEnum):
    """Durable lifecycle states for a protocol-versioned fairness proof."""

    COMMITTED = "committed"
    EVALUATED = "evaluated"
    REVEALED = "revealed"
    EXPIRED = "expired"
    CANCELLED = "cancelled"
    FAILED = "failed"


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
    request_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    status: Mapped[SessionStatus] = mapped_column(
        Enum(SessionStatus, name="session_status", values_callable=lambda e: [x.value for x in e])
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    challenge: Mapped[dict[str, Any]] = mapped_column(JSON)


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

    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


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


class FairnessProof(Timestamped, Base):
    """Publicly verifiable fairness evidence, excluding an unrevealed seed."""

    __tablename__ = "fairness_proofs"
    __table_args__ = (
        UniqueConstraint("session_id", name="uq_fairness_proof_session"),
        UniqueConstraint("outcome_id", name="uq_fairness_proof_outcome"),
        ForeignKeyConstraint(
            ["session_id", "player_id", "game_id", "config_version_id"],
            [
                "game_sessions.id",
                "game_sessions.player_id",
                "game_sessions.game_id",
                "game_sessions.config_version_id",
            ],
            name="fk_fairness_proof_session_binding",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["game_id", "game_key"],
            ["games.id", "games.key"],
            name="fk_fairness_proof_game_key",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["outcome_id", "session_id", "config_version_id"],
            ["outcomes.id", "outcomes.session_id", "outcomes.config_version_id"],
            name="fk_fairness_proof_outcome_binding",
            ondelete="RESTRICT",
        ),
        CheckConstraint("nonce >= 0", name="ck_fairness_proof_nonce_nonnegative"),
        CheckConstraint(
            "normalized_value IS NULL OR normalized_value >= 0",
            name="ck_fairness_proof_normalized_nonnegative",
        ),
        CheckConstraint(
            "derivation_attempt IS NULL OR derivation_attempt >= 0",
            name="ck_fairness_proof_attempt_nonnegative",
        ),
        CheckConstraint(
            "reward_value IS NULL OR reward_value >= 0",
            name="ck_fairness_proof_reward_nonnegative",
        ),
        CheckConstraint(
            "server_seed_commitment ~ '^[0-9a-f]{64}$'",
            name="ck_fairness_proof_commitment_hex",
        ),
        CheckConstraint(
            "mapping_digest ~ '^[0-9a-f]{64}$'",
            name="ck_fairness_proof_mapping_digest_hex",
        ),
        CheckConstraint(
            "raw_random_value IS NULL OR raw_random_value ~ '^[0-9a-f]{64}$'",
            name="ck_fairness_proof_raw_value_hex",
        ),
        CheckConstraint(
            "server_seed_revealed IS NULL OR server_seed_revealed ~ '^[0-9a-f]{64}$'",
            name="ck_fairness_proof_revealed_seed_hex",
        ),
        CheckConstraint(
            "(status = 'committed' AND outcome_id IS NULL AND client_seed IS NULL "
            "AND raw_random_value IS NULL AND normalized_value IS NULL "
            "AND derivation_attempt IS NULL AND reward_key IS NULL AND reward_value IS NULL "
            "AND evaluated_at IS NULL AND server_seed_revealed IS NULL AND revealed_at IS NULL) "
            "OR (status = 'evaluated' AND outcome_id IS NOT NULL AND client_seed IS NOT NULL "
            "AND raw_random_value IS NOT NULL AND normalized_value IS NOT NULL "
            "AND derivation_attempt IS NOT NULL AND reward_key IS NOT NULL "
            "AND reward_value IS NOT NULL AND evaluated_at IS NOT NULL "
            "AND server_seed_revealed IS NULL AND revealed_at IS NULL) "
            "OR (status = 'revealed' AND outcome_id IS NOT NULL AND client_seed IS NOT NULL "
            "AND raw_random_value IS NOT NULL AND normalized_value IS NOT NULL "
            "AND derivation_attempt IS NOT NULL AND reward_key IS NOT NULL "
            "AND reward_value IS NOT NULL AND evaluated_at IS NOT NULL "
            "AND server_seed_revealed IS NOT NULL AND revealed_at IS NOT NULL) "
            "OR (status IN ('expired', 'cancelled', 'failed') AND outcome_id IS NULL "
            "AND raw_random_value IS NULL AND normalized_value IS NULL "
            "AND derivation_attempt IS NULL AND reward_key IS NULL AND reward_value IS NULL "
            "AND evaluated_at IS NULL AND server_seed_revealed IS NULL AND revealed_at IS NULL)",
            name="ck_fairness_proof_lifecycle_evidence",
        ),
    )

    session_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    player_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    game_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    game_key: Mapped[str] = mapped_column(String(40))
    config_version_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    outcome_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    status: Mapped[FairnessProofStatus] = mapped_column(
        Enum(
            FairnessProofStatus,
            name="fairness_proof_status",
            values_callable=lambda e: [x.value for x in e],
        )
    )
    protocol_version: Mapped[str] = mapped_column(String(80))
    algorithm: Mapped[str] = mapped_column(String(80))
    server_seed_commitment: Mapped[str] = mapped_column(String(64))
    nonce: Mapped[int] = mapped_column(BigInteger)
    client_seed: Mapped[str | None] = mapped_column(String(64))
    mapping_version: Mapped[str] = mapped_column(String(80))
    mapping_digest: Mapped[str] = mapped_column(String(64))
    raw_random_value: Mapped[str | None] = mapped_column(String(64))
    normalized_value: Mapped[int | None] = mapped_column(BigInteger)
    derivation_attempt: Mapped[int | None] = mapped_column(BigInteger)
    reward_key: Mapped[str | None] = mapped_column(String(30))
    reward_value: Mapped[int | None]
    evaluated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    server_seed_revealed: Mapped[str | None] = mapped_column(String(64))
    revealed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class FairnessSeedCustody(Timestamped, Base):
    """Restricted unrevealed seed material; never serialize through proof APIs."""

    __tablename__ = "fairness_seed_custody"
    __table_args__ = (
        CheckConstraint(
            "octet_length(server_seed_material) = 32",
            name="ck_fairness_seed_custody_material_length",
        ),
    )

    proof_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("fairness_proofs.id", ondelete="RESTRICT"), unique=True
    )
    server_seed_material: Mapped[bytes] = mapped_column(LargeBinary(32))


class FairnessProofEvent(Timestamped, Base):
    """Append-only, hash-chained evidence for a fairness-proof lifecycle change."""

    __tablename__ = "fairness_proof_events"
    __table_args__ = (
        UniqueConstraint("proof_id", "sequence", name="uq_fairness_proof_event_sequence"),
        CheckConstraint("sequence >= 0", name="ck_fairness_proof_event_sequence_nonnegative"),
        CheckConstraint(
            "(sequence = 0 AND previous_evidence_hash IS NULL) "
            "OR (sequence > 0 AND previous_evidence_hash ~ '^[0-9a-f]{64}$')",
            name="ck_fairness_proof_event_previous_hash",
        ),
        CheckConstraint(
            "evidence_hash ~ '^[0-9a-f]{64}$'",
            name="ck_fairness_proof_event_hash_hex",
        ),
    )

    proof_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("fairness_proofs.id", ondelete="RESTRICT")
    )
    sequence: Mapped[int]
    event_type: Mapped[str] = mapped_column(String(40))
    status: Mapped[FairnessProofStatus] = mapped_column(
        Enum(
            FairnessProofStatus,
            name="fairness_proof_status",
            values_callable=lambda e: [x.value for x in e],
        )
    )
    previous_evidence_hash: Mapped[str | None] = mapped_column(String(64))
    evidence_hash: Mapped[str] = mapped_column(String(64))
