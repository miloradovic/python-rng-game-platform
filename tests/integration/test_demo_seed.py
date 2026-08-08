"""Real PostgreSQL and Redis coverage for optional stakeholder demo data."""

from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select

from app import repositories, services
from app.cache import create_redis_client
from app.config import get_settings
from app.database import Database
from app.models import (
    AnalyticsEvent,
    AuditRecord,
    FinalScore,
    GameSession,
    Outcome,
    Player,
    Reward,
    RewardStatus,
    SessionStatus,
)
from app.rng import HmacOutcomeProvider
from tools.seed import seed_catalogue
from tools.seed_demo import (
    CORRECT_ACTION_COUNTS,
    DEFAULT_PLAYER_COUNT,
    demo_player_id,
    demo_public_label,
    seed_demo_data,
)

pytestmark = pytest.mark.integration


async def test_demo_seed_persists_genuine_flows_and_reruns_safely() -> None:
    database = Database(get_settings())
    redis = create_redis_client(get_settings())
    assert redis is not None
    provider = HmacOutcomeProvider("demo-seed-integration-secret-key")
    player_ids = [demo_player_id(index) for index in range(1, DEFAULT_PLAYER_COUNT + 1)]
    try:
        async with database.session_factory.begin() as session:
            await seed_catalogue(session)

        first = await seed_demo_data(database, provider, client=redis)
        second = await seed_demo_data(database, provider, client=redis)

        assert first.players_created == DEFAULT_PLAYER_COUNT
        assert first.sessions_completed == DEFAULT_PLAYER_COUNT
        assert first.scores_created == DEFAULT_PLAYER_COUNT
        assert first.rewards_claimed == DEFAULT_PLAYER_COUNT
        assert first.projection_rows is not None
        assert first.projection_rows >= DEFAULT_PLAYER_COUNT
        assert second.players_created == 0
        assert second.sessions_completed == 0
        assert second.scores_created == 0
        assert second.rewards_claimed == 0
        assert second.projection_rows == first.projection_rows

        async with database.session_factory() as session:
            players = list(await session.scalars(select(Player).where(Player.id.in_(player_ids))))
            game_sessions = list(
                await session.scalars(
                    select(GameSession).where(GameSession.player_id.in_(player_ids))
                )
            )
            outcomes = list(
                await session.scalars(select(Outcome).where(Outcome.player_id.in_(player_ids)))
            )
            rewards = list(
                await session.scalars(select(Reward).where(Reward.player_id.in_(player_ids)))
            )
            scores = list(
                await session.scalars(
                    select(FinalScore)
                    .where(FinalScore.player_id.in_(player_ids))
                    .order_by(FinalScore.final_score.desc(), FinalScore.completed_at)
                )
            )
            audit_count = await session.scalar(
                select(func.count())
                .select_from(AuditRecord)
                .where(
                    AuditRecord.event_type.in_(
                        (
                            "session_started",
                            "outcome_accepted",
                            "reward_issued",
                            "final_score_submitted",
                            "reward_claimed",
                        )
                    ),
                    AuditRecord.entity_id.in_(
                        [
                            *(game_session.id for game_session in game_sessions),
                            *(outcome.id for outcome in outcomes),
                            *(reward.id for reward in rewards),
                            *(score.id for score in scores),
                        ]
                    ),
                )
            )
            analytics_count = await session.scalar(
                select(func.count())
                .select_from(AnalyticsEvent)
                .where(AnalyticsEvent.player_id.in_(player_ids))
            )
            game = await repositories.get_game(session, "skill_check")
            assert game is not None
            board = await repositories.list_canonical_scores(
                session,
                game_id=game.id,
                period_start=first.period_start,
            )
            labels = await services.public_leaderboard_labels(session, set(player_ids))

        assert len(players) == DEFAULT_PLAYER_COUNT
        assert all(player.display_name.startswith("Synthetic Demo Player ") for player in players)
        assert {player.public_label for player in players} == {
            demo_public_label(index) for index in range(1, DEFAULT_PLAYER_COUNT + 1)
        }
        assert len(game_sessions) == DEFAULT_PLAYER_COUNT
        assert all(item.status == SessionStatus.COMPLETED for item in game_sessions)
        assert len(outcomes) == DEFAULT_PLAYER_COUNT
        assert len(rewards) == DEFAULT_PLAYER_COUNT
        assert all(reward.status == RewardStatus.CLAIMED for reward in rewards)
        assert [score.final_score for score in scores] == sorted(
            (correct * 200 for correct in CORRECT_ACTION_COUNTS[:DEFAULT_PLAYER_COUNT]),
            reverse=True,
        )
        assert all(score.period_start == first.period_start for score in scores)
        assert audit_count == DEFAULT_PLAYER_COUNT * 5
        assert analytics_count == DEFAULT_PLAYER_COUNT * 5
        assert {score.player_id for score in board}.issuperset(player_ids)
        assert labels == {
            demo_player_id(index): demo_public_label(index)
            for index in range(1, DEFAULT_PLAYER_COUNT + 1)
        }
        assert first.period_start == services.leaderboard_period(datetime.now(UTC))[0]
    finally:
        await redis.aclose()
        await database.dispose()
