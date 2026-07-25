"""Pure construction and hashing helpers for fairness lifecycle evidence."""

import hashlib
import json
import uuid
from datetime import UTC, datetime

from app.models import FairnessProof, FairnessProofStatus


def fairness_event_hash_v1(
    *,
    proof_id: uuid.UUID,
    sequence: int,
    event_type: str,
    status: FairnessProofStatus,
    created_at: datetime,
    previous_evidence_hash: str | None,
) -> str:
    """Produce the frozen v1 append-only event hash from explicit UTC evidence."""

    timestamp = created_at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    evidence = (
        "RNG-GAME-PLATFORM-PVF-EVENT/1\n"
        f"proof_id={proof_id}\n"
        f"sequence={sequence}\n"
        f"event_type={event_type}\n"
        f"status={status.value}\n"
        f"created_at={timestamp}\n"
        f"previous_evidence_hash={previous_evidence_hash or ''}\n"
    )
    return hashlib.sha256(evidence.encode("ascii")).hexdigest()


def fairness_event_hash(
    *,
    proof_id: uuid.UUID,
    sequence: int,
    event_type: str,
    status: FairnessProofStatus,
    created_at: datetime,
    previous_evidence_hash: str | None,
    payload: dict[str, object],
) -> str:
    """Bind the complete v2 lifecycle payload using canonical JSON evidence."""

    evidence = {
        "created_at": created_at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        "event_type": event_type,
        "payload": payload,
        "previous_evidence_hash": previous_evidence_hash,
        "proof_id": str(proof_id),
        "sequence": sequence,
        "status": status.value,
    }
    encoded = json.dumps(evidence, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(("RNG-GAME-PLATFORM-PVF-EVENT/2\n" + encoded).encode("ascii")).hexdigest()


def commit_event_payload(proof: FairnessProof) -> dict[str, object]:
    return {
        "algorithm": proof.algorithm,
        "config_version_id": str(proof.config_version_id),
        "game_key": proof.game_key,
        "mapping_digest": proof.mapping_digest,
        "mapping_version": proof.mapping_version,
        "nonce": proof.nonce,
        "protocol_version": proof.protocol_version,
        "server_seed_commitment": proof.server_seed_commitment,
        "session_id": str(proof.session_id),
    }


def terminal_event_payload(proof: FairnessProof, now: datetime) -> dict[str, object]:
    return {
        **commit_event_payload(proof),
        "terminal_at": now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
    }


def revealed_event_payload(proof: FairnessProof) -> dict[str, object]:
    return {
        **commit_event_payload(proof),
        "client_seed": proof.client_seed,
        "derivation_attempt": proof.derivation_attempt,
        "evaluated_at": optional_timestamp(proof.evaluated_at),
        "evaluation_fingerprint": proof.evaluation_fingerprint,
        "normalized_value": proof.normalized_value,
        "outcome_id": str(proof.outcome_id) if proof.outcome_id is not None else None,
        "raw_random_value": proof.raw_random_value,
        "revealed_at": optional_timestamp(proof.revealed_at),
        "reward_key": proof.reward_key,
        "reward_value": proof.reward_value,
        "server_seed_revealed": proof.server_seed_revealed,
    }


def optional_timestamp(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
