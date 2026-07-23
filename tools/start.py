"""Upgrade the authoritative schema before starting the single ASGI process."""

import os
import subprocess

from app.config import AppEnvironment, get_settings


def main() -> None:
    """Run deterministic migrations, then replace this process with Uvicorn."""

    settings = get_settings()
    subprocess.run(["alembic", "upgrade", "head"], check=True)  # noqa: S607
    command = [
        "uvicorn",
        "app.main:app",
        "--host",
        "0.0.0.0",  # noqa: S104 - the container port is explicitly bound by Compose
        "--port",
        "8000",
        "--no-access-log",
    ]
    if settings.app_env is AppEnvironment.DEVELOPMENT:
        command.extend(["--reload", "--reload-dir", "/workspace/app"])
    os.execvp(command[0], command)  # noqa: S606


if __name__ == "__main__":
    main()
