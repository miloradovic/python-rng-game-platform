"""Real PostgreSQL constraints for protocol-versioned fairness evidence."""

from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError

from app import services
from app.config import get_settings
from app.database import Database
from app.models import (
    FairnessProof,
    FairnessProofEvent,
    FairnessProofStatus,
    FairnessSeedCustody,
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


async def test_fairness_proof_constraints_bind_session_and_isolate_seed_custody() -> None:
    database = Database(get_settings())
    try:
        player_id, game_session = await _daily_spin_session(database, "Fairness Player")
        proof = _committed_proof(
            player_id,
            game_session.id,
            game_session.game_id,
            game_session.config_version_id,
        )
        async with database.session_factory() as session:
            session.add(proof)
            await session.flush()
            session.add(FairnessSeedCustody(proof_id=proof.id, server_seed_material=b"s" * 32))
            session.add(
                FairnessProofEvent(
                    proof_id=proof.id,
                    sequence=0,
                    event_type="committed",
                    status=FairnessProofStatus.COMMITTED,
                    previous_evidence_hash=None,
                    evidence_hash=_EVENT_HASH,
                )
            )
            await session.commit()

        async with database.session_factory() as session:
            stored_proof = await session.get(FairnessProof, proof.id)
            stored_custody = await session.scalar(
                text(
                    "SELECT server_seed_material FROM fairness_seed_custody "
                    "WHERE proof_id = :proof_id"
                ),
                {"proof_id": proof.id},
            )
            assert stored_proof is not None
            assert not hasattr(stored_proof, "server_seed_material")
            assert stored_custody == b"s" * 32

        duplicate = _committed_proof(
            player_id,
            game_session.id,
            game_session.game_id,
            game_session.config_version_id,
        )
        async with database.session_factory() as session:
            session.add(duplicate)
            with pytest.raises(IntegrityError):
                await session.commit()

        wrong_owner = _committed_proof(
            uuid4(),
            game_session.id,
            game_session.game_id,
            game_session.config_version_id,
        )
        async with database.session_factory() as session:
            session.add(wrong_owner)
            with pytest.raises(IntegrityError):
                await session.commit()

        _, other_session = await _daily_spin_session(database, "Invalid Fairness Lifecycle")
        incomplete_reveal = _committed_proof(
            other_session.player_id,
            other_session.id,
            other_session.game_id,
            other_session.config_version_id,
        )
        incomplete_reveal.status = FairnessProofStatus.REVEALED
        async with database.session_factory() as session:
            session.add(incomplete_reveal)
            with pytest.raises(IntegrityError):
                await session.commit()
    finally:
        await database.dispose()


async def test_fairness_evidence_is_append_only() -> None:
    database = Database(get_settings())
    try:
        player_id, game_session = await _daily_spin_session(database, "Append Only Fairness")
        proof = _committed_proof(
            player_id,
            game_session.id,
            game_session.game_id,
            game_session.config_version_id,
        )
        async with database.session_factory() as session:
            session.add(proof)
            await session.flush()
            event = FairnessProofEvent(
                proof_id=proof.id,
                sequence=0,
                event_type="committed",
                status=FairnessProofStatus.COMMITTED,
                previous_evidence_hash=None,
                evidence_hash=_EVENT_HASH,
            )
            session.add(event)
            await session.commit()

        async with database.session_factory() as session:
            with pytest.raises(DBAPIError):
                await session.execute(
                    text("UPDATE fairness_proof_events SET event_type = 'changed' WHERE id = :id"),
                    {"id": event.id},
                )
            await session.rollback()
            with pytest.raises(DBAPIError):
                await session.execute(
                    text("UPDATE fairness_proofs SET status = 'committed' WHERE id = :id"),
                    {"id": proof.id},
                )
            await session.rollback()
            with pytest.raises(DBAPIError):
                await session.execute(
                    text("DELETE FROM fairness_proofs WHERE id = :id"),
                    {"id": proof.id},
                )
    finally:
        await database.dispose()
