"""Deterministic domain contract tests."""

from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.schemas import PlayerCreate, game_config_adapter
from app.services import ForbiddenError, retrieve_player

pytestmark = pytest.mark.unit


def test_daily_spin_config_rejects_invalid_reward_weight() -> None:
    with pytest.raises(ValidationError):
        game_config_adapter.validate_python(
            {
                "game_type": "daily_spin",
                "cooldown_seconds": 86400,
                "rewards": [
                    {"key": "loss", "weight": 0, "value": 0},
                    {"key": "win", "weight": 1, "value": 10},
                ],
            }
        )


def test_player_input_forbids_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        PlayerCreate.model_validate({"display_name": "Demo", "status": "active"})


async def test_player_ownership_is_checked_before_database_lookup() -> None:
    with pytest.raises(ForbiddenError):
        await retrieve_player(object(), uuid4(), uuid4())  # type: ignore[arg-type]
