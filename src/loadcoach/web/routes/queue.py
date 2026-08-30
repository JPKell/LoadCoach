"""loadcoach.web.routes.queue — ``GET /queue``, the queue controls, ``/system/status`` and the page.

Queue §11: depth by state and class, oldest queued age, dispatch latency, active executions with
their models, residency and idle times, the starvation counter, circuit-breaker states and recent
throughput. ``/system/status`` (api.md §1) is the same report with the telemetry snapshot beside
it. The controls (api.md §8) write the durable flags the scheduler reads every second, so a pause
survives a restart and a ``loadcoach queue pause`` from another process reaches this one.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Request, status
from fastapi.responses import HTMLResponse

from loadcoach.services.queue import queue_flags, set_queue_flag
from loadcoach.services.status import queue_status
from loadcoach.web.rendering import render

if TYPE_CHECKING:
    from loadcoach.services.worker import QueueRuntime

__all__ = ["router", "ui_router"]

router = APIRouter(tags=["queue"])
ui_router = APIRouter(tags=["ui"], include_in_schema=False)


def _runtime(request: Request) -> QueueRuntime | None:
    runtime: QueueRuntime | None = request.app.state.queue_runtime
    return runtime


@router.get("/queue", summary="Queue depth, latency, residency, breakers")
def get_queue(request: Request) -> dict[str, Any]:
    """Queue §11's report."""
    app = request.app
    return queue_status(
        app.state.database,
        settings=app.state.settings,
        runtime=_runtime(request),
        now=datetime.now(UTC),
    )


@router.get("/system/status", summary="Queue, executions, residency, telemetry")
def get_system_status(request: Request) -> dict[str, Any]:
    """api.md §1's status: the queue report plus the current telemetry snapshot."""
    from loadcoach.services.routing import telemetry_snapshot_json
    from loadcoach.web.routing_support import current_snapshot

    app = request.app
    report = queue_status(
        app.state.database,
        settings=app.state.settings,
        runtime=_runtime(request),
        now=datetime.now(UTC),
    )
    report["telemetry"] = telemetry_snapshot_json(current_snapshot(app))
    return report


def _set(
    request: Request, *, paused: bool | None = None, draining: bool | None = None
) -> dict[str, Any]:
    app = request.app
    now = datetime.now(UTC)
    runtime = _runtime(request)
    if runtime is not None:
        # The runtime's copy and the durable flag are written under one lock, so the
        # scheduler's refresh cannot overwrite this request with a value read just before it.
        runtime.flags.update(app.state.database, now=now, paused=paused, draining=draining)
    else:
        if paused is not None:
            set_queue_flag(app.state.database, "queue.paused", paused, now=now)
        if draining is not None:
            set_queue_flag(app.state.database, "queue.draining", draining, now=now)
    if runtime is not None and (paused is False or draining is False):
        runtime.wakeup.set()
    flags = queue_flags(app.state.database)
    return {
        "paused": flags["queue.paused"],
        "draining": flags["queue.draining"],
        "in_flight": 0 if runtime is None else len(runtime.in_flight),
    }


@router.post("/queue/pause", status_code=status.HTTP_202_ACCEPTED, summary="Stop dispatch")
def post_pause(request: Request) -> dict[str, Any]:
    """Admin: stop claiming without dropping jobs. In-flight work finishes."""
    return _set(request, paused=True)


@router.post("/queue/resume", status_code=status.HTTP_202_ACCEPTED, summary="Resume dispatch")
def post_resume(request: Request) -> dict[str, Any]:
    """Admin: resume claiming; also clears a drain."""
    return _set(request, paused=False, draining=False)


@router.post("/queue/drain", status_code=status.HTTP_202_ACCEPTED, summary="Drain for shutdown")
def post_drain(request: Request) -> dict[str, Any]:
    """Admin: finish in-flight work, claim nothing new. ``in_flight`` says how many remain."""
    return _set(request, draining=True)


@ui_router.get("/queue", summary="Queue page", response_class=HTMLResponse)
def queue_page(request: Request) -> HTMLResponse:
    """Render queue §11's report."""
    app = request.app
    report = queue_status(
        app.state.database,
        settings=app.state.settings,
        runtime=_runtime(request),
        now=datetime.now(UTC),
    )
    return HTMLResponse(render("queue/index.html", page="queue", report=report))
