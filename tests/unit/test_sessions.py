"""Deterministic session lifecycle tests."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from app.models import GameSession, SessionStatus
from app.services import InvalidTransitionError, cancel_active, expire_if_due

pytestmark = pytest.mark.unit


def make_session(expires_at: datetime) -> GameSession:
    return GameSession(
        request_id=uuid4(),
        player_id=uuid4(),
        game_id=uuid4(),
        config_version_id=uuid4(),
        status=SessionStatus.ACTIVE,
        expires_at=expires_at,
        ended_at=None,
    )


def test_expiry_boundary_is_inclusive_and_terminal() -> None:
    now = datetime(2026, 7, 12, tzinfo=UTC)
    game_session = make_session(now)
    assert expire_if_due(game_session, now) is True
    assert game_session.status == SessionStatus.EXPIRED
    assert game_session.ended_at == now
    assert expire_if_due(game_session, now + timedelta(seconds=1)) is False


def test_terminal_session_cannot_be_cancelled() -> None:
    now = datetime(2026, 7, 12, tzinfo=UTC)
    game_session = make_session(now)
    expire_if_due(game_session, now)
    with pytest.raises(InvalidTransitionError):
        cancel_active(game_session, now)
