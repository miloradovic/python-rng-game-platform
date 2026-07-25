"""Pure session-transition behavior without service or database mocks."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from app.domain_errors import InvalidTransitionError
from app.models import GameSession, SessionStatus
from app.session_transitions import cancel_active, expire_if_due

pytestmark = pytest.mark.unit


def make_session(*, status: SessionStatus, expires_at: datetime) -> GameSession:
    return GameSession(
        request_id=uuid4(),
        player_id=uuid4(),
        game_id=uuid4(),
        config_version_id=uuid4(),
        status=status,
        expires_at=expires_at,
        ended_at=None,
    )


def test_expire_if_due_does_not_expire_before_boundary() -> None:
    now = datetime(2026, 7, 25, tzinfo=UTC)
    game_session = make_session(
        status=SessionStatus.ACTIVE, expires_at=now + timedelta(microseconds=1)
    )

    assert expire_if_due(game_session, now) is False
    assert game_session.status is SessionStatus.ACTIVE
    assert game_session.ended_at is None


def test_expire_if_due_expires_at_boundary() -> None:
    now = datetime(2026, 7, 25, tzinfo=UTC)
    game_session = make_session(status=SessionStatus.ACTIVE, expires_at=now)

    assert expire_if_due(game_session, now) is True
    assert game_session.status is SessionStatus.EXPIRED
    assert game_session.ended_at == now


@pytest.mark.parametrize(
    "status",
    [SessionStatus.COMPLETED, SessionStatus.EXPIRED, SessionStatus.CANCELLED],
)
def test_expire_if_due_leaves_terminal_session_unchanged(status: SessionStatus) -> None:
    now = datetime(2026, 7, 25, tzinfo=UTC)
    game_session = make_session(status=status, expires_at=now - timedelta(seconds=1))

    assert expire_if_due(game_session, now) is False
    assert game_session.status is status
    assert game_session.ended_at is None


def test_cancel_active_applies_terminal_transition() -> None:
    now = datetime(2026, 7, 25, tzinfo=UTC)
    game_session = make_session(status=SessionStatus.ACTIVE, expires_at=now + timedelta(seconds=1))

    cancel_active(game_session, now)

    assert game_session.status is SessionStatus.CANCELLED
    assert game_session.ended_at == now


@pytest.mark.parametrize(
    "status",
    [SessionStatus.COMPLETED, SessionStatus.EXPIRED, SessionStatus.CANCELLED],
)
def test_cancel_active_rejects_terminal_session(status: SessionStatus) -> None:
    now = datetime(2026, 7, 25, tzinfo=UTC)
    game_session = make_session(status=status, expires_at=now)

    with pytest.raises(InvalidTransitionError):
        cancel_active(game_session, now)

    assert game_session.status is status
    assert game_session.ended_at is None
