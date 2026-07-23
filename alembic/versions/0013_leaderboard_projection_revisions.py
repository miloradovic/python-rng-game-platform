"""Add authoritative leaderboard projection revisions.

Revision ID: 0013_leaderboard_projection
Revises: 0012_fairness_idempotency
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision = "0013_leaderboard_projection"
down_revision = "0012_fairness_idempotency"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Track every durable score generation independently of application code."""

    op.create_table(
        "leaderboard_projection_revisions",
        sa.Column("game_id", sa.Uuid(), nullable=False),
        sa.Column("period_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revision", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "revision >= 0", name="ck_leaderboard_projection_revision_nonnegative"
        ),
        sa.ForeignKeyConstraint(["game_id"], ["games.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("game_id", "period_start"),
    )
    op.execute(
        """
        INSERT INTO leaderboard_projection_revisions
            (game_id, period_start, revision, updated_at)
        SELECT game_id, period_start, count(*), CURRENT_TIMESTAMP
        FROM final_scores
        GROUP BY game_id, period_start
        """
    )
    op.execute(
        """
        CREATE FUNCTION advance_leaderboard_projection_revision()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
          INSERT INTO leaderboard_projection_revisions
              (game_id, period_start, revision, updated_at)
          VALUES (NEW.game_id, NEW.period_start, 1, statement_timestamp())
          ON CONFLICT (game_id, period_start) DO UPDATE
          SET revision = leaderboard_projection_revisions.revision + 1,
              updated_at = statement_timestamp();
          RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER final_scores_advance_projection_revision
        AFTER INSERT ON final_scores
        FOR EACH ROW EXECUTE FUNCTION advance_leaderboard_projection_revision()
        """
    )


def downgrade() -> None:
    """Remove rebuildable metadata without touching durable scores."""

    op.execute("DROP TRIGGER final_scores_advance_projection_revision ON final_scores")
    op.execute("DROP FUNCTION advance_leaderboard_projection_revision()")
    op.drop_table("leaderboard_projection_revisions")
