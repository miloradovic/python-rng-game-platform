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


def _service_import_graph(root: Path) -> dict[str, set[str]]:
    paths = tuple(path for path in root.glob("*.py") if path.name != "__init__.py")
    known = {f"app.services.{path.stem}" for path in paths}
    graph: dict[str, set[str]] = {}
    for path in paths:
        module_name = f"app.services.{path.stem}"
        dependencies: set[str] = set()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            candidates: set[str] = set()
            if isinstance(node, ast.Import):
                candidates.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                if node.level == 1:
                    if node.module is None:
                        candidates.update(f"app.services.{alias.name}" for alias in node.names)
                    else:
                        candidates.add(f"app.services.{node.module}")
                elif node.module == "app.services":
                    candidates.update(f"app.services.{alias.name}" for alias in node.names)
                elif node.module is not None:
                    candidates.add(node.module)
            for candidate in candidates:
                dependency = next(
                    (
                        known_module
                        for known_module in known
                        if candidate == known_module or candidate.startswith(f"{known_module}.")
                    ),
                    None,
                )
                if dependency is not None:
                    dependencies.add(dependency)
        graph[module_name] = dependencies
    return graph


def _find_import_cycle(graph: dict[str, set[str]]) -> tuple[str, ...] | None:
    visited: set[str] = set()
    active: list[str] = []
    active_indexes: dict[str, int] = {}

    def visit(module: str) -> tuple[str, ...] | None:
        if module in active_indexes:
            start = active_indexes[module]
            return (*active[start:], module)
        if module in visited:
            return None
        active_indexes[module] = len(active)
        active.append(module)
        for dependency in sorted(graph[module]):
            cycle = visit(dependency)
            if cycle is not None:
                return cycle
        active.pop()
        active_indexes.pop(module)
        visited.add(module)
        return None

    for module in sorted(graph):
        cycle = visit(module)
        if cycle is not None:
            return cycle
    return None


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

    transition_imports = _imports(APP_ROOT / "session_transitions.py")
    assert not any(
        name.startswith(
            ("app.api", "app.database", "app.repositories", "app.services", "app.cache")
        )
        for name in transition_imports
    )


def test_service_module_import_graph_is_acyclic() -> None:
    graph = _service_import_graph(APP_ROOT / "services")
    assert _find_import_cycle(graph) is None


def test_service_import_graph_detects_direct_cycle(tmp_path: Path) -> None:
    (tmp_path / "alpha.py").write_text("from app.services import beta\n", encoding="utf-8")
    (tmp_path / "beta.py").write_text("from app.services import alpha\n", encoding="utf-8")

    graph = _service_import_graph(tmp_path)

    assert _find_import_cycle(graph) == (
        "app.services.alpha",
        "app.services.beta",
        "app.services.alpha",
    )


def test_service_import_graph_detects_indirect_cycle(tmp_path: Path) -> None:
    (tmp_path / "alpha.py").write_text("import app.services.beta\n", encoding="utf-8")
    (tmp_path / "beta.py").write_text(
        "from app.services.gamma import capability\n", encoding="utf-8"
    )
    (tmp_path / "gamma.py").write_text("from . import alpha\n", encoding="utf-8")

    graph = _service_import_graph(tmp_path)

    assert _find_import_cycle(graph) == (
        "app.services.alpha",
        "app.services.beta",
        "app.services.gamma",
        "app.services.alpha",
    )


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
