"""Versioned API router boundary."""

from fastapi import APIRouter, Request

from app.config import Settings
from app.schemas import ApiInfoResponse

router = APIRouter(prefix="/api/v1")


@router.get("", response_model=ApiInfoResponse, tags=["meta"])
async def api_info(request: Request) -> ApiInfoResponse:
    """Describe the stable versioned API boundary."""

    settings: Settings = request.app.state.settings
    return ApiInfoResponse(name=settings.app_name, version=settings.app_version)
