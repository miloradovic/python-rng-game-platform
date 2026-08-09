"""Product catalogue declaration tests."""

import pytest

from app.schemas import game_config_adapter
from tools.seed import CATALOGUE

pytestmark = pytest.mark.unit


def test_catalogue_declares_two_valid_versions_with_planned_cooldowns() -> None:
    declared = {(entry.game_key, entry.version): entry for entry in CATALOGUE}

    assert len(declared) == len(CATALOGUE) == 6
    assert {key: entry.payload["cooldown_seconds"] for key, entry in declared.items()} == {
        ("daily_spin", 1): 86400,
        ("daily_spin", 2): 3600,
        ("prediction_card", 1): 300,
        ("prediction_card", 2): 60,
        ("skill_check", 1): 60,
        ("skill_check", 2): 30,
    }
    for entry in CATALOGUE:
        validated = game_config_adapter.validate_python(entry.payload)
        assert validated.game_type == entry.game_key


def test_version_two_changes_only_cooldown() -> None:
    by_game_and_version = {(entry.game_key, entry.version): entry for entry in CATALOGUE}

    for game_key in ("daily_spin", "prediction_card", "skill_check"):
        version_one = dict(by_game_and_version[(game_key, 1)].payload)
        version_two = dict(by_game_and_version[(game_key, 2)].payload)
        del version_one["cooldown_seconds"]
        del version_two["cooldown_seconds"]
        assert version_two == version_one
