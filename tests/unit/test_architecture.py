"""Small executable dependency-boundary checks for the modular monolith."""

import ast
from pathlib import Path

import pytest
from fastapi.routing import APIRoute

from app.api.analytics import router as analytics_router
from app.api.domain import router as domain_router
from app.api.fairness import router as fairness_router
from app.api.gameplay import router as gameplay_router
from app.api.leaderboards import router as leaderboards_router
from app.api.players import router as players_router
from app.api.rewards import router as rewards_router
from app.api.sessions import router as sessions_router

pytestmark = pytest.mark.unit
APP_ROOT = Path("app")


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imported.add(node.module)
    return imported


def test_dependency_direction_has_no_reverse_imports() -> None:
    repository_imports = _imports(APP_ROOT / "repositories.py")
    assert not any(name.startswith(("app.api", "app.services")) for name in repository_imports)

    rules_imports = _imports(APP_ROOT / "game_rules.py")
    assert not any(
        name.startswith(("app.api", "app.database", "app.repositories", "app.services"))
        for name in rules_imports
    )

    for path in (APP_ROOT / "services").glob("*.py"):
        assert not any(name.startswith("app.api") for name in _imports(path)), path


def test_service_package_keeps_each_public_capability_module() -> None:
    expected = {
        "analytics.py",
        "errors.py",
        "fairness.py",
        "gameplay.py",
        "leaderboards.py",
        "players.py",
        "rewards.py",
        "sessions.py",
    }
    assert expected <= {path.name for path in (APP_ROOT / "services").glob("*.py")}
    assert not (APP_ROOT / "services.py").exists()
    assert not (APP_ROOT / "services" / "core.py").exists()


def test_domain_router_is_composition_only_and_registers_every_public_route() -> None:
    tree = ast.parse((APP_ROOT / "api" / "domain.py").read_text(encoding="utf-8"))
    assert not any(isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) for node in tree.body)

    capability_routers = (
        players_router,
        sessions_router,
        gameplay_router,
        rewards_router,
        analytics_router,
        leaderboards_router,
        fairness_router,
    )
    assert len(domain_router.routes) == len(capability_routers)
    registered = {
        (method, route.path)
        for router in capability_routers
        for route in router.routes
        if isinstance(route, APIRoute)
        for method in (route.methods or set())
    }
    assert registered == {
        ("POST", "/players"),
        ("GET", "/players/{player_id}"),
        ("GET", "/games"),
        ("GET", "/games/{game_key}/config"),
        ("POST", "/sessions"),
        ("GET", "/sessions/{session_id}"),
        ("DELETE", "/sessions/{session_id}"),
        ("POST", "/sessions/{session_id}/play"),
        ("POST", "/sessions/{session_id}/claim"),
        ("GET", "/players/{player_id}/rewards"),
        ("GET", "/audit/outcomes/{outcome_id}"),
        ("GET", "/analytics/game-summary"),
        ("POST", "/leaderboards/{game_key}/settle"),
        ("POST", "/scores"),
        ("GET", "/leaderboards/{game_key}"),
        ("GET", "/players/{player_id}/rank"),
        ("POST", "/fairness/commit"),
        ("POST", "/fairness/evaluate"),
        ("GET", "/fairness/outcomes/{outcome_id}/proof"),
        ("GET", "/fairness/outcomes/{outcome_id}/verify"),
    }
