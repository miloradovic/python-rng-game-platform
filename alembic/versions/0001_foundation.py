"""Establish the migration history without creating product tables.

Revision ID: 0001_foundation
Revises:
Create Date: 2026-07-12
"""

from collections.abc import Sequence

revision: str = "0001_foundation"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Establish the reviewed foundation revision."""


def downgrade() -> None:
    """Remove the no-op foundation revision."""

