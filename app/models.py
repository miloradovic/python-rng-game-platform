"""SQLAlchemy models for the server-authoritative domain."""

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    LargeBinary,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
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


class SettlementStatus(StrEnum):
    PROCESSING = "processing"
    COMPLETED = "completed"


class FairnessProofStatus(StrEnum):
    """Durable lifecycle states for a protocol-versioned fairness proof."""

    COMMITTED = "committed"
    REVEALED = "revealed"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


class Timestamped:
    """Server-owned UUID and timezone-aware creation timestamp."""

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Player(Timestamped, Base):
    __tablename__ = "players"
    __table_args__ = (CheckConstraint("length(trim(display_name)) >= 2", name="ck_player_name"),)
    display_name: Mapped[str] = mapped_column(String(50))
    status: Mapped[PlayerStatus] = mapped_column(
        Enum(PlayerStatus, name="player_status", values_callable=lambda e: [x.value for x in e]),
        default=PlayerStatus.ACTIVE,
        server_default=PlayerStatus.ACTIVE.value,
    )


class Game(Timestamped, Base):
    __tablename__ = "games"
    __table_args__ = (UniqueConstraint("id", "key", name="uq_games_id_key"),)
    key: Mapped[str] = mapped_column(String(40), unique=True)
    name: Mapped[str] = mapped_column(String(80))
    description: Mapped[str] = mapped_column(String(300))
    is_active: Mapped[bool] = mapped_column(default=True, server_default="true")


class GameConfigVersion(Timestamped, Base):
    __tablename__ = "game_config_versions"
    __table_args__ = (
        UniqueConstraint("game_id", "version", name="uq_game_config_version"),
        UniqueConstraint("game_id", "id", name="uq_game_config_game_binding"),
        CheckConstraint("version > 0", name="ck_config_version_positive"),
        CheckConstraint(
            "jsonb_typeof(payload) = 'object' "
            "AND payload ? 'game_type' AND payload ? 'cooldown_seconds'",
            name="ck_config_payload_shape",
        ),
        CheckConstraint(
            "(status = 'published' AND published_at IS NOT NULL) OR (status <> 'published')",
            name="ck_config_published_at",
        ),
        Index(
            "uq_game_one_published_config",
            "game_id",
            unique=True,
            postgresql_where=text("status = 'published'"),
        ),
    )
    game_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("games.id", ondelete="RESTRICT"))
    version: Mapped[int]
    status: Mapped[ConfigStatus] = mapped_column(
        Enum(ConfigStatus, name="config_status", values_callable=lambda e: [x.value for x in e])
    )
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class GameSession(Timestamped, Base):
    __tablename__ = "game_sessions"
    __table_args__ = (
        UniqueConstraint("request_id", name="uq_session_request"),
        UniqueConstraint(
            "id",
            "player_id",
            "game_id",
            "config_version_id",
            name="uq_game_sessions_proof_binding",
        ),
        ForeignKeyConstraint(
            ["game_id", "config_version_id"],
            ["game_config_versions.game_id", "game_config_versions.id"],
            name="fk_game_sessions_config_binding",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "(status = 'active' AND ended_at IS NULL) "
            "OR (status <> 'active' AND ended_at IS NOT NULL)",
            name="ck_session_terminal_ended_at",
        ),
        CheckConstraint(
            "request_fingerprint ~ '^[0-9a-f]{64}$'",
            name="ck_session_request_fingerprint_hex",
        ),
        Index("ix_sessions_player_created", "player_id", "created_at"),
        Index(
            "uq_active_session_player_game",
            "player_id",
            "game_id",
            unique=True,
            postgresql_where=text("status = 'active'"),
        ),
    )
    player_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("players.id", ondelete="RESTRICT"))
    game_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    config_version_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    request_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    request_fingerprint: Mapped[str] = mapped_column(String(64))
    status: Mapped[SessionStatus] = mapped_column(
        Enum(SessionStatus, name="session_status", values_callable=lambda e: [x.value for x in e])
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    challenge: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))


class Outcome(Timestamped, Base):
    __tablename__ = "outcomes"
    __table_args__ = (
        UniqueConstraint("session_id", name="uq_outcomes_session"),
        UniqueConstraint("id", "session_id", "config_version_id", name="uq_outcomes_proof_binding"),
        UniqueConstraint("id", "player_id", name="uq_outcomes_player_binding"),
        ForeignKeyConstraint(
            ["session_id", "player_id", "game_id", "config_version_id"],
            [
                "game_sessions.id",
                "game_sessions.player_id",
                "game_sessions.game_id",
                "game_sessions.config_version_id",
            ],
            name="fk_outcomes_session_binding",
            ondelete="RESTRICT",
        ),
    )
    session_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    player_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    game_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    config_version_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    status: Mapped[OutcomeStatus] = mapped_column(
        Enum(OutcomeStatus, name="outcome_status", values_callable=lambda e: [x.value for x in e])
    )
    result: Mapped[dict[str, Any]] = mapped_column(JSONB)


