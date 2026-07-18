"""Fixed-vector, boundary, and tamper coverage for provably-fair protocol v1."""

import secrets
from dataclasses import replace
from uuid import UUID, uuid4

import pytest

from app.rng import (
    ALGORITHM_HMAC_SHA256,
    DAILY_SPIN_MAPPING_VERSION_V1,
    PROTOCOL_VERSION_V1,
    DailySpinDerivation,
    DailySpinProof,
    InvalidFairnessNonceError,
    InvalidFairnessSeedError,
    InvalidRewardMappingError,
    RewardBand,
    UnsupportedFairnessVersionError,
    create_server_seed,
    daily_spin_mapping_digest,
    derive_daily_spin,
    select_daily_spin_reward,
    server_seed_commitment,
    verify_daily_spin_proof,
)

pytestmark = pytest.mark.unit

SERVER_SEED = bytes.fromhex("000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f")
CONFIG_VERSION_ID = UUID("11111111-2222-3333-4444-555555555555")
SESSION_ID = UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
CLIENT_SEED = "client-seed-2026"
REWARD_BANDS = (
    RewardBand("bronze", 25, 5),
    RewardBand("silver", 25, 10),
    RewardBand("gold", 50, 20),
)


def _derive(nonce: int = 0) -> DailySpinDerivation:
    return derive_daily_spin(
        server_seed=SERVER_SEED,
        client_seed=CLIENT_SEED,
        nonce=nonce,
        config_version_id=CONFIG_VERSION_ID,
        session_id=SESSION_ID,
        reward_bands=REWARD_BANDS,
    )


def _proof() -> DailySpinProof:
    result = _derive()
    return DailySpinProof(
        protocol_version=PROTOCOL_VERSION_V1,
        algorithm=ALGORITHM_HMAC_SHA256,
        server_seed_commitment=server_seed_commitment(SERVER_SEED),
        server_seed_hex=SERVER_SEED.hex(),
        client_seed=CLIENT_SEED,
        nonce=0,
        game_key="daily_spin",
        config_version_id=CONFIG_VERSION_ID,
        session_id=SESSION_ID,
        reward_bands=REWARD_BANDS,
        mapping_version=DAILY_SPIN_MAPPING_VERSION_V1,
        mapping_digest=result.mapping_digest,
        raw_digest_hex=result.raw_digest_hex,
        normalized_value=result.normalized_value,
        attempt=result.attempt,
        reward_key=result.reward.key,
        reward_value=result.reward.value,
    )


def test_v1_fixed_vectors_match_the_frozen_protocol() -> None:
    first = _derive(0)
    second = _derive(1)

    assert server_seed_commitment(SERVER_SEED) == (
        "630dcd2966c4336691125448bbb25b4ff412a49c732db2c8abc1b8581bd710dd"
    )
    assert daily_spin_mapping_digest(REWARD_BANDS) == (
        "de89890976a89daa9841f5e49380ab41df7d732fb46ba249890db3fd8fc58281"
    )
    assert (first.attempt, first.raw_digest_hex, first.raw_value, first.normalized_value) == (
        0,
        "d8ced2843f99a8c7cc8242c202b92a8dd421f168ac27683f909e74afee4f7952",
        98064998721473559286997853775141885387818788982483429880663541945029838731602,
        2,
    )
    assert first.reward == RewardBand("bronze", 25, 5)
    assert (second.attempt, second.raw_digest_hex, second.raw_value, second.normalized_value) == (
        0,
        "0973aae57d93d58193a5ca7f6230a7628fa55c6b36296d33064c5a5f7e7d6994",
        4275182533630504708823436366365313245256971365063390938131914831072772057492,
        92,
    )
    assert second.reward == RewardBand("gold", 50, 20)


def test_complete_inputs_are_reproducible_and_nonce_sensitive() -> None:
    assert _derive() == _derive()
    assert _derive().raw_digest_hex != _derive(1).raw_digest_hex


@pytest.mark.parametrize(
    ("normalized_value", "reward_key"),
    [(0, "bronze"), (24, "bronze"), (25, "silver"), (49, "silver"), (50, "gold"), (99, "gold")],
)
def test_reward_bucket_boundaries_are_exact(normalized_value: int, reward_key: str) -> None:
    assert select_daily_spin_reward(REWARD_BANDS, normalized_value).key == reward_key


def test_normalization_is_in_range_for_many_complete_inputs() -> None:
    for nonce in range(128):
        result = derive_daily_spin(
            server_seed=SERVER_SEED,
            client_seed=f"seed-{nonce}",
            nonce=nonce,
            config_version_id=CONFIG_VERSION_ID,
            session_id=SESSION_ID,
            reward_bands=REWARD_BANDS,
        )
        assert 0 <= result.normalized_value < 100
        assert result.reward == select_daily_spin_reward(REWARD_BANDS, result.normalized_value)


