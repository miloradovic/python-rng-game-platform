"""Reconcile metadata and enforce cross-aggregate persistence integrity.

Revision ID: 0011_persistence_integrity
Revises: 0010_settlements
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0011_persistence_integrity"
down_revision = "0010_settlements"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add authoritative configuration, ownership, reward, and settlement bindings."""

    op.create_unique_constraint(
        "uq_game_config_game_binding", "game_config_versions", ["game_id", "id"]
    )
    op.drop_constraint(
        "game_sessions_config_version_id_fkey", "game_sessions", type_="foreignkey"
    )
    op.drop_constraint("game_sessions_game_id_fkey", "game_sessions", type_="foreignkey")
    op.create_foreign_key(
        "fk_game_sessions_config_binding",
        "game_sessions",
        "game_config_versions",
        ["game_id", "config_version_id"],
        ["game_id", "id"],
        ondelete="RESTRICT",
    )

    op.add_column("outcomes", sa.Column("player_id", postgresql.UUID(as_uuid=True)))
    op.add_column("outcomes", sa.Column("game_id", postgresql.UUID(as_uuid=True)))
    op.execute(
        """UPDATE outcomes AS o
           SET player_id = s.player_id, game_id = s.game_id
           FROM game_sessions AS s
           WHERE s.id = o.session_id"""
    )
    op.alter_column("outcomes", "player_id", nullable=False)
    op.alter_column("outcomes", "game_id", nullable=False)
    op.drop_constraint("outcomes_session_id_fkey", "outcomes", type_="foreignkey")
    op.drop_constraint("outcomes_config_version_id_fkey", "outcomes", type_="foreignkey")
    op.drop_constraint("outcomes_session_id_key", "outcomes", type_="unique")
    op.create_unique_constraint("uq_outcomes_session", "outcomes", ["session_id"])
    op.create_unique_constraint(
        "uq_outcomes_player_binding", "outcomes", ["id", "player_id"]
    )
    op.create_foreign_key(
        "fk_outcomes_session_binding",
        "outcomes",
        "game_sessions",
        ["session_id", "player_id", "game_id", "config_version_id"],
        ["id", "player_id", "game_id", "config_version_id"],
        ondelete="RESTRICT",
    )

    op.create_unique_constraint(
        "uq_final_scores_settlement_binding",
        "final_scores",
        ["id", "player_id", "game_id", "period_start"],
    )
    op.create_unique_constraint(
        "uq_reward_tier_config_game_binding", "reward_tier_configs", ["id", "game_id"]
    )
    op.create_unique_constraint(
        "uq_settlement_run_period_binding",
        "settlement_runs",
        ["id", "game_id", "period_start"],
    )
    op.drop_constraint("settlement_runs_game_id_fkey", "settlement_runs", type_="foreignkey")
    op.drop_constraint(
        "settlement_runs_tier_config_id_fkey", "settlement_runs", type_="foreignkey"
    )
    op.create_foreign_key(
        "fk_settlement_runs_tier_game",
        "settlement_runs",
        "reward_tier_configs",
        ["tier_config_id", "game_id"],
        ["id", "game_id"],
        ondelete="RESTRICT",
    )

    op.add_column(
        "settlement_recipients",
        sa.Column("game_id", postgresql.UUID(as_uuid=True)),
    )
    op.add_column(
        "settlement_recipients",
        sa.Column("period_start", sa.DateTime(timezone=True)),
    )
    op.execute(
        """UPDATE settlement_recipients AS recipient
           SET game_id = run.game_id, period_start = run.period_start
           FROM settlement_runs AS run
           WHERE run.id = recipient.run_id"""
    )
    op.alter_column("settlement_recipients", "game_id", nullable=False)
    op.alter_column("settlement_recipients", "period_start", nullable=False)
    op.drop_constraint(
        "settlement_recipients_run_id_fkey", "settlement_recipients", type_="foreignkey"
    )
    op.drop_constraint(
        "settlement_recipients_player_id_fkey",
        "settlement_recipients",
        type_="foreignkey",
    )
    op.drop_constraint(
        "settlement_recipients_score_id_fkey", "settlement_recipients", type_="foreignkey"
    )
    op.create_unique_constraint(
        "uq_settlement_recipient_owner_binding",
        "settlement_recipients",
        ["id", "player_id"],
    )
    op.create_foreign_key(
        "fk_settlement_recipients_run_binding",
        "settlement_recipients",
        "settlement_runs",
        ["run_id", "game_id", "period_start"],
        ["id", "game_id", "period_start"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_settlement_recipients_score_binding",
        "settlement_recipients",
        "final_scores",
        ["score_id", "player_id", "game_id", "period_start"],
        ["id", "player_id", "game_id", "period_start"],
        ondelete="RESTRICT",
    )

    op.drop_constraint("rewards_outcome_id_fkey", "rewards", type_="foreignkey")
    op.drop_constraint("rewards_player_id_fkey", "rewards", type_="foreignkey")
    op.drop_constraint("fk_rewards_settlement_recipient", "rewards", type_="foreignkey")
    op.create_foreign_key(
        "fk_rewards_outcome_owner",
        "rewards",
        "outcomes",
        ["outcome_id", "player_id"],
        ["id", "player_id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_rewards_settlement_owner",
        "rewards",
        "settlement_recipients",
        ["settlement_recipient_id", "player_id"],
        ["id", "player_id"],
        ondelete="RESTRICT",
    )

    op.execute(
        """CREATE FUNCTION protect_append_only_evidence() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          RAISE EXCEPTION 'audit and analytics evidence is append-only';
        END $$"""
    )
    op.execute(
        """CREATE TRIGGER audit_records_append_only
        BEFORE UPDATE OR DELETE ON audit_records
        FOR EACH ROW EXECUTE FUNCTION protect_append_only_evidence()"""
    )
    op.execute(
        """CREATE TRIGGER analytics_events_append_only
        BEFORE UPDATE OR DELETE ON analytics_events
        FOR EACH ROW EXECUTE FUNCTION protect_append_only_evidence()"""
    )


def downgrade() -> None:
    """Restore the prior schema only when no durable evidence can be weakened."""

    op.execute(
        """DO $$ BEGIN
          IF EXISTS (SELECT 1 FROM audit_records)
             OR EXISTS (SELECT 1 FROM analytics_events)
             OR EXISTS (SELECT 1 FROM outcomes)
             OR EXISTS (SELECT 1 FROM final_scores)
             OR EXISTS (SELECT 1 FROM settlement_runs) THEN
            RAISE EXCEPTION 'cannot downgrade 0011 with persisted domain evidence';
          END IF;
        END $$"""
    )
    op.execute("DROP TRIGGER analytics_events_append_only ON analytics_events")
    op.execute("DROP TRIGGER audit_records_append_only ON audit_records")
    op.execute("DROP FUNCTION protect_append_only_evidence()")

    op.drop_constraint("fk_rewards_settlement_owner", "rewards", type_="foreignkey")
    op.drop_constraint("fk_rewards_outcome_owner", "rewards", type_="foreignkey")
    op.create_foreign_key(
        "fk_rewards_settlement_recipient",
        "rewards",
        "settlement_recipients",
        ["settlement_recipient_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(None, "rewards", "players", ["player_id"], ["id"])
    op.create_foreign_key(None, "rewards", "outcomes", ["outcome_id"], ["id"])

    op.drop_constraint(
        "fk_settlement_recipients_score_binding",
        "settlement_recipients",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_settlement_recipients_run_binding", "settlement_recipients", type_="foreignkey"
    )
    op.drop_constraint(
        "uq_settlement_recipient_owner_binding", "settlement_recipients", type_="unique"
    )
    op.create_foreign_key(
        None, "settlement_recipients", "final_scores", ["score_id"], ["id"], ondelete="RESTRICT"
    )
    op.create_foreign_key(
        None, "settlement_recipients", "players", ["player_id"], ["id"], ondelete="RESTRICT"
    )
    op.create_foreign_key(
        None,
        "settlement_recipients",
        "settlement_runs",
        ["run_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.drop_column("settlement_recipients", "period_start")
    op.drop_column("settlement_recipients", "game_id")

    op.drop_constraint("fk_settlement_runs_tier_game", "settlement_runs", type_="foreignkey")
    op.create_foreign_key(
        None, "settlement_runs", "games", ["game_id"], ["id"], ondelete="RESTRICT"
    )
    op.create_foreign_key(
        None,
        "settlement_runs",
        "reward_tier_configs",
        ["tier_config_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.drop_constraint(
        "uq_settlement_run_period_binding", "settlement_runs", type_="unique"
    )
    op.drop_constraint(
        "uq_reward_tier_config_game_binding", "reward_tier_configs", type_="unique"
    )
    op.drop_constraint(
        "uq_final_scores_settlement_binding", "final_scores", type_="unique"
    )

    op.drop_constraint("fk_outcomes_session_binding", "outcomes", type_="foreignkey")
    op.drop_constraint("uq_outcomes_player_binding", "outcomes", type_="unique")
    op.drop_constraint("uq_outcomes_session", "outcomes", type_="unique")
    op.create_unique_constraint(None, "outcomes", ["session_id"])
    op.create_foreign_key(None, "outcomes", "game_config_versions", ["config_version_id"], ["id"])
    op.create_foreign_key(None, "outcomes", "game_sessions", ["session_id"], ["id"])
    op.drop_column("outcomes", "game_id")
    op.drop_column("outcomes", "player_id")

    op.drop_constraint("fk_game_sessions_config_binding", "game_sessions", type_="foreignkey")
    op.create_foreign_key(
        None, "game_sessions", "games", ["game_id"], ["id"], ondelete="RESTRICT"
    )
    op.create_foreign_key(
        None, "game_sessions", "game_config_versions", ["config_version_id"], ["id"]
    )
    op.drop_constraint(
        "uq_game_config_game_binding", "game_config_versions", type_="unique"
    )
