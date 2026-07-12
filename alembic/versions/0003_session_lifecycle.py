"""Add concurrency-safe session lifecycle fields.

Revision ID: 0003_session_lifecycle
Revises: 0002_domain_catalogue
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0003_session_lifecycle"
down_revision = "0002_domain_catalogue"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("game_sessions", sa.Column("request_id", postgresql.UUID(as_uuid=True)))
    op.add_column("game_sessions", sa.Column("ended_at", sa.DateTime(timezone=True)))
    op.execute("UPDATE game_sessions SET request_id = gen_random_uuid()")
    op.alter_column("game_sessions", "request_id", nullable=False)
    op.create_unique_constraint(
        "uq_session_player_request", "game_sessions", ["player_id", "request_id"]
    )
    op.create_index(
        "uq_active_session_player_game",
        "game_sessions",
        ["player_id", "game_id"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )
    op.create_check_constraint(
        "ck_session_terminal_ended_at",
        "game_sessions",
        "(status = 'active' AND ended_at IS NULL) OR "
        "(status <> 'active' AND ended_at IS NOT NULL)",
    )


def downgrade() -> None:
    op.drop_constraint("ck_session_terminal_ended_at", "game_sessions", type_="check")
    op.drop_index("uq_active_session_player_game", table_name="game_sessions")
    op.drop_constraint("uq_session_player_request", "game_sessions", type_="unique")
    op.drop_column("game_sessions", "ended_at")
    op.drop_column("game_sessions", "request_id")
