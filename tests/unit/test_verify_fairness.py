"""Offline fairness verification command coverage."""

import json
from uuid import UUID, uuid4

import pytest

from tools import verify_fairness

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("verified", "code", "expected_exit"),
    [(True, "verified", 0), (False, "event_hash_mismatch", 1)],
)
def test_offline_verifier_reports_machine_readable_verdict(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    verified: bool,
    code: str,
    expected_exit: int,
) -> None:
    outcome_id = uuid4()
    player_id = uuid4()

    async def fake_verify(requested_outcome: UUID, requested_player: UUID) -> tuple[bool, str]:
        assert requested_outcome == outcome_id
        assert requested_player == player_id
        return verified, code

    monkeypatch.setattr(verify_fairness, "verify", fake_verify)
    result = verify_fairness.main(["--outcome-id", str(outcome_id), "--player-id", str(player_id)])

    assert result == expected_exit
    assert json.loads(capsys.readouterr().out) == {
        "outcome_id": str(outcome_id),
        "verified": verified,
        "code": code,
    }
