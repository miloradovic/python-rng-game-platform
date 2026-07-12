"""Explicit, repeatable seed-data command."""

import asyncio
import logging

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import Database
from app.logging import configure_logging

logger = logging.getLogger(__name__)


async def seed_foundation(session: AsyncSession) -> None:
    """Seed foundation data."""

    del session


async def main() -> None:
    """Run seed operations in one explicit transaction."""

    settings = get_settings()
    configure_logging(settings.log_level)
    database = Database(settings)
    try:
        async with database.session_factory.begin() as session:
            await seed_foundation(session)
        logger.info("seed_complete product_rows=0")
    finally:
        await database.dispose()


if __name__ == "__main__":
    asyncio.run(main())
