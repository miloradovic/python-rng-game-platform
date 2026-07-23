"""Add durable final-score records for PostgreSQL-authoritative leaderboards.

Revision ID: 0009_final_scores
Revises: 0008_fairness_lifecycle_guard
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0009_final_scores"
down_revision = "0008_fairness_lifecycle_guard"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "final_scores",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("player_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("game_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("session_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("outcome_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("config_version_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("period_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("final_score", sa.BigInteger(), nullable=False),
        sa.UniqueConstraint("session_id", name="uq_final_scores_session"),
        sa.ForeignKeyConstraint(
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
        sa.ForeignKeyConstraint(
            ["outcome_id", "session_id", "config_version_id"],
            ["outcomes.id", "outcomes.session_id", "outcomes.config_version_id"],
            name="fk_final_scores_outcome_binding",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint("final_score >= 0", name="ck_final_scores_score_nonnegative"),
        sa.CheckConstraint("final_score <= 1000000", name="ck_final_scores_score_maximum"),
    )
    op.create_index(
        "ix_final_scores_ranking",
        "final_scores",
        ["game_id", "period_start", sa.text("final_score DESC"), "completed_at", "session_id"],
    )
    op.create_index(
        "ix_final_scores_player_period", "final_scores", ["player_id", "game_id", "period_start"]
    )


def downgrade() -> None:
    op.execute(
        """DO $$ BEGIN
          IF EXISTS (SELECT 1 FROM final_scores) THEN
            RAISE EXCEPTION 'cannot downgrade 0009 with persisted score evidence';
          END IF;
        END $$"""
    )
    op.drop_index("ix_final_scores_player_period", table_name="final_scores")
    op.drop_index("ix_final_scores_ranking", table_name="final_scores")
    op.drop_table("final_scores")
