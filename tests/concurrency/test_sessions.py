"""Concurrent session creation is serialized by the player row."""

import asyncio
from datetime import timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select

from app import repositories, services
from app.config import get_settings
from app.database import Database
from app.models import (
    FairnessProof,
    FairnessProofEvent,
    FairnessProofStatus,
    GameSession,
    Player,
    SessionStatus,
)
from tools.seed import seed_catalogue

pytestmark = pytest.mark.concurrency


async def test_concurrent_retry_returns_one_session() -> None:
    database = Database(get_settings())
    player_id = uuid4()
    request_id = uuid4()
    try:
        async with database.session_factory.begin() as session:
            await seed_catalogue(session)
            session.add(Player(id=player_id, display_name="Concurrent Test"))

        async def create() -> GameSession:
            async with database.session_factory() as session:
                return await services.create_session(
                    session,
                    request_id=request_id,
                    player_id=player_id,
                    owner_id=player_id,
                    game_key="skill_check",
                )

        first, second = await asyncio.gather(create(), create())
        assert first.id == second.id
    finally:
        await database.dispose()


async def test_cross_player_request_key_race_returns_one_stable_conflict() -> None:
    database = Database(get_settings())
    first_player, second_player, request_id = uuid4(), uuid4(), uuid4()
    try:
        async with database.session_factory.begin() as session:
            await seed_catalogue(session)
            session.add_all(
                [
                    Player(id=first_player, display_name="First Intent"),
                    Player(id=second_player, display_name="Second Intent"),
                ]
            )

        async def create(player_id: UUID) -> object:
            async with database.session_factory() as session:
                return await services.create_session(
                    session,
                    request_id=request_id,
                    player_id=player_id,
                    owner_id=player_id,
                    game_key="skill_check",
                )

        results = await asyncio.gather(
            create(first_player), create(second_player), return_exceptions=True
        )
        assert sum(isinstance(result, GameSession) for result in results) == 1
        assert sum(isinstance(result, services.IdempotencyConflictError) for result in results) == 1
    finally:
        await database.dispose()


async def test_expiration_and_replacement_creation_race_to_consistent_terminal_state() -> None:
    database = Database(get_settings())
    player_id = uuid4()
    try:
        async with database.session_factory.begin() as session:
            await seed_catalogue(session)
            session.add(Player(id=player_id, display_name="Expiration Replacement Race"))
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
        after_cooldown = game_session.created_at + timedelta(days=1, seconds=1)

        async def expire_from_read() -> GameSession:
            async with database.session_factory() as session:
                return await services.retrieve_session(
                    session,
                    game_session.id,
                    player_id,
                    clock=lambda: after_cooldown,
                )

        async def create_replacement() -> GameSession:
            async with database.session_factory() as session:
                return await services.create_session(
                    session,
                    request_id=uuid4(),
                    player_id=player_id,
                    owner_id=player_id,
                    game_key="daily_spin",
                    clock=lambda: after_cooldown,
                )

        expired, replacement = await asyncio.gather(expire_from_read(), create_replacement())
        assert expired.status is SessionStatus.EXPIRED
        assert replacement.status is SessionStatus.ACTIVE
        assert replacement.id != expired.id

        async with database.session_factory() as session:
            stored_proof = await session.get(FairnessProof, proof.id)
            custody = await repositories.get_fairness_seed_custody(session, proof.id)
            event_count = await session.scalar(
                select(func.count())
                .select_from(FairnessProofEvent)
                .where(FairnessProofEvent.proof_id == proof.id)
            )
        assert stored_proof is not None
        assert stored_proof.status is FairnessProofStatus.EXPIRED
        assert custody is None
        assert event_count == 2
    finally:
        await database.dispose()
