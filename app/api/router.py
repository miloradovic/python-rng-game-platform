"""Versioned API router boundary."""

from typing import Annotated

from fastapi import APIRouter, Depends

from app.api.dependencies import get_app_settings
from app.api.domain import router as domain_router
from app.config import Settings
from app.schemas import ApiInfoResponse

router = APIRouter(prefix="/api/v1")
router.include_router(domain_router)


@router.get("", response_model=ApiInfoResponse, tags=["meta"])
async def api_info(
    settings: Annotated[Settings, Depends(get_app_settings)],
) -> ApiInfoResponse:
    """Describe the stable versioned API boundary."""

    return ApiInfoResponse(name=settings.app_name, version=settings.app_version)
