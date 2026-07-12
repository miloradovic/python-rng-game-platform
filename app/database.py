"""Asynchronous PostgreSQL engine and request-session ownership."""

from collections.abc import AsyncIterator

from fastapi import Request
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncAttrs,
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from app.config import Settings


class Base(AsyncAttrs, DeclarativeBase):
    """Declarative base for all durable SQLAlchemy models."""


class Database:
    """Own the process engine and async session factory."""

    def __init__(self, settings: Settings) -> None:
        self.engine: AsyncEngine = create_async_engine(
            settings.database_url.get_secret_value(),
            pool_pre_ping=True,
            connect_args={"timeout": settings.database_connect_timeout_seconds},
        )
        self.session_factory = async_sessionmaker(
            bind=self.engine,
            class_=AsyncSession,
            autoflush=False,
            expire_on_commit=False,
        )

    async def check_connection(self) -> None:
        """Verify PostgreSQL readiness without mutating application data."""

        async with self.engine.connect() as connection:
            await connection.execute(text("SELECT 1"))

    async def dispose(self) -> None:
        """Close all pooled database connections during process shutdown."""

        await self.engine.dispose()


def get_database(request: Request) -> Database:
    """Return the application-scoped database owner."""

    database: Database = request.app.state.database
    return database


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    """Yield one request-scoped session without implicitly committing.

    Service/use-case functions own commits. Repositories may flush but must not
    commit, preserving visible transaction boundaries for idempotent operations.
    """

    database = get_database(request)
    async with database.session_factory() as session:
        try:
            yield session
        except BaseException:
            await session.rollback()
            raise
