"""Small executable dependency-boundary checks for the modular monolith."""

import ast
from pathlib import Path

import pytest

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
