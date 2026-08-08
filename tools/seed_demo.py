"""Opt-in seeding of synthetic Skill Check players and genuine completed records."""

import argparse
import asyncio
import hashlib
import logging
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime

from redis.asyncio import Redis

from app import services
from app.cache import create_redis_client
from app.config import AppEnvironment, get_settings
from app.database import Database
from app.logging import configure_logging
from app.models import RewardStatus, SessionStatus
from app.rng import HmacOutcomeProvider, OutcomeProvider
from app.services._common import utc_now
from tools.rebuild_leaderboard import rebuild_leaderboard

logger = logging.getLogger(__name__)

DEMO_NAMESPACE = uuid.UUID("b09c564a-89b1-4dc5-9981-ec4aba234070")
DEFAULT_PLAYER_COUNT = 8
MAX_PLAYER_COUNT = 10
CORRECT_ACTION_COUNTS = (5, 4, 4, 3, 3, 2, 1, 0, 5, 2)


@dataclass(frozen=True, slots=True)
class DemoSeedReport:
    """Non-sensitive summary of one optional demo-data run."""

    period_start: datetime
    players_requested: int
    players_created: int
    sessions_completed: int
    scores_created: int
    rewards_claimed: int
    projection_rows: int | None


def _player_count(value: str) -> int:
    try:
        count = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("players must be an integer") from error
    if not 1 <= count <= MAX_PLAYER_COUNT:
        raise argparse.ArgumentTypeError(f"players must be between 1 and {MAX_PLAYER_COUNT}")
    return count


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse the deliberately explicit demo-data command options."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--confirm-demo-data",
        action="store_true",
        required=True,
        help="confirm that clearly synthetic presentation records should be created",
    )
    parser.add_argument(
        "--players",
        type=_player_count,
        default=DEFAULT_PLAYER_COUNT,
        help=f"number of synthetic players (1-{MAX_PLAYER_COUNT})",
    )
    parser.add_argument(
        "--skip-projection",
        action="store_true",
        help="leave Redis untouched; PostgreSQL leaderboard reads still work",
    )
    return parser.parse_args(argv)


def demo_player_id(index: int) -> uuid.UUID:
    """Return the stable identity for one synthetic persona."""

    return uuid.uuid5(DEMO_NAMESPACE, f"synthetic-player:{index}")


def demo_public_label(index: int) -> str:
    """Return a stable safe label without exposing the synthetic display name."""

    digest = hashlib.sha256(f"synthetic-public-label-v1:{index}".encode()).hexdigest()
    return f"Player-{digest[:12].upper()}"


def demo_request_id(index: int, period_start: datetime) -> uuid.UUID:
    """Bind an idempotent session intent to one persona and UTC leaderboard week."""

    return uuid.uuid5(
        DEMO_NAMESPACE,
        f"skill-check-session:{index}:{period_start.isoformat()}",
    )


def _actions_for_score(challenge: object, correct_actions: int) -> list[int]:
    if (
        not isinstance(challenge, list)
        or len(challenge) != 5
        or any(not isinstance(value, int) for value in challenge)
    ):
        raise RuntimeError("seeded Skill Check session has an invalid server challenge")
    if correct_actions == len(challenge):
        return list(challenge)
    prefix = list(challenge[:correct_actions])
    wrong = next(
        value for value in range(10) if value not in prefix and value != challenge[correct_actions]
    )
    return [*prefix, wrong]


async def seed_demo_data(
    database: Database,
    provider: OutcomeProvider,
    *,
    player_count: int = DEFAULT_PLAYER_COUNT,
    client: Redis | None = None,
    clock: Callable[[], datetime] = utc_now,
) -> DemoSeedReport:
    """Create or resume real service-layer Skill Check flows for synthetic players."""

    if not 1 <= player_count <= MAX_PLAYER_COUNT:
        raise ValueError(f"player_count must be between 1 and {MAX_PLAYER_COUNT}")
    period_start, _ = services.leaderboard_period(clock())
    players_created = sessions_completed = scores_created = rewards_claimed = 0

    for index in range(1, player_count + 1):
        player_id = demo_player_id(index)
        async with database.session_factory() as session:
            _, created = await services.provision_player(
                session,
                player_id=player_id,
                display_name=f"Synthetic Demo Player {index:02d}",
                public_label=demo_public_label(index),
            )
            players_created += int(created)

        async with database.session_factory() as session:
            game_session = await services.create_session(
                session,
                request_id=demo_request_id(index, period_start),
                player_id=player_id,
                owner_id=player_id,
                game_key="skill_check",
                clock=clock,
            )

        if game_session.status == SessionStatus.ACTIVE:
            actions = _actions_for_score(
                game_session.challenge.get("sequence"), CORRECT_ACTION_COUNTS[index - 1]
            )
            async with database.session_factory() as session:
                await services.play_session(
                    session,
                    session_id=game_session.id,
                    owner_id=player_id,
                    choice=None,
                    actions=actions,
                    provider=provider,
                    clock=clock,
                )
            sessions_completed += 1

        async with database.session_factory() as session:
            score, created = await services.submit_final_score(
                session,
                session_id=game_session.id,
                owner_id=player_id,
                clock=clock,
            )
            scores_created += int(created)
            if score.period_start != period_start:
                raise RuntimeError("demo score was not recorded in the requested UTC week")

        async with database.session_factory() as session:
            state = await services.retrieve_player_game_state(
                session,
                player_id=player_id,
                owner_id=player_id,
                game_key="skill_check",
                clock=clock,
            )
            already_claimed = (
                state.reward is not None and state.reward.status == RewardStatus.CLAIMED
            )

        async with database.session_factory() as session:
            reward = await services.claim_session_reward(
                session,
                session_id=game_session.id,
                owner_id=player_id,
                clock=clock,
            )
            rewards_claimed += int(not already_claimed and reward.claimed_at is not None)

    projection_rows = None
    if client is not None:
        async with database.session_factory() as session:
            projection_rows = await rebuild_leaderboard(
                session,
                client,
                game_key="skill_check",
                period_start=period_start,
            )
    return DemoSeedReport(
        period_start=period_start,
        players_requested=player_count,
        players_created=players_created,
        sessions_completed=sessions_completed,
        scores_created=scores_created,
        rewards_claimed=rewards_claimed,
        projection_rows=projection_rows,
    )


async def _run(args: argparse.Namespace) -> DemoSeedReport:
    settings = get_settings()
    if settings.app_env is AppEnvironment.PRODUCTION:
        raise RuntimeError("synthetic demo data is disabled in production")
    database = Database(settings)
    client = None if args.skip_projection else create_redis_client(settings)
    if not args.skip_projection and client is None:
        raise RuntimeError("Redis is not configured; use --skip-projection explicitly")
    provider = HmacOutcomeProvider(settings.outcome_hmac_secret.get_secret_value())
    try:
        return await seed_demo_data(
            database,
            provider,
            player_count=args.players,
            client=client,
        )
    finally:
        if client is not None:
            await client.aclose()
        await database.dispose()


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    settings = get_settings()
    configure_logging(settings.log_level)
    report = asyncio.run(_run(args))
    logger.info(
        "demo_seed_complete synthetic_players=%s players_created=%s sessions_completed=%s "
        "scores_created=%s rewards_claimed=%s projection_rows=%s period_start=%s",
        report.players_requested,
        report.players_created,
        report.sessions_completed,
        report.scores_created,
        report.rewards_claimed,
        report.projection_rows,
        report.period_start.isoformat(),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
