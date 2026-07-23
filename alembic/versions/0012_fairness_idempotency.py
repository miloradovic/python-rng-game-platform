"""Unify daily-spin fairness and bind idempotent intent.

Revision ID: 0012_fairness_idempotency
Revises: 0011_persistence_integrity
"""

import hashlib
import json
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0012_fairness_idempotency"
down_revision = "0011_persistence_integrity"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _fingerprint(operation: str, material_input: dict[str, object]) -> str:
    canonical = json.dumps(
        {"operation": operation, "input": material_input},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return hashlib.sha256(canonical.encode("ascii")).hexdigest()


def upgrade() -> None:
    """Bind request intent, remove dead states, and version lifecycle evidence."""

    op.execute(
        """DO $$ BEGIN
          IF EXISTS (
            SELECT request_id FROM game_sessions GROUP BY request_id HAVING count(*) > 1
          ) THEN
            RAISE EXCEPTION 'cannot globally bind duplicate session request identifiers';
          END IF;
          IF EXISTS (
            SELECT 1 FROM fairness_proofs WHERE status IN ('evaluated', 'failed')
          ) THEN
            RAISE EXCEPTION 'cannot remove fairness states while evaluated/failed proofs exist';
          END IF;
        END $$"""
    )

    op.add_column("game_sessions", sa.Column("request_fingerprint", sa.String(64)))
    connection = op.get_bind()
    sessions = connection.execute(
        sa.text(
            """SELECT session.id, session.player_id, game.key AS game_key
               FROM game_sessions AS session
               JOIN games AS game ON game.id = session.game_id"""
        )
    ).mappings()
    for row in sessions:
        connection.execute(
            sa.text("UPDATE game_sessions SET request_fingerprint = :fingerprint WHERE id = :id"),
            {
                "id": row["id"],
                "fingerprint": _fingerprint(
                    "create_session",
                    {"player_id": str(row["player_id"]), "game_key": row["game_key"]},
                ),
            },
        )
    op.alter_column("game_sessions", "request_fingerprint", nullable=False)
    op.create_check_constraint(
        "ck_session_request_fingerprint_hex",
        "game_sessions",
        "request_fingerprint ~ '^[0-9a-f]{64}$'",
    )
    op.drop_constraint("uq_session_player_request", "game_sessions", type_="unique")
    op.create_unique_constraint("uq_session_request", "game_sessions", ["request_id"])

    op.drop_constraint("ck_fairness_proof_lifecycle_evidence", "fairness_proofs", type_="check")
    op.add_column("fairness_proofs", sa.Column("evaluation_fingerprint", sa.String(64)))
    op.execute(
        "ALTER TABLE fairness_proofs DISABLE TRIGGER fairness_proofs_finalized_immutable"
    )
    proofs = connection.execute(
        sa.text("SELECT id, client_seed FROM fairness_proofs WHERE status = 'revealed'")
    ).mappings()
    for row in proofs:
        connection.execute(
            sa.text(
                "UPDATE fairness_proofs SET evaluation_fingerprint = :fingerprint WHERE id = :id"
            ),
            {
                "id": row["id"],
                "fingerprint": _fingerprint(
                    "evaluate_fairness",
                    {"proof_id": str(row["id"]), "client_seed": row["client_seed"]},
                ),
            },
        )
    op.execute(
        "ALTER TABLE fairness_proofs ENABLE TRIGGER fairness_proofs_finalized_immutable"
    )
    op.create_check_constraint(
        "ck_fairness_proof_evaluation_fingerprint_hex",
        "fairness_proofs",
        "evaluation_fingerprint IS NULL OR evaluation_fingerprint ~ '^[0-9a-f]{64}$'",
    )

    op.add_column(
        "fairness_proof_events",
        sa.Column("evidence_version", sa.Integer(), nullable=False, server_default="1"),
    )
    op.add_column(
        "fairness_proof_events",
        sa.Column(
            "payload",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.alter_column("fairness_proof_events", "evidence_version", server_default=None)
    op.alter_column("fairness_proof_events", "payload", server_default=None)

    op.alter_column(
        "fairness_proofs",
        "status",
        type_=sa.String(),
        postgresql_using="status::text",
    )
    op.alter_column(
        "fairness_proof_events",
        "status",
        type_=sa.String(),
        postgresql_using="status::text",
    )
    op.execute("DROP TYPE fairness_proof_status")
    status_enum = postgresql.ENUM(
        "committed", "revealed", "expired", "cancelled", name="fairness_proof_status"
    )
    status_enum.create(op.get_bind(), checkfirst=False)
    op.alter_column(
        "fairness_proofs",
        "status",
        type_=status_enum,
        postgresql_using="status::fairness_proof_status",
    )
    op.alter_column(
        "fairness_proof_events",
        "status",
        type_=status_enum,
        postgresql_using="status::fairness_proof_status",
    )
    op.create_check_constraint(
        "ck_fairness_proof_lifecycle_evidence",
        "fairness_proofs",
        """(status = 'committed' AND outcome_id IS NULL AND client_seed IS NULL
        AND evaluation_fingerprint IS NULL AND raw_random_value IS NULL
        AND normalized_value IS NULL AND derivation_attempt IS NULL
        AND reward_key IS NULL AND reward_value IS NULL AND evaluated_at IS NULL
        AND server_seed_revealed IS NULL AND revealed_at IS NULL)
        OR (status = 'revealed' AND outcome_id IS NOT NULL AND client_seed IS NOT NULL
        AND evaluation_fingerprint IS NOT NULL AND raw_random_value IS NOT NULL
        AND normalized_value IS NOT NULL AND derivation_attempt IS NOT NULL
        AND reward_key IS NOT NULL AND reward_value IS NOT NULL AND evaluated_at IS NOT NULL
        AND server_seed_revealed IS NOT NULL AND revealed_at IS NOT NULL)
        OR (status IN ('expired', 'cancelled') AND outcome_id IS NULL
        AND client_seed IS NULL AND evaluation_fingerprint IS NULL
        AND raw_random_value IS NULL AND normalized_value IS NULL
        AND derivation_attempt IS NULL AND reward_key IS NULL AND reward_value IS NULL
        AND evaluated_at IS NULL AND server_seed_revealed IS NULL AND revealed_at IS NULL)""",
    )
    op.execute(
        """CREATE OR REPLACE FUNCTION protect_finalized_fairness_proof()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF TG_OP = 'DELETE' THEN
            RAISE EXCEPTION 'fairness proofs cannot be deleted';
          END IF;
          IF OLD.status = 'committed' AND NEW.status IN ('revealed', 'expired', 'cancelled') THEN
            RETURN NEW;
          END IF;
          RAISE EXCEPTION 'invalid or immutable fairness proof lifecycle transition';
        END $$"""
    )


def downgrade() -> None:
    """Restore the prior state vocabulary only on a database without domain evidence."""

    op.execute(
        """DO $$ BEGIN
          IF EXISTS (SELECT 1 FROM game_sessions)
             OR EXISTS (SELECT 1 FROM fairness_proofs)
             OR EXISTS (SELECT 1 FROM fairness_proof_events) THEN
            RAISE EXCEPTION 'cannot downgrade 0012 with persisted session or fairness evidence';
          END IF;
        END $$"""
    )
    op.drop_constraint("ck_fairness_proof_lifecycle_evidence", "fairness_proofs", type_="check")
    op.alter_column("fairness_proofs", "status", type_=sa.String(), postgresql_using="status::text")
    op.alter_column(
        "fairness_proof_events", "status", type_=sa.String(), postgresql_using="status::text"
    )
    op.execute("DROP TYPE fairness_proof_status")
    prior_enum = postgresql.ENUM(
        "committed",
        "evaluated",
        "revealed",
        "expired",
        "cancelled",
        "failed",
        name="fairness_proof_status",
    )
    prior_enum.create(op.get_bind(), checkfirst=False)
    op.alter_column(
        "fairness_proofs",
        "status",
        type_=prior_enum,
        postgresql_using="status::fairness_proof_status",
    )
    op.alter_column(
        "fairness_proof_events",
        "status",
        type_=prior_enum,
        postgresql_using="status::fairness_proof_status",
    )
    op.drop_column("fairness_proof_events", "payload")
    op.drop_column("fairness_proof_events", "evidence_version")
    op.drop_constraint(
        "ck_fairness_proof_evaluation_fingerprint_hex", "fairness_proofs", type_="check"
    )
    op.drop_column("fairness_proofs", "evaluation_fingerprint")
    op.create_check_constraint(
        "ck_fairness_proof_lifecycle_evidence",
        "fairness_proofs",
        """(status = 'committed' AND outcome_id IS NULL AND client_seed IS NULL
        AND raw_random_value IS NULL AND normalized_value IS NULL
        AND derivation_attempt IS NULL AND reward_key IS NULL AND reward_value IS NULL
        AND evaluated_at IS NULL AND server_seed_revealed IS NULL AND revealed_at IS NULL)
        OR (status = 'evaluated' AND outcome_id IS NOT NULL AND client_seed IS NOT NULL
        AND raw_random_value IS NOT NULL AND normalized_value IS NOT NULL
        AND derivation_attempt IS NOT NULL AND reward_key IS NOT NULL
        AND reward_value IS NOT NULL AND evaluated_at IS NOT NULL
        AND server_seed_revealed IS NULL AND revealed_at IS NULL)
        OR (status = 'revealed' AND outcome_id IS NOT NULL AND client_seed IS NOT NULL
        AND raw_random_value IS NOT NULL AND normalized_value IS NOT NULL
        AND derivation_attempt IS NOT NULL AND reward_key IS NOT NULL
        AND reward_value IS NOT NULL AND evaluated_at IS NOT NULL
        AND server_seed_revealed IS NOT NULL AND revealed_at IS NOT NULL)
        OR (status IN ('expired', 'cancelled', 'failed') AND outcome_id IS NULL
        AND raw_random_value IS NULL AND normalized_value IS NULL
        AND derivation_attempt IS NULL AND reward_key IS NULL AND reward_value IS NULL
        AND evaluated_at IS NULL AND server_seed_revealed IS NULL AND revealed_at IS NULL)""",
    )
    op.drop_constraint("uq_session_request", "game_sessions", type_="unique")
    op.create_unique_constraint(
        "uq_session_player_request", "game_sessions", ["player_id", "request_id"]
    )
    op.drop_constraint("ck_session_request_fingerprint_hex", "game_sessions", type_="check")
    op.drop_column("game_sessions", "request_fingerprint")
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
