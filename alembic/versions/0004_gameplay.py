"""Add server-authoritative gameplay state.

Revision ID: 0004_gameplay
Revises: 0003_session_lifecycle
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0004_gameplay"
down_revision = "0003_session_lifecycle"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "game_sessions",
        sa.Column(
            "challenge",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.execute(
        """CREATE OR REPLACE FUNCTION protect_published_config() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
          IF OLD.status = 'published' AND NOT (
            NEW.status = 'retired'
            AND NEW.game_id = OLD.game_id
            AND NEW.version = OLD.version
            AND NEW.payload = OLD.payload
            AND NEW.published_at = OLD.published_at
          ) THEN
            RAISE EXCEPTION 'published game configuration contents are immutable';
          END IF;
          RETURN NEW;
        END $$"""
    )


def downgrade() -> None:
    op.execute(
        """CREATE OR REPLACE FUNCTION protect_published_config() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
          IF OLD.status = 'published' THEN
            RAISE EXCEPTION 'published game configurations are immutable';
          END IF;
          RETURN NEW;
        END $$"""
    )
    op.drop_column("game_sessions", "challenge")
