"""Unversioned liveness and readiness HTTP endpoints."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.exc import SQLAlchemyError

from app.cache import redis_is_available
from app.database import Database, get_database
from app.schemas import LivenessResponse, ReadinessResponse, ServiceAvailability

router = APIRouter(tags=["health"])


@router.get("/live", response_model=LivenessResponse)
async def liveness() -> LivenessResponse:
    """Report that the ASGI process can serve requests."""

    return LivenessResponse()


@router.get(
    "/ready",
    response_model=ReadinessResponse,
    responses={status.HTTP_503_SERVICE_UNAVAILABLE: {"description": "PostgreSQL unavailable"}},
)
async def readiness(
    request: Request,
    database: Annotated[Database, Depends(get_database)],
) -> ReadinessResponse:
    """Require PostgreSQL and report optional Redis availability."""

    try:
        await database.check_connection()
    except SQLAlchemyError as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="required database is unavailable",
        ) from error

    redis_available = await redis_is_available(request.app.state.redis)
    if redis_available is None:
        redis_status = ServiceAvailability.NOT_CONFIGURED
    elif redis_available:
        redis_status = ServiceAvailability.AVAILABLE
    else:
        redis_status = ServiceAvailability.UNAVAILABLE

    return ReadinessResponse(
        database=ServiceAvailability.AVAILABLE,
        redis=redis_status,
    )
