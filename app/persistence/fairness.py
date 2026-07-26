"""Database queries; repositories flush but never commit."""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    FairnessProof,
    FairnessProofEvent,
    FairnessSeedCustody,
)


async def lock_fairness_proof_by_session(
    session: AsyncSession, session_id: uuid.UUID
) -> FairnessProof | None:
    """Lock the one proof bound to a session for replay-safe evaluation."""

    proof: FairnessProof | None = await session.scalar(
        select(FairnessProof).where(FairnessProof.session_id == session_id).with_for_update()
    )
    return proof


async def add_fairness_proof(session: AsyncSession, proof: FairnessProof) -> FairnessProof:
    session.add(proof)
    await session.flush()
    await session.refresh(proof)
    return proof


async def add_fairness_seed_custody(
    session: AsyncSession, proof_id: uuid.UUID, server_seed: bytes
) -> FairnessSeedCustody:
    custody = FairnessSeedCustody(proof_id=proof_id, server_seed_material=server_seed)
    session.add(custody)
    await session.flush()
    return custody


async def get_fairness_seed_custody(
    session: AsyncSession, proof_id: uuid.UUID
) -> FairnessSeedCustody | None:
    custody: FairnessSeedCustody | None = await session.scalar(
        select(FairnessSeedCustody).where(FairnessSeedCustody.proof_id == proof_id)
    )
    return custody


async def delete_fairness_seed_custody(session: AsyncSession, proof_id: uuid.UUID) -> None:
    """Remove unrevealed seed material after a terminal non-reveal transition."""

    custody = await get_fairness_seed_custody(session, proof_id)
    if custody is not None:
        await session.delete(custody)


async def add_fairness_proof_event(session: AsyncSession, event: FairnessProofEvent) -> None:
    session.add(event)
    await session.flush()


async def lock_latest_fairness_proof_event(
    session: AsyncSession, proof_id: uuid.UUID
) -> FairnessProofEvent | None:
    """Lock the latest append-only event to extend its durable hash chain."""

    event: FairnessProofEvent | None = await session.scalar(
        select(FairnessProofEvent)
        .where(FairnessProofEvent.proof_id == proof_id)
        .order_by(FairnessProofEvent.sequence.desc())
        .limit(1)
        .with_for_update()
    )
    return event


async def list_fairness_proof_events(
    session: AsyncSession, proof_id: uuid.UUID
) -> list[FairnessProofEvent]:
    """Load a proof's append-only lifecycle chain in verification order."""

    events = await session.scalars(
        select(FairnessProofEvent)
        .where(FairnessProofEvent.proof_id == proof_id)
        .order_by(FairnessProofEvent.sequence)
    )
    return list(events)


async def lock_fairness_proof(session: AsyncSession, proof_id: uuid.UUID) -> FairnessProof | None:
    """Lock one proof by its public identifier."""

    proof: FairnessProof | None = await session.scalar(
        select(FairnessProof).where(FairnessProof.id == proof_id).with_for_update()
    )
    return proof


async def get_fairness_proof(
    session: AsyncSession, proof_id: uuid.UUID
) -> tuple[uuid.UUID, uuid.UUID] | None:
    """Load one proof for authorization before acquiring lifecycle locks in order."""

    row = (
        await session.execute(
            select(FairnessProof.player_id, FairnessProof.session_id).where(
                FairnessProof.id == proof_id
            )
        )
    ).one_or_none()
    if row is None:
        return None
    return row.player_id, row.session_id


async def get_fairness_proof_by_outcome(
    session: AsyncSession, outcome_id: uuid.UUID
) -> FairnessProof | None:
    """Load the unique proof bound to an authoritative outcome."""

    proof: FairnessProof | None = await session.scalar(
        select(FairnessProof).where(FairnessProof.outcome_id == outcome_id)
    )
    return proof
