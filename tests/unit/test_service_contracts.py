"""Characterization tests for existing service contracts before decomposition."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app import repositories, services
from app.models import Game, GameConfigVersion, GameSession, Outcome, Reward, SessionStatus
from app.rng import HmacOutcomeProvider

pytestmark = pytest.mark.unit


async def test_create_session_retry_commits_once_without_repeating_the_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The established idempotent retry boundary commits and returns the original session."""

    player_id = uuid4()
    existing = GameSession(
        id=uuid4(),
        request_id=uuid4(),
        player_id=player_id,
        game_id=uuid4(),
        config_version_id=uuid4(),
        status=SessionStatus.ACTIVE,
        expires_at=datetime(2026, 1, 1, tzinfo=UTC),
        challenge={},
    )
    session = AsyncMock()
    monkeypatch.setattr(repositories, "lock_player", AsyncMock(return_value=object()))
    monkeypatch.setattr(repositories, "get_session_by_request", AsyncMock(return_value=existing))
    get_game = AsyncMock()
    monkeypatch.setattr(repositories, "get_game", get_game)

    result = await services.create_session(
        session,
        request_id=existing.request_id,
        player_id=player_id,
        owner_id=player_id,
        game_key="skill_check",
    )

    assert result is existing
    session.commit.assert_awaited_once()
    get_game.assert_not_awaited()


async def test_retrieve_session_never_commits_even_when_reporting_effective_expiry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Queries can report effective lifecycle state without owning a durable transaction."""

    now = datetime(2026, 1, 1, tzinfo=UTC)
    player_id = uuid4()
    game_session = GameSession(
        id=uuid4(),
        request_id=uuid4(),
        player_id=player_id,
        game_id=uuid4(),
        config_version_id=uuid4(),
        status=SessionStatus.ACTIVE,
        expires_at=now - timedelta(seconds=1),
        ended_at=None,
        challenge={},
    )
    session = AsyncMock()
    monkeypatch.setattr(repositories, "get_session", AsyncMock(return_value=game_session))

    result = await services.retrieve_session(
        session, session_id=game_session.id, owner_id=player_id, clock=lambda: now
    )

    assert result.status is SessionStatus.EXPIRED
    session.commit.assert_not_awaited()


@pytest.mark.parametrize(
    ("game_key", "payload", "choice", "actions", "challenge", "result_key"),
    [
        (
            "daily_spin",
            {
                "game_type": "daily_spin",
                "cooldown_seconds": 86400,
                "rewards": [
                    {"key": "coins", "weight": 1, "value": 10},
                    {"key": "bonus", "weight": 1, "value": 20},
                ],
            },
            None,
            None,
            {},
            "reward_key",
        ),
        (
            "prediction_card",
            {
                "game_type": "prediction_card",
                "cooldown_seconds": 60,
                "choices": ["red", "black"],
                "correct_reward": 25,
            },
            "red",
            None,
            {},
            "authoritative_choice",
        ),
        (
            "skill_check",
            {
                "game_type": "skill_check",
                "cooldown_seconds": 60,
                "duration_seconds": 30,
                "max_score": 1000,
            },
            None,
            [1, 2],
            {"sequence": [1, 2]},
            "score",
        ),
    ],
)
async def test_play_session_dispatches_each_registered_current_game_branch(
    monkeypatch: pytest.MonkeyPatch,
    game_key: str,
    payload: dict[str, object],
    choice: str | None,
    actions: list[int] | None,
    challenge: dict[str, object],
    result_key: str,
) -> None:
    """Capture the three current dispatch paths before they move behind a registry."""

    now = datetime(2026, 1, 1, tzinfo=UTC)
    player_id, game_id, config_id, session_id = (uuid4() for _ in range(4))
    game_session = GameSession(
        id=session_id,
        request_id=uuid4(),
        player_id=player_id,
        game_id=game_id,
        config_version_id=config_id,
        status=SessionStatus.ACTIVE,
        created_at=now - timedelta(seconds=1),
        expires_at=now + timedelta(seconds=30),
        challenge=challenge,
    )
    game = Game(id=game_id, key=game_key, name=game_key, description=game_key)
    config = GameConfigVersion(id=config_id, game_id=game_id, version=1, payload=payload)
    outcome = Outcome(id=uuid4(), session_id=session_id, config_version_id=config_id, result={})
    reward = Reward(id=uuid4(), outcome_id=outcome.id, player_id=player_id, value=0)
    session = AsyncMock()

    async def add_outcome_side_effect(*arguments: object) -> Outcome:
        result = arguments[2]
        assert isinstance(result, dict)
        outcome.result = result
        return outcome

    add_outcome = AsyncMock(side_effect=add_outcome_side_effect)

    monkeypatch.setattr(repositories, "lock_session", AsyncMock(return_value=game_session))
    monkeypatch.setattr(repositories, "get_game_by_id", AsyncMock(return_value=game))
    monkeypatch.setattr(repositories, "get_config_by_id", AsyncMock(return_value=config))
    monkeypatch.setattr(
        repositories, "lock_fairness_proof_by_session", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(repositories, "add_outcome", add_outcome)
    monkeypatch.setattr(repositories, "add_reward", AsyncMock(return_value=reward))
    monkeypatch.setattr(repositories, "add_outcome_audit", AsyncMock())
    monkeypatch.setattr(repositories, "add_game_played_event", AsyncMock())
    monkeypatch.setattr(repositories, "add_reward_evidence", AsyncMock())

    result = await services.play_session(
        session,
        session_id=session_id,
        owner_id=player_id,
        choice=choice,
        actions=actions,
        provider=HmacOutcomeProvider("x" * 32),
        clock=lambda: now,
    )

    assert result is outcome
    call_arguments = add_outcome.await_args
    assert call_arguments is not None
    assert result_key in call_arguments.args[2]
    assert game_session.status is SessionStatus.COMPLETED
    session.commit.assert_awaited_once()
