"""Add reward claim timestamps and history index.

Revision ID: 0005_rewards
Revises: 0004_gameplay
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision = "0005_rewards"
down_revision = "0004_gameplay"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("rewards", sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index(
        "ix_rewards_player_created_id",
        "rewards",
        ["player_id", sa.text("created_at DESC"), sa.text("id DESC")],
    )


def downgrade() -> None:
    op.drop_index("ix_rewards_player_created_id", table_name="rewards")
    op.drop_column("rewards", "claimed_at")