class Reward(Timestamped, Base):
    __tablename__ = "rewards"
    __table_args__ = (
        UniqueConstraint("outcome_id", name="rewards_outcome_id_key"),
        UniqueConstraint("settlement_recipient_id", name="uq_rewards_settlement_recipient"),
        ForeignKeyConstraint(
            ["outcome_id", "player_id"],
            ["outcomes.id", "outcomes.player_id"],
            name="fk_rewards_outcome_owner",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["settlement_recipient_id", "player_id"],
            ["settlement_recipients.id", "settlement_recipients.player_id"],
            name="fk_rewards_settlement_owner",
            ondelete="RESTRICT",
        ),
        CheckConstraint("value >= 0", name="ck_reward_value"),
        CheckConstraint(
            "(outcome_id IS NOT NULL) <> (settlement_recipient_id IS NOT NULL)",
            name="ck_rewards_exactly_one_source",
        ),
        Index("ix_rewards_player_created", "player_id", "created_at"),
        Index(
            "ix_rewards_player_created_id",
            "player_id",
            text("created_at DESC"),
            text("id DESC"),
        ),
    )
    outcome_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    settlement_recipient_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    player_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    status: Mapped[RewardStatus] = mapped_column(
        Enum(RewardStatus, name="reward_status", values_callable=lambda e: [x.value for x in e])
    )
    value: Mapped[int]

    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class FinalScore(Timestamped, Base):
    """One immutable server-derived leaderboard score for a completed session."""

    __tablename__ = "final_scores"
    __table_args__ = (
        UniqueConstraint("session_id", name="uq_final_scores_session"),
        UniqueConstraint(
            "id",
            "player_id",
            "game_id",
            "period_start",
            name="uq_final_scores_settlement_binding",
        ),
        ForeignKeyConstraint(
            ["session_id", "player_id", "game_id", "config_version_id"],
            [
                "game_sessions.id",
                "game_sessions.player_id",
                "game_sessions.game_id",
                "game_sessions.config_version_id",
            ],
            name="fk_final_scores_session_binding",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["outcome_id", "session_id", "config_version_id"],
            ["outcomes.id", "outcomes.session_id", "outcomes.config_version_id"],
            name="fk_final_scores_outcome_binding",
            ondelete="RESTRICT",
        ),
        CheckConstraint("final_score >= 0", name="ck_final_scores_score_nonnegative"),
        CheckConstraint("final_score <= 1000000", name="ck_final_scores_score_maximum"),
        Index(
            "ix_final_scores_ranking",
            "game_id",
            "period_start",
            text("final_score DESC"),
            "completed_at",
            "session_id",
        ),
        Index("ix_final_scores_player_period", "player_id", "game_id", "period_start"),
    )
    player_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    game_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    session_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    outcome_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    config_version_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    final_score: Mapped[int] = mapped_column(BigInteger)


class LeaderboardProjectionRevision(Base):
    """PostgreSQL watermark for one disposable leaderboard projection."""

    __tablename__ = "leaderboard_projection_revisions"
    __table_args__ = (
        CheckConstraint("revision >= 0", name="ck_leaderboard_projection_revision_nonnegative"),
    )
    game_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("games.id", ondelete="RESTRICT"), primary_key=True
    )
    period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    revision: Mapped[int] = mapped_column(BigInteger, server_default=text("0"))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("CURRENT_TIMESTAMP")
    )


