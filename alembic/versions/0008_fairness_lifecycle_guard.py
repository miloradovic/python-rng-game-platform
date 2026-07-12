"""Enforce forward-only fairness proof lifecycle transitions.

Revision ID: 0008_fairness_lifecycle_guard
Revises: 0007_fairness_proofs
"""

from collections.abc import Sequence

from alembic import op

revision = "0008_fairness_lifecycle_guard"
down_revision = "0007_fairness_proofs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """CREATE OR REPLACE FUNCTION protect_finalized_fairness_proof()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF TG_OP = 'DELETE' THEN
            RAISE EXCEPTION 'fairness proofs cannot be deleted';
          END IF;
          IF OLD.status = 'committed' AND NEW.status IN (
            'evaluated', 'revealed', 'expired', 'cancelled', 'failed'
          ) THEN
            RETURN NEW;
          END IF;
          IF OLD.status = 'evaluated' AND NEW.status IN ('revealed', 'failed') THEN
            RETURN NEW;
          END IF;
          RAISE EXCEPTION 'invalid or immutable fairness proof lifecycle transition';
        END $$"""
    )


def downgrade() -> None:
    op.execute(
        """CREATE OR REPLACE FUNCTION protect_finalized_fairness_proof()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF TG_OP = 'DELETE' OR OLD.status = 'revealed' THEN
            RAISE EXCEPTION 'fairness proofs cannot be deleted and revealed evidence is immutable';
          END IF;
          RETURN NEW;
        END $$"""
    )