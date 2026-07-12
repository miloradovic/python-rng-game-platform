"""In-process API contract tests that do not need infrastructure."""

from typing import Any

import httpx
import pytest

from app.config import Settings
from app.database import get_database
from app.main import create_app

pytestmark = pytest.mark.api


class AvailableDatabase:
    """Minimal readiness dependency with a successful connection check."""

    async def check_connection(self) -> None:
        """Represent an available required database."""


@pytest.fixture
def application(settings: Settings) -> Any:
    """Create an app without entering infrastructure-owning lifespan hooks."""

    app = create_app(settings)
    app.dependency_overrides[get_database] = AvailableDatabase
    app.state.redis = None
    return app


async def test_liveness_is_dependency_independent(application: Any) -> None:
    """Liveness reports process health without probing infrastructure."""

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application),
        base_url="http://test",
    ) as client:
        response = await client.get("/live")

    assert response.status_code == 200
    assert response.json() == {"status": "alive"}


async def test_readiness_reports_optional_redis_not_configured(application: Any) -> None:
    """PostgreSQL can make the app ready while optional Redis is absent."""

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application),
        base_url="http://test",
    ) as client:
        response = await client.get("/ready")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ready",
        "database": "available",
        "redis": "not_configured",
    }


async def test_versioned_api_boundary(application: Any) -> None:
    """The initial API metadata is served below the stable v1 prefix."""

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application),
        base_url="http://test",
    ) as client:
        response = await client.get("/api/v1")

    assert response.status_code == 200
    assert response.json()["version"] == "0.1.0"
