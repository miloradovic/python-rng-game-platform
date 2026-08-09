"""Permit multiple immutable published game configurations.

Revision ID: 0015_multiple_published_configs
Revises: 0014_player_public_labels
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision = "0015_multiple_published_configs"
down_revision = "0014_player_public_labels"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Allow published history to coexist and close the existing delete loophole."""

    op.drop_index("uq_game_one_published_config", table_name="game_config_versions")
    op.execute(
        """CREATE OR REPLACE FUNCTION protect_published_config() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
          IF TG_OP = 'DELETE' THEN
            IF OLD.status = 'published' THEN
              RAISE EXCEPTION 'published game configurations are immutable';
            END IF;
            RETURN OLD;
          END IF;
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
    """Restore the old constraint only when existing history is compatible."""

    op.execute(
        """
        DO $$
        BEGIN
          IF EXISTS (
            SELECT 1
            FROM game_config_versions
            WHERE status = 'published'
            GROUP BY game_id
            HAVING count(*) > 1
          ) THEN
            RAISE EXCEPTION
              'cannot downgrade 0015 while a game has multiple published configurations';
          END IF;
        END
        $$
        """
    )
    op.create_index(
        "uq_game_one_published_config",
        "game_config_versions",
        ["game_id"],
        unique=True,
        postgresql_where=sa.text("status = 'published'"),
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
