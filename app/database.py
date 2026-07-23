"""Asynchronous PostgreSQL engine and request-session ownership."""

from collections.abc import AsyncIterator

from fastapi import Request
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import (
    AsyncAttrs,
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from app.config import Settings
from app.observability import MetricsRegistry

EXPECTED_ALEMBIC_REVISION = "0013_leaderboard_projection"


class Base(AsyncAttrs, DeclarativeBase):
    """Declarative base for all durable SQLAlchemy models."""


class Database:
    """Own the process engine and async session factory."""

    def __init__(self, settings: Settings, metrics: MetricsRegistry | None = None) -> None:
        self.engine: AsyncEngine = create_async_engine(
            settings.database_url.get_secret_value(),
            pool_pre_ping=True,
            pool_size=settings.database_pool_size,
            max_overflow=settings.database_max_overflow,
            pool_timeout=settings.database_pool_timeout_seconds,
            connect_args={
                "timeout": settings.database_connect_timeout_seconds,
                "server_settings": {
                    "statement_timeout": str(settings.database_statement_timeout_ms),
                    "lock_timeout": str(settings.database_lock_timeout_ms),
                },
            },
        )
        self.session_factory = async_sessionmaker(
            bind=self.engine,
            class_=AsyncSession,
            autoflush=False,
            expire_on_commit=False,
        )
        if metrics is not None:
            event.listen(
                self.engine.sync_engine,
                "before_cursor_execute",
                lambda *args: metrics.increment("database_operations_total"),
            )

    async def check_connection(self) -> None:
        """Verify PostgreSQL readiness without mutating application data."""

        async with self.engine.connect() as connection:
            await connection.execute(text("SELECT 1"))

    async def check_schema_revision(self) -> None:
        """Reject readiness when the database is not at the application schema head."""

        async with self.engine.connect() as connection:
            revision = await connection.scalar(text("SELECT version_num FROM alembic_version"))
        if revision != EXPECTED_ALEMBIC_REVISION:
            raise RuntimeError("database schema revision does not match application")

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
