"""Safe request correlation and essential in-process operational measurements."""

import logging
import re
import time
from collections import defaultdict
from collections.abc import Awaitable, Callable
from threading import Lock
from uuid import uuid4

from fastapi import FastAPI, Request, Response

logger = logging.getLogger(__name__)
_SAFE_REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


class MetricsRegistry:
    """Thread-safe counters and sums rendered in Prometheus text format."""

    def __init__(self) -> None:
        self._values: defaultdict[tuple[str, tuple[tuple[str, str], ...]], float] = defaultdict(
            float
        )
        self._lock = Lock()

    def increment(self, name: str, **labels: str) -> None:
        self.add(name, 1.0, **labels)

    def add(self, name: str, value: float, **labels: str) -> None:
        key = name, tuple(sorted(labels.items()))
        with self._lock:
            self._values[key] += value

    def render(self) -> str:
        """Return a stable snapshot without any high-cardinality identity labels."""

        with self._lock:
            values = sorted(self._values.items())
        lines: list[str] = []
        for (name, labels), value in values:
            suffix = ""
            if labels:
                suffix = "{" + ",".join(f'{key}="{label}"' for key, label in labels) + "}"
            lines.append(f"{name}{suffix} {value:g}")
        return "\n".join(lines) + "\n"


def install_observability(application: FastAPI, metrics: MetricsRegistry) -> None:
    """Install correlation middleware and the dependency-free metrics endpoint."""

    @application.middleware("http")
    async def observe_request(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        supplied = request.headers.get("X-Request-ID", "")
        request_id = supplied if _SAFE_REQUEST_ID.fullmatch(supplied) else str(uuid4())
        request.state.request_id = request_id
        started = time.perf_counter()
        status_code = 500
        try:
            response = await call_next(request)
            status_code = response.status_code
        finally:
            duration = time.perf_counter() - started
            route = request.scope.get("route")
            route_template = getattr(route, "path", "unmatched")
            metrics.increment(
                "http_requests_total",
                method=request.method,
                status_class=f"{status_code // 100}xx",
            )
            metrics.increment("http_request_duration_seconds_count", method=request.method)
            metrics.add("http_request_duration_seconds_sum", duration, method=request.method)
            logger.info(
                "request_completed",
                extra={
                    "request_id": request_id,
                    "method": request.method,
                    "path": route_template,
                    "status_code": status_code,
                    "duration_ms": round(duration * 1000, 3),
                },
            )
        response.headers["X-Request-ID"] = request_id
        return response

    @application.get("/metrics", include_in_schema=False)
    async def operational_metrics() -> Response:
        return Response(content=metrics.render(), media_type="text/plain; version=0.0.4")