@pytest.mark.parametrize("client_seed", ["", "has space", "non-ascii-é", "x" * 65])
def test_invalid_client_seeds_are_rejected(client_seed: str) -> None:
    with pytest.raises(InvalidFairnessSeedError):
        _ = derive_daily_spin(
            server_seed=SERVER_SEED,
            client_seed=client_seed,
            nonce=0,
            config_version_id=CONFIG_VERSION_ID,
            session_id=SESSION_ID,
            reward_bands=REWARD_BANDS,
        )


@pytest.mark.parametrize("nonce", [-1, 1 << 64, True])
def test_invalid_nonces_and_maps_are_rejected(nonce: int) -> None:
    with pytest.raises(InvalidFairnessNonceError):
        _ = _derive(nonce)
    with pytest.raises(InvalidRewardMappingError):
        _ = select_daily_spin_reward(
            (RewardBand("duplicate", 1, 0), RewardBand("duplicate", 1, 0)), 0
        )


def test_unsupported_versions_and_secure_seed_generation_are_explicit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(UnsupportedFairnessVersionError):
        _ = derive_daily_spin(
            server_seed=SERVER_SEED,
            client_seed=CLIENT_SEED,
            nonce=0,
            config_version_id=CONFIG_VERSION_ID,
            session_id=SESSION_ID,
            reward_bands=REWARD_BANDS,
            protocol_version="rng-game-platform-pvf-v2",
        )
    calls: list[int] = []

    def token_bytes(size: int) -> bytes:
        calls.append(size)
        return b"s" * size

    monkeypatch.setattr(secrets, "token_bytes", token_bytes)
    assert create_server_seed() == b"s" * 32
    assert calls == [32]


def test_independent_verification_recalculates_and_detects_material_mutations() -> None:
    proof = _proof()
    assert verify_daily_spin_proof(proof).verified
    mutations = (
        (replace(proof, server_seed_hex="ff" * 32), "commitment_mismatch"),
        (replace(proof, server_seed_commitment="0" * 64), "commitment_mismatch"),
        (replace(proof, client_seed="other-client-seed"), "raw_digest_mismatch"),
        (replace(proof, nonce=1), "raw_digest_mismatch"),
        (replace(proof, config_version_id=uuid4()), "raw_digest_mismatch"),
        (replace(proof, game_key="other_game"), "unsupported_fairness_version"),
        (replace(proof, mapping_version="other-map"), "unsupported_fairness_version"),
        (replace(proof, mapping_digest="0" * 64), "mapping_digest_mismatch"),
        (replace(proof, raw_digest_hex="0" * 64), "raw_digest_mismatch"),
        (replace(proof, normalized_value=99), "normalized_value_mismatch"),
        (replace(proof, reward_key="silver"), "reward_mismatch"),
        (
            replace(proof, reward_bands=(RewardBand("bronze", 26, 5), *proof.reward_bands[1:])),
            "mapping_digest_mismatch",
        ),
    )
    for tampered, expected_code in mutations:
        result = verify_daily_spin_proof(tampered)
        assert not result.verified
        assert result.code == expected_code


@pytest.mark.parametrize("normalized_value", [-1, 100, True])
def test_out_of_range_normalized_values_are_rejected(normalized_value: int) -> None:
    with pytest.raises(InvalidRewardMappingError):
        _ = select_daily_spin_reward(REWARD_BANDS, normalized_value)


def test_malformed_server_seed_is_rejected() -> None:
    with pytest.raises(InvalidFairnessSeedError):
        _ = derive_daily_spin(
            server_seed=b"s" * 31,
            client_seed=CLIENT_SEED,
            nonce=0,
            config_version_id=CONFIG_VERSION_ID,
            session_id=SESSION_ID,
            reward_bands=REWARD_BANDS,
        )


def test_verification_uses_all_remaining_protocol_fields() -> None:
    proof = _proof()
    mutations = (
        (replace(proof, session_id=uuid4()), "raw_digest_mismatch"),
        (replace(proof, algorithm="other-algorithm"), "unsupported_fairness_version"),
        (replace(proof, attempt=1), "attempt_mismatch"),
        (replace(proof, reward_value=10), "reward_mismatch"),
    )
    for tampered, expected_code in mutations:
        result = verify_daily_spin_proof(tampered)
        assert not result.verified
        assert result.code == expected_code
