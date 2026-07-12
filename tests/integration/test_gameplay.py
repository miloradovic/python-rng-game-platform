"""Real PostgreSQL gameplay and audit transaction coverage."""

from uuid import uuid4

import pytest
from sqlalchemy import func, select

from app import services
from app.config import get_settings
from app.database import Database
from app.models import AnalyticsEvent, AuditRecord, Outcome, Player, Reward, SessionStatus
from app.rng import HmacOutcomeProvider
from tools.seed import seed_catalogue

pytestmark = pytest.mark.integration


async def test_all_games_persist_one_config_bound_outcome_and_audit() -> None:
    database = Database(get_settings())
    provider = HmacOutcomeProvider("integration-gameplay-secret-key-32-bytes")
    try:
        async with database.session_factory.begin() as session:
            await seed_catalogue(session)
        for game_key in ("daily_spin", "prediction_card", "skill_check"):
            player_id = uuid4()
            async with database.session_factory.begin() as session:
                session.add(Player(id=player_id, display_name=f"Play {game_key}"))
            async with database.session_factory() as session:
                game_session = await services.create_session(
                    session,
                    request_id=uuid4(),
                    player_id=player_id,
                    owner_id=player_id,
                    game_key=game_key,
                )
                choice = "red" if game_key == "prediction_card" else None
                actions = (
                    list(game_session.challenge["sequence"]) if game_key == "skill_check" else None
                )
                outcome = await services.play_session(
                    session,
                    session_id=game_session.id,
                    owner_id=player_id,
                    choice=choice,
                    actions=actions,
                    provider=provider,
                )
                assert outcome.config_version_id == game_session.config_version_id
                assert game_session.status == SessionStatus.COMPLETED
                reward = await services.claim_session_reward(
                    session, session_id=game_session.id, owner_id=player_id
                )
                summary = await services.analytics_game_summary(
                    session,
                    owner_id=player_id,
                    start_at=None,
                    end_at=None,
                    game_key=game_key,
                )
                assert len(summary) == 1
                assert summary[0].game_key == game_key
                assert summary[0].plays == 1
                assert summary[0].rewards_issued == 1
                assert summary[0].average_reward_value == reward.value
        async with database.session_factory() as session:
            outcome_count = await session.scalar(select(func.count()).select_from(Outcome))
            reward_count = await session.scalar(select(func.count()).select_from(Reward))
            assert outcome_count is not None and outcome_count >= 3
            assert reward_count is not None and reward_count >= 3
            audit_count = await session.scalar(
                select(func.count())
                .select_from(AuditRecord)
                .where(AuditRecord.event_type == "outcome_accepted")
            )
            assert audit_count is not None and audit_count >= 3
            for event_type in (
                "session_started",
                "game_played",
                "reward_issued",
                "reward_claimed",
            ):
                event_count = await session.scalar(
                    select(func.count())
                    .select_from(AnalyticsEvent)
                    .where(AnalyticsEvent.event_type == event_type)
                )
                assert event_count is not None and event_count >= 3
    finally:
        await database.dispose()