class RewardTierConfig(Timestamped, Base):
    """Immutable published leaderboard reward tiers."""

    __tablename__ = "reward_tier_configs"
    __table_args__ = (
        UniqueConstraint("game_id", "version", name="uq_reward_tier_config_version"),
        UniqueConstraint("id", "game_id", name="uq_reward_tier_config_game_binding"),
    )
    game_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("games.id", ondelete="RESTRICT"))
    version: Mapped[int]
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class SettlementRun(Timestamped, Base):
    """One idempotent settlement of a closed game period."""

    __tablename__ = "settlement_runs"
    __table_args__ = (
        UniqueConstraint("game_id", "period_start", name="uq_settlement_game_period"),
        UniqueConstraint("id", "game_id", "period_start", name="uq_settlement_run_period_binding"),
        ForeignKeyConstraint(
            ["tier_config_id", "game_id"],
            ["reward_tier_configs.id", "reward_tier_configs.game_id"],
            name="fk_settlement_runs_tier_game",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "period_end = period_start + interval '7 days'", name="ck_settlement_period"
        ),
        CheckConstraint("status IN ('processing', 'completed')", name="ck_settlement_status"),
    )
    game_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    period_end: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    tier_config_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    tier_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB)
    status: Mapped[SettlementStatus] = mapped_column(
        Enum(
            SettlementStatus,
            name="settlement_status",
            values_callable=lambda e: [x.value for x in e],
            native_enum=False,
            create_constraint=False,
            length=20,
        )
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SettlementRecipient(Timestamped, Base):
    """Durable rank, score, tier, and reward evidence for one recipient."""

    __tablename__ = "settlement_recipients"
    __table_args__ = (
        UniqueConstraint("run_id", "player_id", name="uq_settlement_recipient_player"),
        UniqueConstraint("run_id", "rank", name="uq_settlement_recipient_rank"),
        UniqueConstraint("id", "player_id", name="uq_settlement_recipient_owner_binding"),
        ForeignKeyConstraint(
            ["run_id", "game_id", "period_start"],
            ["settlement_runs.id", "settlement_runs.game_id", "settlement_runs.period_start"],
            name="fk_settlement_recipients_run_binding",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["score_id", "player_id", "game_id", "period_start"],
            [
                "final_scores.id",
                "final_scores.player_id",
                "final_scores.game_id",
                "final_scores.period_start",
            ],
            name="fk_settlement_recipients_score_binding",
            ondelete="RESTRICT",
        ),
        CheckConstraint("rank > 0", name="ck_settlement_recipient_rank"),
        CheckConstraint("reward_value >= 0", name="ck_settlement_recipient_reward"),
    )
    run_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    player_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    score_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    game_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    rank: Mapped[int]
    tier_key: Mapped[str] = mapped_column(String(40))
    reward_value: Mapped[int]


class AuditRecord(Timestamped, Base):
    __tablename__ = "audit_records"
    __table_args__ = (Index("ix_audit_entity", "entity_type", "entity_id", "created_at"),)
    event_type: Mapped[str] = mapped_column(String(80))
    entity_type: Mapped[str] = mapped_column(String(40))
    entity_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    evidence: Mapped[dict[str, Any]] = mapped_column(JSONB)


class AnalyticsEvent(Timestamped, Base):
    __tablename__ = "analytics_events"
    __table_args__ = (
        Index("ix_analytics_player_type_created", "player_id", "event_type", "created_at"),
    )
    event_key: Mapped[str] = mapped_column(String(120), unique=True)
    event_type: Mapped[str] = mapped_column(String(80))
    player_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("players.id"))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)


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
            "evaluation_fingerprint IS NULL OR evaluation_fingerprint ~ '^[0-9a-f]{64}$'",
            name="ck_fairness_proof_evaluation_fingerprint_hex",
        ),
        CheckConstraint(
            "(status = 'committed' AND outcome_id IS NULL AND client_seed IS NULL "
            "AND evaluation_fingerprint IS NULL "
            "AND raw_random_value IS NULL AND normalized_value IS NULL "
            "AND derivation_attempt IS NULL AND reward_key IS NULL AND reward_value IS NULL "
            "AND evaluated_at IS NULL AND server_seed_revealed IS NULL AND revealed_at IS NULL) "
            "OR (status = 'revealed' AND outcome_id IS NOT NULL AND client_seed IS NOT NULL "
            "AND evaluation_fingerprint IS NOT NULL "
            "AND raw_random_value IS NOT NULL AND normalized_value IS NOT NULL "
            "AND derivation_attempt IS NOT NULL AND reward_key IS NOT NULL "
            "AND reward_value IS NOT NULL AND evaluated_at IS NOT NULL "
            "AND server_seed_revealed IS NOT NULL AND revealed_at IS NOT NULL) "
            "OR (status IN ('expired', 'cancelled') AND outcome_id IS NULL "
            "AND client_seed IS NULL AND evaluation_fingerprint IS NULL "
            "AND raw_random_value IS NULL AND normalized_value IS NULL "
            "AND derivation_attempt IS NULL AND reward_key IS NULL AND reward_value IS NULL "
            "AND evaluated_at IS NULL AND server_seed_revealed IS NULL AND revealed_at IS NULL)",
            name="ck_fairness_proof_lifecycle_evidence",
        ),
        Index("ix_fairness_proofs_player_created", "player_id", "created_at"),
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
    evaluation_fingerprint: Mapped[str | None] = mapped_column(String(64))
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
    evidence_version: Mapped[int]
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    status: Mapped[FairnessProofStatus] = mapped_column(
        Enum(
            FairnessProofStatus,
            name="fairness_proof_status",
            values_callable=lambda e: [x.value for x in e],
        )
    )
    previous_evidence_hash: Mapped[str | None] = mapped_column(String(64))
    evidence_hash: Mapped[str] = mapped_column(String(64))
