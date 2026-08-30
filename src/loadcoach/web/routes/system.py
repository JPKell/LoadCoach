"""loadcoach.web.routes.system — health and version.

``GET /version`` is never authenticated (ADR-0026 §5): version negotiation must work before a
client can know whether its credential is valid. Authentication itself does not exist until a later
phase, so today that is simply the router's default.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from loadcoach.__about__ import __version__
from loadcoach.services.health import get_health_report

__all__ = ["router"]

router = APIRouter(tags=["system"])


@router.get("/health", summary="Component health")
async def health(request: Request) -> JSONResponse:
    """Return the current health report; 200 for ok/degraded, 503 for unavailable.

    Reports on the handle the server is serving from, not a connection opened for the check — a
    health check against a different connection than requests use is answering a question nobody
    asked.
    """
    report = get_health_report(
        database=request.app.state.database,
        provider=request.app.state.provider,
        settings=request.app.state.settings,
        queue_runtime=request.app.state.queue_runtime,
    )
    status_code = 200 if report.status in ("ok", "degraded") else 503
    return JSONResponse(status_code=status_code, content=report.model_dump(mode="json"))


@router.get("/version", summary="Application and API versions")
async def version() -> dict[str, object]:
    """Return the application version and served API majors."""
    return {
        "application": {"name": "loadcoach", "version": __version__, "git_commit": None},
        "api": {"current": "v1", "supported": ["v1"], "deprecated": []},
    }
