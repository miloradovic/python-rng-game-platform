"""Explicit, repeatable product catalogue seed command."""

import asyncio
import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import Database
from app.game_rules import GameKey
from app.logging import configure_logging
from app.models import ConfigStatus, Game, GameConfigVersion
from app.schemas import game_config_adapter

logger = logging.getLogger(__name__)

CATALOGUE: tuple[tuple[str, str, str, dict[str, Any]], ...] = (
    (
        GameKey.DAILY_SPIN.value,
        "Daily Spin",
        "A once-daily weighted reward spin.",
        {
            "game_type": GameKey.DAILY_SPIN.value,
            "cooldown_seconds": 86400,
            "rewards": [
                {"key": "coins_10", "weight": 80, "value": 10},
                {"key": "coins_50", "weight": 20, "value": 50},
            ],
        },
    ),
    (
        GameKey.PREDICTION_CARD.value,
        "Prediction Card",
        "Predict the server-owned card result.",
        {
            "game_type": GameKey.PREDICTION_CARD.value,
            "cooldown_seconds": 300,
            "choices": ["red", "black"],
            "correct_reward": 25,
        },
    ),
    (
        GameKey.SKILL_CHECK.value,
        "Skill Check",
        "Submit input evaluated against server-owned timing rules.",
        {
            "game_type": GameKey.SKILL_CHECK.value,
            "cooldown_seconds": 60,
            "duration_seconds": 30,
            "max_score": 1000,
        },
    ),
)


async def seed_catalogue(session: AsyncSession) -> int:
    created = 0
    for key, name, description, payload in CATALOGUE:
        validated = game_config_adapter.validate_python(payload).model_dump(mode="json")
        game = await session.scalar(select(Game).where(Game.key == key))
        if game is None:
            game = Game(id=uuid.uuid4(), key=key, name=name, description=description)
            session.add(game)
            await session.flush()
            created += 1
        config = await session.scalar(
            select(GameConfigVersion).where(
                GameConfigVersion.game_id == game.id, GameConfigVersion.version == 1
            )
        )
        if config is None:
            session.add(
                GameConfigVersion(
                    id=uuid.uuid4(),
                    game_id=game.id,
                    version=1,
                    status=ConfigStatus.PUBLISHED,
                    payload=validated,
                    published_at=datetime.now(UTC),
                )
            )
            created += 1
    return created


async def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    database = Database(settings)
    try:
        async with database.session_factory.begin() as session:
            rows = await seed_catalogue(session)
        logger.info("seed_complete product_rows=%s", rows)
    finally:
        await database.dispose()


if __name__ == "__main__":
    asyncio.run(main())
