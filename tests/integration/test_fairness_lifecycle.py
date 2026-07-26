"""Real PostgreSQL constraints for protocol-versioned fairness evidence."""

from datetime import timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from app import repositories, services
from app.config import get_settings
from app.database import Database
from app.models import (
    FairnessProof,
    FairnessProofEvent,
    FairnessProofStatus,
    FairnessSeedCustody,
    GameSession,
    Player,
    SessionStatus,
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
                    evidence_version=1,
                    payload={},
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
                evidence_version=1,
                payload={},
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


async def test_cancelling_a_committed_session_removes_unrevealed_seed_custody() -> None:
    database = Database(get_settings())
    try:
        player_id, game_session = await _daily_spin_session(database, "Cancelled Fairness")
        async with database.session_factory() as session:
            proof = await services.commit_fairness(
                session, session_id=game_session.id, owner_id=player_id
            )
        async with database.session_factory() as session:
            _ = await services.cancel_session(session, game_session.id, player_id)

        async with database.session_factory() as session:
            stored = await session.get(FairnessProof, proof.id)
            custody = await repositories.get_fairness_seed_custody(session, proof.id)
            events = list(
                (
                    await session.scalars(
                        select(FairnessProofEvent)
                        .where(FairnessProofEvent.proof_id == proof.id)
                        .order_by(FairnessProofEvent.sequence)
                    )
                ).all()
            )
            assert stored is not None
            assert stored.status == FairnessProofStatus.CANCELLED
            assert custody is None
            assert len(events) == 2
            assert events[1].previous_evidence_hash == events[0].evidence_hash
    finally:
        await database.dispose()


async def test_expired_session_atomically_terminates_committed_fairness() -> None:
    database = Database(get_settings())
    try:
        player_id, game_session = await _daily_spin_session(database, "Expired Fairness")
        async with database.session_factory() as session:
            proof = await services.commit_fairness(
                session, session_id=game_session.id, owner_id=player_id
            )
        after_expiry = game_session.expires_at + timedelta(seconds=1)

        async with database.session_factory() as session:
            expired = await services.retrieve_session(
                session, game_session.id, player_id, clock=lambda: after_expiry
            )
        assert expired.status is SessionStatus.EXPIRED

        async with database.session_factory() as session:
            stored = await session.get(FairnessProof, proof.id)
            custody = await repositories.get_fairness_seed_custody(session, proof.id)
            events = await repositories.list_fairness_proof_events(session, proof.id)
        assert stored is not None
        assert stored.status is FairnessProofStatus.EXPIRED
        assert custody is None
        assert [event.status for event in events] == [
            FairnessProofStatus.COMMITTED,
            FairnessProofStatus.EXPIRED,
        ]
        assert events[1].previous_evidence_hash == events[0].evidence_hash

        async with database.session_factory() as session:
            retried = await services.retrieve_session(
                session, game_session.id, player_id, clock=lambda: after_expiry
            )
            retried_events = await repositories.list_fairness_proof_events(session, proof.id)
        assert retried.status is SessionStatus.EXPIRED
        assert len(retried_events) == 2
    finally:
        await database.dispose()


async def test_expiration_cleanup_allows_replacement_session_creation() -> None:
    database = Database(get_settings())
    try:
        player_id, game_session = await _daily_spin_session(database, "Fairness Replacement")
        async with database.session_factory() as session:
            proof = await services.commit_fairness(
                session, session_id=game_session.id, owner_id=player_id
            )
        after_cooldown = game_session.created_at + timedelta(days=1, seconds=1)

        async with database.session_factory() as session:
            replacement = await services.create_session(
                session,
                request_id=uuid4(),
                player_id=player_id,
                owner_id=player_id,
                game_key="daily_spin",
                clock=lambda: after_cooldown,
            )

        assert replacement.id != game_session.id
        assert replacement.status is SessionStatus.ACTIVE
        async with database.session_factory() as session:
            stored_proof = await session.get(FairnessProof, proof.id)
            custody = await repositories.get_fairness_seed_custody(session, proof.id)
        assert stored_proof is not None
        assert stored_proof.status is FairnessProofStatus.EXPIRED
        assert custody is None
    finally:
        await database.dispose()


@pytest.mark.parametrize("failure_point", ["custody_delete", "event_append"])
async def test_expiration_rolls_back_if_terminal_side_effect_fails(
    monkeypatch: pytest.MonkeyPatch,
    failure_point: str,
) -> None:
    database = Database(get_settings())
    try:
        player_id, game_session = await _daily_spin_session(database, "Fairness Rollback")
        async with database.session_factory() as session:
            proof = await services.commit_fairness(
                session, session_id=game_session.id, owner_id=player_id
            )
        after_expiry = game_session.expires_at + timedelta(seconds=1)

        async def fail_terminal_side_effect(*args: object, **kwargs: object) -> None:
            del args, kwargs
            raise RuntimeError("injected terminal side-effect failure")

        target = (
            "delete_fairness_seed_custody"
            if failure_point == "custody_delete"
            else "add_fairness_proof_event"
        )
        monkeypatch.setattr(repositories, target, fail_terminal_side_effect)
        with pytest.raises(RuntimeError, match="injected terminal side-effect failure"):
            async with database.session_factory() as session:
                await services.retrieve_session(
                    session, game_session.id, player_id, clock=lambda: after_expiry
                )

        async with database.session_factory() as session:
            stored_session = await session.get(GameSession, game_session.id)
            stored_proof = await session.get(FairnessProof, proof.id)
            custody = await repositories.get_fairness_seed_custody(session, proof.id)
            events = await repositories.list_fairness_proof_events(session, proof.id)
        assert stored_session is not None
        assert stored_session.status is SessionStatus.ACTIVE
        assert stored_proof is not None
        assert stored_proof.status is FairnessProofStatus.COMMITTED
        assert custody is not None
        assert len(events) == 1
    finally:
        await database.dispose()
