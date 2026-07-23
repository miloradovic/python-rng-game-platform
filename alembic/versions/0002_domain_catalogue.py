"""Create domain catalogue.

Revision ID: 0002_domain_catalogue
Revises: 0001_foundation
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0002_domain_catalogue"
down_revision = "0001_foundation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def id_columns() -> list[sa.Column[object]]:
    return [
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    ]


def upgrade() -> None:
    player_status = sa.Enum("active", "inactive", name="player_status")
    config_status = sa.Enum("draft", "published", "retired", name="config_status")
    session_status = sa.Enum("active", "completed", "expired", "cancelled", name="session_status")
    outcome_status = sa.Enum("accepted", "rejected", name="outcome_status")
    reward_status = sa.Enum("issued", "claimed", "expired", name="reward_status")
    op.create_table("players", *id_columns(), sa.Column("display_name", sa.String(50), nullable=False), sa.Column("status", player_status, server_default="active", nullable=False), sa.CheckConstraint("length(trim(display_name)) >= 2", name="ck_player_name"))
    op.create_table("games", *id_columns(), sa.Column("key", sa.String(40), nullable=False, unique=True), sa.Column("name", sa.String(80), nullable=False), sa.Column("description", sa.String(300), nullable=False), sa.Column("is_active", sa.Boolean(), server_default=sa.true(), nullable=False))
    op.create_table("game_config_versions", *id_columns(), sa.Column("game_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("games.id", ondelete="RESTRICT"), nullable=False), sa.Column("version", sa.Integer(), nullable=False), sa.Column("status", config_status, nullable=False), sa.Column("payload", postgresql.JSONB(), nullable=False), sa.Column("published_at", sa.DateTime(timezone=True)), sa.UniqueConstraint("game_id", "version", name="uq_game_config_version"), sa.CheckConstraint("version > 0", name="ck_config_version_positive"), sa.CheckConstraint("jsonb_typeof(payload) = 'object' AND payload ? 'game_type' AND payload ? 'cooldown_seconds'", name="ck_config_payload_shape"), sa.CheckConstraint("(status = 'published' AND published_at IS NOT NULL) OR (status <> 'published')", name="ck_config_published_at"))
    op.create_index("uq_game_one_published_config", "game_config_versions", ["game_id"], unique=True, postgresql_where=sa.text("status = 'published'"))
    op.create_table("game_sessions", *id_columns(), sa.Column("player_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("players.id", ondelete="RESTRICT"), nullable=False), sa.Column("game_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("games.id", ondelete="RESTRICT"), nullable=False), sa.Column("config_version_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("game_config_versions.id"), nullable=False), sa.Column("status", session_status, nullable=False), sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False))
    op.create_index("ix_sessions_player_created", "game_sessions", ["player_id", "created_at"])
    op.create_table("outcomes", *id_columns(), sa.Column("session_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("game_sessions.id"), nullable=False, unique=True), sa.Column("config_version_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("game_config_versions.id"), nullable=False), sa.Column("status", outcome_status, nullable=False), sa.Column("result", postgresql.JSONB(), nullable=False))
    op.create_table("rewards", *id_columns(), sa.Column("outcome_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("outcomes.id"), nullable=False, unique=True), sa.Column("player_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("players.id"), nullable=False), sa.Column("status", reward_status, nullable=False), sa.Column("value", sa.Integer(), nullable=False), sa.CheckConstraint("value >= 0", name="ck_reward_value"))
    op.create_index("ix_rewards_player_created", "rewards", ["player_id", "created_at"])
    op.create_table("audit_records", *id_columns(), sa.Column("event_type", sa.String(80), nullable=False), sa.Column("entity_type", sa.String(40), nullable=False), sa.Column("entity_id", postgresql.UUID(as_uuid=True), nullable=False), sa.Column("evidence", postgresql.JSONB(), nullable=False))
    op.create_index("ix_audit_entity", "audit_records", ["entity_type", "entity_id", "created_at"])
    op.create_table("analytics_events", *id_columns(), sa.Column("event_key", sa.String(120), nullable=False, unique=True), sa.Column("event_type", sa.String(80), nullable=False), sa.Column("player_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("players.id")), sa.Column("payload", postgresql.JSONB(), nullable=False))
    op.execute("""CREATE FUNCTION protect_published_config() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN IF OLD.status = 'published' THEN RAISE EXCEPTION 'published game configurations are immutable'; END IF; RETURN NEW; END $$""")
    op.execute("""CREATE TRIGGER game_config_immutable BEFORE UPDATE OR DELETE ON game_config_versions FOR EACH ROW EXECUTE FUNCTION protect_published_config()""")


def downgrade() -> None:
    op.execute(
        """DO $$ BEGIN
          IF EXISTS (SELECT 1 FROM players)
             OR EXISTS (SELECT 1 FROM games)
             OR EXISTS (SELECT 1 FROM game_config_versions)
             OR EXISTS (SELECT 1 FROM game_sessions)
             OR EXISTS (SELECT 1 FROM outcomes)
             OR EXISTS (SELECT 1 FROM rewards)
             OR EXISTS (SELECT 1 FROM audit_records)
             OR EXISTS (SELECT 1 FROM analytics_events) THEN
            RAISE EXCEPTION 'cannot downgrade 0002 with persisted domain data';
          END IF;
        END $$"""
    )
    op.drop_table("analytics_events")
    op.drop_table("audit_records")
    op.drop_table("rewards")
    op.drop_table("outcomes")
    op.drop_table("game_sessions")
    op.execute("DROP TRIGGER game_config_immutable ON game_config_versions")
    op.execute("DROP FUNCTION protect_published_config()")
    op.drop_table("game_config_versions")
    op.drop_table("games")
    op.drop_table("players")
    for name in ("reward_status", "outcome_status", "session_status", "config_status", "player_status"):
        op.execute(f"DROP TYPE {name}")
