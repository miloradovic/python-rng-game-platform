"""Opt-in service-layer demo scores for every leaderboard-capable game."""

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
from app.game_rules import GameKey
from app.logging import configure_logging
from app.models import SessionStatus
from app.rng import HmacOutcomeProvider, OutcomeProvider
from app.services._common import utc_now
from tools.rebuild_leaderboard import rebuild_leaderboard

logger = logging.getLogger(__name__)
DEMO_NAMESPACE = uuid.UUID("36ad9995-4e72-4e50-81f8-f6d9259ca793")


@dataclass(frozen=True, slots=True)
class AllGameDemoReport:
    period_start: datetime
    sessions_completed: int
    scores_created: int
    projection_rows: dict[str, int]


def demo_player_id(game_key: GameKey) -> uuid.UUID:
    return uuid.uuid5(DEMO_NAMESPACE, f"player:{game_key.value}")


def demo_request_id(game_key: GameKey, period_start: datetime) -> uuid.UUID:
    return uuid.uuid5(DEMO_NAMESPACE, f"session:{game_key.value}:{period_start.isoformat()}")


def demo_public_label(game_key: GameKey) -> str:
    digest = hashlib.sha256(f"all-game-public-label-v1:{game_key.value}".encode()).hexdigest()
    return f"Player-{digest[:12].upper()}"


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--confirm-demo-data", action="store_true", required=True)
    parser.add_argument(
        "--skip-projection",
        action="store_true",
        help="leave Redis untouched; PostgreSQL leaderboard reads still work",
    )
    return parser.parse_args(argv)


async def _complete_game(
    database: Database,
    provider: OutcomeProvider,
    *,
    game_key: GameKey,
    period_start: datetime,
    clock: Callable[[], datetime],
) -> tuple[bool, bool]:
    player_id = demo_player_id(game_key)
    async with database.session_factory() as session:
        await services.provision_player(
            session,
            player_id=player_id,
            display_name=f"Synthetic {game_key.value.replace('_', ' ').title()} Player",
            public_label=demo_public_label(game_key),
        )
    async with database.session_factory() as session:
        game_session = await services.create_session(
            session,
            request_id=demo_request_id(game_key, period_start),
            player_id=player_id,
            owner_id=player_id,
            game_key=game_key.value,
            clock=clock,
        )
    completed = False
    if game_session.status == SessionStatus.ACTIVE:
        if game_key is GameKey.DAILY_SPIN:
            async with database.session_factory() as session:
                proof = await services.commit_fairness(
                    session, session_id=game_session.id, owner_id=player_id, clock=clock
                )
            async with database.session_factory() as session:
                await services.evaluate_fairness(
                    session,
                    proof_id=proof.id,
                    owner_id=player_id,
                    client_seed="all-game-demo-v1",
                    clock=clock,
                )
        else:
            intent = (
                {"choice": "red", "actions": None}
                if game_key is GameKey.PREDICTION_CARD
                else {"choice": None, "actions": list(game_session.challenge["sequence"])}
            )
            async with database.session_factory() as session:
                await services.play_session(
                    session,
                    session_id=game_session.id,
                    owner_id=player_id,
                    provider=provider,
                    clock=clock,
                    **intent,
                )
        completed = True
    async with database.session_factory() as session:
        _, created, submitted_game_key = await services.submit_final_score(
            session, session_id=game_session.id, owner_id=player_id, clock=clock
        )
    if submitted_game_key != game_key.value:
        raise RuntimeError("score submission resolved the wrong game")
    return completed, created


async def seed_all_game_demo_data(
    database: Database,
    provider: OutcomeProvider,
    *,
    client: Redis | None = None,
    clock: Callable[[], datetime] = utc_now,
) -> AllGameDemoReport:
    period_start, _ = services.leaderboard_period(clock())
    sessions_completed = scores_created = 0
    for game_key in GameKey:
        completed, created = await _complete_game(
            database,
            provider,
            game_key=game_key,
            period_start=period_start,
            clock=clock,
        )
        sessions_completed += int(completed)
        scores_created += int(created)
    projection_rows: dict[str, int] = {}
    if client is not None:
        for game_key in GameKey:
            async with database.session_factory() as session:
                projection_rows[game_key.value] = await rebuild_leaderboard(
                    session,
                    client,
                    game_key=game_key.value,
                    period_start=period_start,
                )
    return AllGameDemoReport(
        period_start=period_start,
        sessions_completed=sessions_completed,
        scores_created=scores_created,
        projection_rows=projection_rows,
    )


async def _run(args: argparse.Namespace) -> AllGameDemoReport:
    settings = get_settings()
    if settings.app_env is AppEnvironment.PRODUCTION:
        raise RuntimeError("synthetic demo data is disabled in production")
    database = Database(settings)
    client = None if args.skip_projection else create_redis_client(settings)
    if not args.skip_projection and client is None:
        raise RuntimeError("Redis is not configured; use --skip-projection explicitly")
    provider = HmacOutcomeProvider(settings.outcome_hmac_secret.get_secret_value())
    try:
        return await seed_all_game_demo_data(database, provider, client=client)
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
        "all_game_demo_seed_complete sessions_completed=%s scores_created=%s "
        "projection_rows=%s period_start=%s",
        report.sessions_completed,
        report.scores_created,
        report.projection_rows,
        report.period_start.isoformat(),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
