"""Add immutable reward tiers and idempotent leaderboard settlement.

Revision ID: 0010_settlements
Revises: 0009_final_scores
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0010_settlements"
down_revision = "0009_final_scores"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "reward_tier_configs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("game_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["game_id"], ["games.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("game_id", "version", name="uq_reward_tier_config_version"),
    )
    op.create_table(
        "settlement_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("game_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("period_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("period_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("tier_config_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tier_snapshot", postgresql.JSONB(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.ForeignKeyConstraint(["game_id"], ["games.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["tier_config_id"], ["reward_tier_configs.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("game_id", "period_start", name="uq_settlement_game_period"),
        sa.CheckConstraint("period_end = period_start + interval '7 days'", name="ck_settlement_period"),
        sa.CheckConstraint("status IN ('processing', 'completed')", name="ck_settlement_status"),
    )
    op.create_table(
        "settlement_recipients",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("player_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("score_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("rank", sa.Integer(), nullable=False),
        sa.Column("tier_key", sa.String(40), nullable=False),
        sa.Column("reward_value", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["settlement_runs.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["player_id"], ["players.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["score_id"], ["final_scores.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("run_id", "player_id", name="uq_settlement_recipient_player"),
        sa.UniqueConstraint("run_id", "rank", name="uq_settlement_recipient_rank"),
        sa.CheckConstraint("rank > 0", name="ck_settlement_recipient_rank"),
        sa.CheckConstraint("reward_value >= 0", name="ck_settlement_recipient_reward"),
    )
    op.alter_column("rewards", "outcome_id", nullable=True)
    op.add_column("rewards", sa.Column("settlement_recipient_id", postgresql.UUID(as_uuid=True)))
    op.create_foreign_key(
        "fk_rewards_settlement_recipient", "rewards", "settlement_recipients",
        ["settlement_recipient_id"], ["id"], ondelete="RESTRICT",
    )
    op.create_unique_constraint(
        "uq_rewards_settlement_recipient", "rewards", ["settlement_recipient_id"]
    )
    op.create_check_constraint(
        "ck_rewards_exactly_one_source", "rewards",
        "(outcome_id IS NOT NULL) <> (settlement_recipient_id IS NOT NULL)",
    )
    op.get_bind().exec_driver_sql(
        """INSERT INTO reward_tier_configs
           (id, game_id, version, payload, published_at)
           SELECT gen_random_uuid(), id, 1,
             '{"tiers":[
               {"key":"champion","min_rank":1,"max_rank":1,"reward_value":500},
               {"key":"podium","min_rank":2,"max_rank":3,"reward_value":200},
               {"key":"top_ten","min_rank":4,"max_rank":10,"reward_value":50}
             ]}'::jsonb,
             '1970-01-01 00:00:00+00'
           FROM games WHERE key = 'skill_check'
           ON CONFLICT (game_id, version) DO NOTHING"""
    )
    op.execute(
        """CREATE FUNCTION protect_reward_tier_config() RETURNS trigger LANGUAGE plpgsql AS $$
           BEGIN RAISE EXCEPTION 'published reward tier configurations are immutable'; END $$"""
    )
    op.execute(
        """CREATE TRIGGER reward_tier_config_immutable
           BEFORE UPDATE OR DELETE ON reward_tier_configs
           FOR EACH ROW EXECUTE FUNCTION protect_reward_tier_config()"""
    )


def downgrade() -> None:
    op.execute(
        """DO $$ BEGIN
          IF EXISTS (SELECT 1 FROM reward_tier_configs)
             OR EXISTS (SELECT 1 FROM settlement_runs)
             OR EXISTS (SELECT 1 FROM settlement_recipients)
             OR EXISTS (SELECT 1 FROM rewards WHERE settlement_recipient_id IS NOT NULL) THEN
            RAISE EXCEPTION 'cannot downgrade 0010 with persisted settlement evidence';
          END IF;
        END $$"""
    )
    op.execute("DROP TRIGGER reward_tier_config_immutable ON reward_tier_configs")
    op.execute("DROP FUNCTION protect_reward_tier_config()")
    op.drop_constraint("ck_rewards_exactly_one_source", "rewards", type_="check")
    op.drop_constraint("uq_rewards_settlement_recipient", "rewards", type_="unique")
    op.drop_constraint("fk_rewards_settlement_recipient", "rewards", type_="foreignkey")
    op.drop_column("rewards", "settlement_recipient_id")
    op.alter_column("rewards", "outcome_id", nullable=False)
    op.drop_table("settlement_recipients")
    op.drop_table("settlement_runs")
    op.drop_table("reward_tier_configs")
