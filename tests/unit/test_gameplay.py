"""Deterministic cryptographic provider and game-rule coverage."""

from uuid import uuid4

import pytest

from app.rng import HmacOutcomeProvider

pytestmark = pytest.mark.unit


def test_hmac_provider_reproduces_identical_inputs() -> None:
    provider = HmacOutcomeProvider("x" * 32)
    session_id = uuid4()
    config_id = uuid4()
    first = provider.uniform(
        session_id=session_id,
        game_key="daily_spin",
        config_version_id=config_id,
        purpose="outcome",
        upper_bound=100,
    )
    second = provider.uniform(
        session_id=session_id,
        game_key="daily_spin",
        config_version_id=config_id,
        purpose="outcome",
        upper_bound=100,
    )
    assert first == second
    assert 0 <= first.value < 100


def test_hmac_provider_separates_purposes_and_rejects_invalid_bound() -> None:
    provider = HmacOutcomeProvider("y" * 32)
    session_id = uuid4()
    config_id = uuid4()
    first = provider.uniform(
        session_id=session_id,
        game_key="daily_spin",
        config_version_id=config_id,
        purpose="first",
        upper_bound=2**255,
    )
    second = provider.uniform(
        session_id=session_id,
        game_key="daily_spin",
        config_version_id=config_id,
        purpose="second",
        upper_bound=2**255,
    )
    assert first.digest_hex != second.digest_hex
    with pytest.raises(ValueError):
        provider.uniform(
            session_id=session_id,
            game_key="daily_spin",
            config_version_id=config_id,
            purpose="invalid",
            upper_bound=0,
        )


def test_hmac_provider_requires_a_substantial_secret() -> None:
    with pytest.raises(ValueError):
        HmacOutcomeProvider("short")
