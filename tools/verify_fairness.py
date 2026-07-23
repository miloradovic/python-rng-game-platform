"""Offline verification of a stored proof derivation and lifecycle event chain."""

import argparse
import asyncio
import json
from collections.abc import Sequence
from uuid import UUID

from app import services
from app.config import get_settings
from app.database import Database


async def verify(outcome_id: UUID, player_id: UUID) -> tuple[bool, str]:
    """Verify one owner-bound outcome from PostgreSQL without calling the API."""

    database = Database(get_settings())
    try:
        async with database.session_factory() as session:
            _, _, code, verified = await services.verify_fairness_proof(
                session, outcome_id=outcome_id, owner_id=player_id
            )
            return verified, code
    finally:
        await database.dispose()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Verify daily-spin proof derivation and its append-only lifecycle chain."
    )
    parser.add_argument("--outcome-id", required=True, type=UUID)
    parser.add_argument("--player-id", required=True, type=UUID)
    arguments = parser.parse_args(argv)
    verified, code = asyncio.run(verify(arguments.outcome_id, arguments.player_id))
    print(json.dumps({"outcome_id": str(arguments.outcome_id), "verified": verified, "code": code}))
    return 0 if verified else 1


if __name__ == "__main__":
    raise SystemExit(main())
