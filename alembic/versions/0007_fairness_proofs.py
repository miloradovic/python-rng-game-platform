"""Persist protocol-versioned fairness proof evidence.

Revision ID: 0007_fairness_proofs
Revises: 0006_analytics
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0007_fairness_proofs"
down_revision = "0006_analytics"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

fairness_proof_status = postgresql.ENUM(
    "committed",
    "evaluated",
    "revealed",
    "expired",
    "cancelled",
    "failed",
    name="fairness_proof_status",
    create_type=False,
)


def id_columns() -> list[sa.Column[object]]:
    return [
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    ]


def upgrade() -> None:
    op.create_unique_constraint("uq_games_id_key", "games", ["id", "key"])
    op.create_unique_constraint(
        "uq_game_sessions_proof_binding",
        "game_sessions",
        ["id", "player_id", "game_id", "config_version_id"],
    )
    op.create_unique_constraint(
        "uq_outcomes_proof_binding",
        "outcomes",
        ["id", "session_id", "config_version_id"],
    )
    fairness_proof_status.create(op.get_bind(), checkfirst=False)
    op.create_table(
        "fairness_proofs",
        *id_columns(),
        sa.Column("session_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("player_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("game_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("game_key", sa.String(40), nullable=False),
        sa.Column("config_version_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("outcome_id", postgresql.UUID(as_uuid=True)),
        sa.Column("status", fairness_proof_status, nullable=False),
        sa.Column("protocol_version", sa.String(80), nullable=False),
        sa.Column("algorithm", sa.String(80), nullable=False),
        sa.Column("server_seed_commitment", sa.String(64), nullable=False),
        sa.Column("nonce", sa.BigInteger(), nullable=False),
        sa.Column("client_seed", sa.String(64)),
        sa.Column("mapping_version", sa.String(80), nullable=False),
        sa.Column("mapping_digest", sa.String(64), nullable=False),
        sa.Column("raw_random_value", sa.String(64)),
        sa.Column("normalized_value", sa.BigInteger()),
        sa.Column("derivation_attempt", sa.BigInteger()),
        sa.Column("reward_key", sa.String(30)),
        sa.Column("reward_value", sa.Integer()),
        sa.Column("evaluated_at", sa.DateTime(timezone=True)),
        sa.Column("server_seed_revealed", sa.String(64)),
        sa.Column("revealed_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("session_id", name="uq_fairness_proof_session"),
        sa.UniqueConstraint("outcome_id", name="uq_fairness_proof_outcome"),
        sa.ForeignKeyConstraint(
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
        sa.ForeignKeyConstraint(
            ["game_id", "game_key"],
            ["games.id", "games.key"],
            name="fk_fairness_proof_game_key",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["outcome_id", "session_id", "config_version_id"],
            ["outcomes.id", "outcomes.session_id", "outcomes.config_version_id"],
            name="fk_fairness_proof_outcome_binding",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint("nonce >= 0", name="ck_fairness_proof_nonce_nonnegative"),
        sa.CheckConstraint(
            "server_seed_commitment ~ '^[0-9a-f]{64}$'",
            name="ck_fairness_proof_commitment_hex",
        ),
        sa.CheckConstraint(
            "mapping_digest ~ '^[0-9a-f]{64}$'",
            name="ck_fairness_proof_mapping_digest_hex",
        ),
        sa.CheckConstraint(
            "raw_random_value IS NULL OR raw_random_value ~ '^[0-9a-f]{64}$'",
            name="ck_fairness_proof_raw_value_hex",
        ),
        sa.CheckConstraint(
            "server_seed_revealed IS NULL OR server_seed_revealed ~ '^[0-9a-f]{64}$'",
            name="ck_fairness_proof_revealed_seed_hex",
        ),
        sa.CheckConstraint(
            "normalized_value IS NULL OR normalized_value >= 0",
            name="ck_fairness_proof_normalized_nonnegative",
        ),
        sa.CheckConstraint(
            "derivation_attempt IS NULL OR derivation_attempt >= 0",
            name="ck_fairness_proof_attempt_nonnegative",
        ),
        sa.CheckConstraint(
            "reward_value IS NULL OR reward_value >= 0",
            name="ck_fairness_proof_reward_nonnegative",
        ),
        sa.CheckConstraint(
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
    op.create_index(
        "ix_fairness_proofs_player_created",
        "fairness_proofs",
        ["player_id", "created_at"],
    )
    op.create_table(
        "fairness_seed_custody",
        *id_columns(),
        sa.Column(
            "proof_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("fairness_proofs.id", ondelete="RESTRICT"),
            nullable=False,
            unique=True,
        ),
sa.Column("server_seed_material", sa.LargeBinary(32), nullable=False),
        sa.CheckConstraint(
            "octet_length(server_seed_material) = 32",
            name="ck_fairness_seed_custody_material_length",
        ),
    )
    op.create_table(
        "fairness_proof_events",
        *id_columns(),
        sa.Column(
            "proof_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("fairness_proofs.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(40), nullable=False),
        sa.Column("status", fairness_proof_status, nullable=False),
        sa.Column("previous_evidence_hash", sa.String(64)),
        sa.Column("evidence_hash", sa.String(64), nullable=False),
        sa.UniqueConstraint("proof_id", "sequence", name="uq_fairness_proof_event_sequence"),
        sa.CheckConstraint("sequence >= 0", name="ck_fairness_proof_event_sequence_nonnegative"),
        sa.CheckConstraint(
            "(sequence = 0 AND previous_evidence_hash IS NULL) "
            "OR (sequence > 0 AND previous_evidence_hash ~ '^[0-9a-f]{64}$')",
            name="ck_fairness_proof_event_previous_hash",
        ),
        sa.CheckConstraint(
            "evidence_hash ~ '^[0-9a-f]{64}$'",
            name="ck_fairness_proof_event_hash_hex",
        ),
    )
    op.execute(
        """CREATE FUNCTION protect_fairness_proof_event() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          RAISE EXCEPTION 'fairness proof events are append-only';
        END $$"""
    )
    op.execute(
        """CREATE TRIGGER fairness_proof_events_append_only
        BEFORE UPDATE OR DELETE ON fairness_proof_events
        FOR EACH ROW EXECUTE FUNCTION protect_fairness_proof_event()"""
    )
    op.execute(
        """CREATE FUNCTION protect_finalized_fairness_proof() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF TG_OP = 'DELETE' OR OLD.status = 'revealed' THEN
            RAISE EXCEPTION 'fairness proofs cannot be deleted and revealed evidence is immutable';
          END IF;
          RETURN NEW;
        END $$"""
    )
    op.execute(
        """CREATE TRIGGER fairness_proofs_finalized_immutable
        BEFORE UPDATE OR DELETE ON fairness_proofs
        FOR EACH ROW EXECUTE FUNCTION protect_finalized_fairness_proof()"""
    )


def downgrade() -> None:
    op.execute(
        """DO $$ BEGIN
          IF EXISTS (SELECT 1 FROM fairness_proofs) THEN
            RAISE EXCEPTION 'cannot downgrade 0007 with persisted fairness proof evidence';
          END IF;
        END $$"""
    )
    op.execute("DROP TRIGGER fairness_proofs_finalized_immutable ON fairness_proofs")
    op.execute("DROP FUNCTION protect_finalized_fairness_proof()")
    op.execute("DROP TRIGGER fairness_proof_events_append_only ON fairness_proof_events")
    op.execute("DROP FUNCTION protect_fairness_proof_event()")
    op.drop_table("fairness_proof_events")
    op.drop_table("fairness_seed_custody")
    op.drop_index("ix_fairness_proofs_player_created", table_name="fairness_proofs")
    op.drop_table("fairness_proofs")
    fairness_proof_status.drop(op.get_bind(), checkfirst=False)
    op.drop_constraint("uq_outcomes_proof_binding", "outcomes", type_="unique")
    op.drop_constraint("uq_game_sessions_proof_binding", "game_sessions", type_="unique")
    op.drop_constraint("uq_games_id_key", "games", type_="unique")