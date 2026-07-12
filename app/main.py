"""FastAPI application factory and process entry point."""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse

from app.api.health import router as health_router
from app.api.router import router as api_router
from app.cache import create_redis_client, redis_is_available
from app.config import Settings, get_settings
from app.database import Database
from app.logging import configure_logging
from app.rng import HmacOutcomeProvider
from app.services import (
    ActiveSessionError,
    CooldownError,
    DomainError,
    ForbiddenError,
    InactiveGameError,
    InactivePlayerError,
    InvalidPlayError,
    InvalidTransitionError,
    NotFoundError,
    SessionExpiredError,
)

logger = logging.getLogger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    """Create one configured FastAPI application instance."""

    resolved_settings = settings or get_settings()
    configure_logging(resolved_settings.log_level)

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        database = Database(resolved_settings)
        redis_client = create_redis_client(resolved_settings)
        application.state.database = database
        application.state.redis = redis_client

        await database.check_connection()
        logger.info(
            "application_started environment=%s redis_available=%s",
            resolved_settings.app_env,
            await redis_is_available(redis_client),
        )
        try:
            yield
        finally:
            if redis_client is not None:
                await redis_client.aclose()
            await database.dispose()
            logger.info("application_stopped")

    application = FastAPI(
        title=resolved_settings.app_name,
        version=resolved_settings.app_version,
        description="Server-authoritative free-to-play game platform.",
        lifespan=lifespan,
    )
    application.state.settings = resolved_settings
    application.state.outcome_provider = HmacOutcomeProvider(
        resolved_settings.outcome_hmac_secret.get_secret_value()
    )

    @application.exception_handler(DomainError)
    async def domain_error_handler(request: Request, error: DomainError) -> JSONResponse:
        del request
        status_code = {
            NotFoundError: status.HTTP_404_NOT_FOUND,
            ForbiddenError: status.HTTP_403_FORBIDDEN,
            InactiveGameError: status.HTTP_409_CONFLICT,
            InactivePlayerError: status.HTTP_409_CONFLICT,
            CooldownError: status.HTTP_409_CONFLICT,
            ActiveSessionError: status.HTTP_409_CONFLICT,
            InvalidTransitionError: status.HTTP_409_CONFLICT,
            SessionExpiredError: status.HTTP_409_CONFLICT,
            InvalidPlayError: status.HTTP_422_UNPROCESSABLE_CONTENT,
        }.get(type(error), status.HTTP_400_BAD_REQUEST)
        return JSONResponse(status_code=status_code, content={"error": {"code": error.code}})

    application.include_router(health_router)
    application.include_router(api_router)
    return application


app = create_app()
