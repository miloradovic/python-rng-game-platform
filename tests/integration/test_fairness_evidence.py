"""Real PostgreSQL constraints for protocol-versioned fairness evidence."""

from datetime import timedelta
from uuid import UUID, uuid4

import pytest

from app import repositories, services
from app.config import get_settings
from app.database import Database
from app.models import (
    FairnessProof,
    FairnessProofStatus,
    GameSession,
    Player,
)
from tools.seed import seed_catalogue

pytestmark = pytest.mark.integration

_COMMITMENT = "a" * 64
_MAPPING_DIGEST = "b" * 64
_EVENT_HASH = "c" * 64


async def _daily_spin_session(database: Database, display_name: str) -> tuple[UUID, GameSession]:
    player_id = uuid4()
    async with database.session_factory.begin() as session:
        await seed_catalogue(session)
        session.add(Player(id=player_id, display_name=display_name))
    async with database.session_factory() as session:
        game_session = await services.create_session(
            session,
            request_id=uuid4(),
            player_id=player_id,
            owner_id=player_id,
            game_key="daily_spin",
        )
    return player_id, game_session


def _committed_proof(
    player_id: UUID, game_session_id: UUID, game_id: UUID, config_id: UUID
) -> FairnessProof:
    return FairnessProof(
        session_id=game_session_id,
        player_id=player_id,
        game_id=game_id,
        game_key="daily_spin",
        config_version_id=config_id,
        outcome_id=None,
        status=FairnessProofStatus.COMMITTED,
        protocol_version="rng-game-platform-pvf-v1",
        algorithm="hmac-sha256",
        server_seed_commitment=_COMMITMENT,
        nonce=0,
        client_seed=None,
        evaluation_fingerprint=None,
        mapping_version="daily-spin-weighted-reward-v1",
        mapping_digest=_MAPPING_DIGEST,
        raw_random_value=None,
        normalized_value=None,
        derivation_attempt=None,
        reward_key=None,
        reward_value=None,
        evaluated_at=None,
        server_seed_revealed=None,
        revealed_at=None,
    )


async def test_event_chain_detects_every_material_revealed_proof_mutation() -> None:
    database = Database(get_settings())
    try:
        player_id, game_session = await _daily_spin_session(database, "Mutation Detection")
        async with database.session_factory() as session:
            proof = await services.commit_fairness(
                session, session_id=game_session.id, owner_id=player_id
            )
        async with database.session_factory() as session:
            _, _, revealed = await services.evaluate_fairness(
                session,
                proof_id=proof.id,
                owner_id=player_id,
                client_seed="mutation-client-seed",
            )

        async with database.session_factory() as session:
            stored = await session.get(FairnessProof, revealed.id)
            events = await repositories.list_fairness_proof_events(session, revealed.id)
            assert stored is not None
            assert stored.evaluated_at is not None
            assert stored.revealed_at is not None
            mutations: tuple[tuple[str, object], ...] = (
                ("server_seed_commitment", "0" * 64),
                ("mapping_digest", "1" * 64),
                ("client_seed", "other-client-seed"),
                ("outcome_id", uuid4()),
                ("reward_key", "other-reward"),
                ("reward_value", stored.reward_value + 1 if stored.reward_value is not None else 1),
                ("raw_random_value", "2" * 64),
                ("normalized_value", (stored.normalized_value or 0) + 1),
                ("derivation_attempt", (stored.derivation_attempt or 0) + 1),
                ("server_seed_revealed", "3" * 64),
                ("evaluated_at", stored.evaluated_at + timedelta(microseconds=1)),
                ("revealed_at", stored.revealed_at + timedelta(microseconds=1)),
            )
            for field, changed in mutations:
                original = getattr(stored, field)
                setattr(stored, field, changed)
                verified, _ = services.verify_fairness_event_chain(stored, events)
                assert not verified, field
                setattr(stored, field, original)
            await session.rollback()
    finally:
        await database.dispose()
