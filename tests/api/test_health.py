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

    async def check_schema_revision(self) -> None:
        """Represent a database migrated to the expected application head."""


class OutdatedDatabase(AvailableDatabase):
    """Minimal dependency representing a reachable but outdated schema."""

    async def check_schema_revision(self) -> None:
        raise RuntimeError("outdated")


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


async def test_requests_are_correlated_and_metrics_are_exposed(application: Any) -> None:
    """Safe caller IDs are echoed and low-cardinality request measurements are visible."""

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application), base_url="http://test"
    ) as client:
        response = await client.get("/live", headers={"X-Request-ID": "phase4-check"})
        metrics = await client.get("/metrics")

    assert response.headers["X-Request-ID"] == "phase4-check"
    assert "http_requests_total" in metrics.text
    assert "phase4-check" not in metrics.text


async def test_readiness_rejects_an_outdated_schema(application: Any) -> None:
    """Connectivity alone cannot make an incompatible database report ready."""

    application.dependency_overrides[get_database] = OutdatedDatabase
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application), base_url="http://test"
    ) as client:
        response = await client.get("/ready")

    assert response.status_code == 503
    assert response.json() == {"detail": "required database is unavailable"}
