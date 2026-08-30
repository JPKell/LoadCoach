"""loadcoach.web.routes.system — health and version.

``GET /version`` is never authenticated (ADR-0026 §5): version negotiation must work before a
client can know whether its credential is valid. Authentication itself does not exist until a later
phase, so today that is simply the router's default.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse
from mirrorwall import sse_response

from loadcoach.__about__ import __version__
from loadcoach.domain.authorization import authorize
from loadcoach.services.health import get_health_report
from loadcoach.services.machine import machine_fingerprint
from loadcoach.services.status import queue_status
from loadcoach.services.telemetry_stream import TELEMETRY_STREAM_ID, telemetry_payload
from loadcoach.web.auth import CurrentPrincipal
from loadcoach.web.rendering import render
from loadcoach.web.routes.generate import GENERATOR
from loadcoach.web.routing_support import current_snapshot

if TYPE_CHECKING:
    from starlette.responses import StreamingResponse

__all__ = ["router", "ui_router"]

router = APIRouter(tags=["system"])
ui_router = APIRouter(tags=["ui"], include_in_schema=False)


@router.get("/health", summary="Component health")
async def health(request: Request, principal: CurrentPrincipal) -> JSONResponse:
    """Return the current health report; 200 for ok/degraded, 503 for unavailable.

    Reports on the handle the server is serving from, not a connection opened for the check — a
    health check against a different connection than requests use is answering a question nobody
    asked.
    """
    authorize(principal, "read")
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


@router.get("/system/telemetry/stream", summary="Sampled machine telemetry")
async def telemetry_stream(request: Request, principal: CurrentPrincipal) -> StreamingResponse:
    """SSE: one ``telemetry.sampled`` frame per ``[telemetry] interval_ms`` (api.md §1).

    A new client receives the latest sample at once and follows from there; the stream is
    open-ended and closes when the client disconnects.
    """
    authorize(principal, "read")
    sampler = request.app.state.telemetry_stream
    return sse_response(
        sampler,
        stream_id=TELEMETRY_STREAM_ID,
        last_event_id=request.headers.get("last-event-id"),
        generator=GENERATOR,
        heartbeat_seconds=15.0,
        poll_interval_seconds=0.05,
        terminal_events=frozenset(),
    )


@ui_router.get("/system", summary="System page", response_class=HTMLResponse)
def system_page(request: Request, principal: CurrentPrincipal) -> HTMLResponse:
    """Telemetry, residency, the thread pool, dispatch latency, starvation and breakers (P8)."""
    authorize(principal, "read")
    app = request.app
    runtime = app.state.queue_runtime
    now = datetime.now(UTC)
    snapshot = current_snapshot(app)
    report = queue_status(app.state.database, settings=app.state.settings, runtime=runtime, now=now)
    health = get_health_report(
        database=app.state.database,
        provider=app.state.provider,
        settings=app.state.settings,
        queue_runtime=runtime,
    )
    return HTMLResponse(
        render(
            "system/index.html",
            page="system",
            telemetry=None if snapshot is None else telemetry_payload(snapshot),
            report=report,
            health=health,
            workers=None if runtime is None else len(runtime.workers),
            in_flight=None if runtime is None else len(runtime.in_flight),
            max_concurrent_jobs=app.state.settings.execution.max_concurrent_jobs,
            machine_fingerprint=machine_fingerprint(),
            version=__version__,
        )
    )
