"""Durable orchestration shared by session-ending use cases."""

from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app import repositories
from app.fairness_events import fairness_event_hash, terminal_event_payload
from app.models import (
    FairnessProof,
    FairnessProofEvent,
    FairnessProofStatus,
    GameSession,
    SessionStatus,
)
from app.services.errors import InvalidTransitionError
from app.session_transitions import expire_if_due


async def end_committed_fairness_proof(
    session: AsyncSession,
    proof: FairnessProof,
    *,
    status: FairnessProofStatus,
    now: datetime,
) -> bool:
    """Terminally retire an unrevealed proof and its seed custody evidence."""

    if proof.status != FairnessProofStatus.COMMITTED:
        return False
    if status not in (FairnessProofStatus.EXPIRED, FairnessProofStatus.CANCELLED):
        raise InvalidTransitionError
    previous_event = await repositories.lock_latest_fairness_proof_event(session, proof.id)
    if previous_event is None:
        raise InvalidTransitionError
    proof.status = status
    await repositories.delete_fairness_seed_custody(session, proof.id)
    sequence = previous_event.sequence + 1
    event_type = status.value
    payload = terminal_event_payload(proof, now)
    await repositories.add_fairness_proof_event(
        session,
        FairnessProofEvent(
            proof_id=proof.id,
            sequence=sequence,
            event_type=event_type,
            evidence_version=2,
            payload=payload,
            status=status,
            previous_evidence_hash=previous_event.evidence_hash,
            evidence_hash=fairness_event_hash(
                proof_id=proof.id,
                sequence=sequence,
                event_type=event_type,
                status=status,
                created_at=now,
                previous_evidence_hash=previous_event.evidence_hash,
                payload=payload,
            ),
            created_at=now,
        ),
    )
    return True


async def finalize_expired_session(
    session: AsyncSession,
    game_session: GameSession,
    *,
    now: datetime,
    locked_proof: FairnessProof | None = None,
) -> bool:
    """Atomically finalize an elapsed session and any committed fairness proof."""

    session_changed = expire_if_due(game_session, now)
    if not session_changed and game_session.status != SessionStatus.EXPIRED:
        return False
    proof = locked_proof
    if proof is None:
        proof = await repositories.lock_fairness_proof_by_session(session, game_session.id)
    proof_changed = False
    if proof is not None:
        proof_changed = await end_committed_fairness_proof(
            session, proof, status=FairnessProofStatus.EXPIRED, now=now
        )
    return session_changed or proof_changed
