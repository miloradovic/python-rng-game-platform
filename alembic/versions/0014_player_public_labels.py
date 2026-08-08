"""Add safe generated labels for player-facing leaderboard entries.

Revision ID: 0014_player_public_labels
Revises: 0013_leaderboard_projection
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision = "0014_player_public_labels"
down_revision = "0013_leaderboard_projection"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Backfill non-personal labels before making the contract mandatory."""

    op.add_column("players", sa.Column("public_label", sa.String(length=19), nullable=True))
    op.execute(
        """
        UPDATE players
        SET public_label = 'Player-' || upper(substring(md5(id::text || '-public-label-v1'), 1, 12))
        """
    )
    op.alter_column("players", "public_label", nullable=False)
    op.create_check_constraint(
        "ck_player_public_label_format",
        "players",
        "public_label ~ '^Player-[A-F0-9]{12}$'",
    )
    op.create_unique_constraint("uq_players_public_label", "players", ["public_label"])


def downgrade() -> None:
    """Remove public presentation labels without touching player identities."""

    op.drop_constraint("uq_players_public_label", "players", type_="unique")
    op.drop_constraint("ck_player_public_label_format", "players", type_="check")
    op.drop_column("players", "public_label")
