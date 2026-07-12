"""Add analytics summary query indexes.

Revision ID: 0006_analytics
Revises: 0005_rewards
"""

from collections.abc import Sequence

from alembic import op

revision = "0006_analytics"
down_revision = "0005_rewards"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """INSERT INTO analytics_events
            (id, created_at, event_key, event_type, player_id, payload)
        SELECT md5('session_started:' || s.id::text)::uuid,
               s.created_at,
               'session_started:' || s.id::text,
               'session_started',
               s.player_id,
               jsonb_build_object(
                   'session_id', s.id::text,
                   'game_key', g.key,
                   'config_version_id', s.config_version_id::text
               )
        FROM game_sessions AS s
        JOIN games AS g ON g.id = s.game_id
        ON CONFLICT (event_key) DO NOTHING"""
    )
    op.execute(
        """INSERT INTO analytics_events
            (id, created_at, event_key, event_type, player_id, payload)
        SELECT md5('game_played:' || o.id::text)::uuid,
               o.created_at,
               'game_played:' || o.id::text,
               'game_played',
               s.player_id,
               jsonb_build_object(
                   'outcome_id', o.id::text,
                   'session_id', s.id::text,
                   'game_key', g.key,
                   'config_version_id', o.config_version_id::text
               )
        FROM outcomes AS o
        JOIN game_sessions AS s ON s.id = o.session_id
        JOIN games AS g ON g.id = s.game_id
        WHERE o.status = 'accepted'
        ON CONFLICT (event_key) DO NOTHING"""
    )
    op.execute(
        """INSERT INTO analytics_events
            (id, created_at, event_key, event_type, player_id, payload)
        SELECT md5('reward_issued:' || r.id::text)::uuid,
               r.created_at,
               'reward_issued:' || r.id::text,
               'reward_issued',
               r.player_id,
               jsonb_build_object(
                   'reward_id', r.id::text,
                   'outcome_id', r.outcome_id::text,
                   'player_id', r.player_id::text,
                   'value', r.value,
                   'status', 'issued',
                   'game_key', g.key
               )
        FROM rewards AS r
        JOIN outcomes AS o ON o.id = r.outcome_id
        JOIN game_sessions AS s ON s.id = o.session_id
        JOIN games AS g ON g.id = s.game_id
        ON CONFLICT (event_key) DO UPDATE
        SET payload = analytics_events.payload ||
            jsonb_build_object('game_key', EXCLUDED.payload->'game_key')"""
    )
    op.execute(
        """INSERT INTO analytics_events
            (id, created_at, event_key, event_type, player_id, payload)
        SELECT md5('reward_claimed:' || r.id::text)::uuid,
               r.claimed_at,
               'reward_claimed:' || r.id::text,
               'reward_claimed',
               r.player_id,
               jsonb_build_object(
                   'reward_id', r.id::text,
                   'outcome_id', r.outcome_id::text,
                   'player_id', r.player_id::text,
                   'value', r.value,
                   'status', 'claimed',
                   'game_key', g.key
               )
        FROM rewards AS r
        JOIN outcomes AS o ON o.id = r.outcome_id
        JOIN game_sessions AS s ON s.id = o.session_id
        JOIN games AS g ON g.id = s.game_id
        WHERE r.status = 'claimed' AND r.claimed_at IS NOT NULL
        ON CONFLICT (event_key) DO UPDATE
        SET payload = analytics_events.payload ||
            jsonb_build_object('game_key', EXCLUDED.payload->'game_key')"""
    )
    op.create_index(
        "ix_analytics_player_type_created",
        "analytics_events",
        ["player_id", "event_type", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_analytics_player_type_created", table_name="analytics_events")
