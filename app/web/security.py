"""Security and caching headers for repository-owned player web surfaces."""

from collections.abc import Awaitable, Callable

from fastapi import FastAPI, Request, Response

_CONTENT_SECURITY_POLICY = "; ".join(
    (
        "default-src 'self'",
        "base-uri 'none'",
        "connect-src 'self'",
        "font-src 'self'",
        "form-action 'self'",
        "frame-ancestors 'none'",
        "img-src 'self' data:",
        "manifest-src 'self'",
        "media-src 'self'",
        "object-src 'none'",
        "script-src 'self'",
        "style-src 'self'",
        "worker-src 'none'",
    )
)


def _is_player_surface(path: str) -> bool:
    return path == "/" or path == "/leaderboard" or path.startswith(("/games/", "/static/"))


def install_web_security(application: FastAPI) -> None:
    """Apply browser defenses without breaking FastAPI's external-asset API docs."""

    @application.middleware("http")
    async def web_security_headers(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = (
            "camera=(), geolocation=(), microphone=(), payment=(), usb=()"
        )
        response.headers["Cross-Origin-Opener-Policy"] = "same-origin"

        if _is_player_surface(request.url.path):
            response.headers["Content-Security-Policy"] = _CONTENT_SECURITY_POLICY
        if request.url.path.startswith("/static/vendor/"):
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        elif request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "public, max-age=3600"
        elif _is_player_surface(request.url.path):
            response.headers["Cache-Control"] = "no-store"
        return response
