"""Concurrent play cannot create two terminal outcomes."""

import asyncio
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from app import services
from app.config import get_settings
from app.database import Database
from app.models import (
    AnalyticsEvent,
    AuditRecord,
    FairnessProof,
    FairnessProofStatus,
    GameSession,
    Outcome,
    Player,
    Reward,
)
from tools.seed import seed_catalogue

pytestmark = pytest.mark.concurrency


async def test_concurrent_play_has_one_durable_winner() -> None:
    database = Database(get_settings())
    player_id = uuid4()
    try:
        async with database.session_factory.begin() as session:
            await seed_catalogue(session)
            session.add(Player(id=player_id, display_name="Concurrent Play"))
        async with database.session_factory() as session:
            game_session = await services.create_session(
                session,
                request_id=uuid4(),
                player_id=player_id,
                owner_id=player_id,
                game_key="daily_spin",
            )

        async def commit() -> FairnessProof:
            async with database.session_factory() as session:
                return await services.commit_fairness(
                    session, session_id=game_session.id, owner_id=player_id
                )

        commits = await asyncio.gather(commit(), commit())
        assert commits[0].id == commits[1].id

        async def evaluate() -> tuple[Outcome, Reward, object]:
            async with database.session_factory() as session:
                return await services.evaluate_fairness(
                    session,
                    proof_id=commits[0].id,
                    owner_id=player_id,
                    client_seed="concurrent-client-seed",
                )

        results = await asyncio.gather(evaluate(), evaluate())
        assert results[0][0].id == results[1][0].id
        assert results[0][1].id == results[1][1].id

        async def claim() -> Reward:
            async with database.session_factory() as session:
                return await services.claim_session_reward(
                    session, session_id=game_session.id, owner_id=player_id
                )

        claims = await asyncio.gather(claim(), claim())
        assert claims[0].id == claims[1].id
        assert claims[0].claimed_at == claims[1].claimed_at

        async with database.session_factory() as session:
            claim_audits = await session.scalar(
                select(func.count())
                .select_from(AuditRecord)
                .where(
                    AuditRecord.entity_id == claims[0].id,
                    AuditRecord.event_type == "reward_claimed",
                )
            )
            claim_events = await session.scalar(
                select(func.count())
                .select_from(AnalyticsEvent)
                .where(AnalyticsEvent.event_key == f"reward_claimed:{claims[0].id}")
            )
        assert claim_audits == 1
        assert claim_events == 1
    finally:
        await database.dispose()


async def test_evaluation_and_cancellation_race_to_one_valid_terminal_state() -> None:
    database = Database(get_settings())
    player_id = uuid4()
    try:
        async with database.session_factory.begin() as session:
            await seed_catalogue(session)
            session.add(Player(id=player_id, display_name="Fairness Race"))
        async with database.session_factory() as session:
            game_session = await services.create_session(
                session,
                request_id=uuid4(),
                player_id=player_id,
                owner_id=player_id,
                game_key="daily_spin",
            )
            proof = await services.commit_fairness(
                session, session_id=game_session.id, owner_id=player_id
            )

        async def evaluate() -> object:
            async with database.session_factory() as session:
                return await services.evaluate_fairness(
                    session,
                    proof_id=proof.id,
                    owner_id=player_id,
                    client_seed="race-client-seed",
                )

        async def cancel() -> object:
            async with database.session_factory() as session:
                return await services.cancel_session(session, game_session.id, player_id)

        results = await asyncio.gather(evaluate(), cancel(), return_exceptions=True)
        assert sum(not isinstance(result, Exception) for result in results) == 1
        assert sum(isinstance(result, services.InvalidTransitionError) for result in results) == 1

        async with database.session_factory() as session:
            stored = await session.get(FairnessProof, proof.id)
            outcome_count = await session.scalar(
                select(func.count())
                .select_from(Outcome)
                .where(Outcome.session_id == game_session.id)
            )
            reward_count = await session.scalar(
                select(func.count()).select_from(Reward).where(Reward.player_id == player_id)
            )
        assert stored is not None
        assert stored.status in (FairnessProofStatus.REVEALED, FairnessProofStatus.CANCELLED)
        expected_count = 1 if stored.status is FairnessProofStatus.REVEALED else 0
        assert outcome_count == expected_count
        assert reward_count == expected_count
        assert any(isinstance(result, (tuple, GameSession)) for result in results)
    finally:
        await database.dispose()


async def test_expiry_and_cancellation_race_without_outcome_or_reward() -> None:
    database = Database(get_settings())
    player_id = uuid4()
    try:
        async with database.session_factory.begin() as session:
            await seed_catalogue(session)
            session.add(Player(id=player_id, display_name="Expiry Race"))
        async with database.session_factory() as session:
            game_session = await services.create_session(
                session,
                request_id=uuid4(),
                player_id=player_id,
                owner_id=player_id,
                game_key="daily_spin",
            )
            proof = await services.commit_fairness(
                session, session_id=game_session.id, owner_id=player_id
            )
        after_expiry = game_session.expires_at + timedelta(seconds=1)

        async def expire_during_evaluation() -> object:
            async with database.session_factory() as session:
                return await services.evaluate_fairness(
                    session,
                    proof_id=proof.id,
                    owner_id=player_id,
                    client_seed="expired-client-seed",
                    clock=lambda: after_expiry,
                )

        async def cancel() -> object:
            async with database.session_factory() as session:
                return await services.cancel_session(session, game_session.id, player_id)

        results = await asyncio.gather(expire_during_evaluation(), cancel(), return_exceptions=True)
        assert all(
            isinstance(
                result,
                (GameSession, services.SessionExpiredError, services.InvalidTransitionError),
            )
            for result in results
        )
        async with database.session_factory() as session:
            stored = await session.get(FairnessProof, proof.id)
            outcome_count = await session.scalar(
                select(func.count())
                .select_from(Outcome)
                .where(Outcome.session_id == game_session.id)
            )
            reward_count = await session.scalar(
                select(func.count()).select_from(Reward).where(Reward.player_id == player_id)
            )
        assert stored is not None
        assert stored.status in (FairnessProofStatus.EXPIRED, FairnessProofStatus.CANCELLED)
        assert outcome_count == 0
        assert reward_count == 0
    finally:
        await database.dispose()
