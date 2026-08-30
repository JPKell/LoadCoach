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
from fastapi.responses import HTMLResponse, RedirectResponse
from mirrorwall import sse_response

from loadcoach.services.queue import queue_flags, set_queue_flag
from loadcoach.services.queue_stream import QUEUE_STREAM_ID
from loadcoach.services.status import queue_status
from loadcoach.web.auth import require_scope
from loadcoach.web.csrf import render_form_page
from loadcoach.web.routes.generate import GENERATOR

if TYPE_CHECKING:
    from starlette.responses import StreamingResponse

    from loadcoach.config import Settings
    from loadcoach.services.worker import QueueRuntime

__all__ = ["router", "ui_router"]

router = APIRouter(tags=["queue"])
ui_router = APIRouter(tags=["ui"], include_in_schema=False)


def _runtime(request: Request) -> QueueRuntime | None:
    runtime: QueueRuntime | None = request.app.state.queue_runtime
    return runtime


def _authorize(request: Request, *, scope: str) -> None:
    """Enforce a scope for this request (api.md §11): the controls are ``admin``."""
    settings: Settings = request.app.state.settings
    request.state.token_name = require_scope(
        request.app.state.database,
        required=scope,
        authorization=request.headers.get("authorization"),
        bind_host=settings.server.host,
        now=datetime.now(UTC),
    )


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
    _authorize(request, scope="admin")
    return _set(request, paused=True)


@router.post("/queue/resume", status_code=status.HTTP_202_ACCEPTED, summary="Resume dispatch")
def post_resume(request: Request) -> dict[str, Any]:
    """Admin: resume claiming; also clears a drain."""
    _authorize(request, scope="admin")
    return _set(request, paused=False, draining=False)


@router.post("/queue/drain", status_code=status.HTTP_202_ACCEPTED, summary="Drain for shutdown")
def post_drain(request: Request) -> dict[str, Any]:
    """Admin: finish in-flight work, claim nothing new. ``in_flight`` says how many remain."""
    _authorize(request, scope="admin")
    return _set(request, draining=True)


@router.get("/queue/stream", summary="Live queue status")
async def get_queue_stream(request: Request) -> StreamingResponse:
    """SSE: one ``queue.status`` frame per change, each the whole current report and the page
    fragment rendered from it — never a diff, so a reconnect is right after one frame."""
    return sse_response(
        request.app.state.queue_stream,
        stream_id=QUEUE_STREAM_ID,
        last_event_id=request.headers.get("last-event-id"),
        generator=GENERATOR,
        heartbeat_seconds=15.0,
        poll_interval_seconds=0.05,
        terminal_events=frozenset(),
    )


@ui_router.get("/queue", summary="Queue page", response_class=HTMLResponse)
def queue_page(request: Request) -> HTMLResponse:
    """Render queue §11's report, with the controls and the live region."""
    app = request.app
    report = queue_status(
        app.state.database,
        settings=app.state.settings,
        runtime=_runtime(request),
        now=datetime.now(UTC),
    )
    return render_form_page(request, "queue/index.html", page="queue", report=report)


def _control_form(request: Request, **flags: bool | None) -> RedirectResponse:
    """A page's control form: the same admin scope and the same write, then back to the page."""
    _authorize(request, scope="admin")
    _set(request, **flags)
    return RedirectResponse("/queue", status_code=status.HTTP_303_SEE_OTHER)


@ui_router.post("/queue/pause", summary="Pause from the page")
def queue_pause_form(request: Request) -> RedirectResponse:
    """The Pause button (CSRF-checked by MirrorWall's middleware)."""
    return _control_form(request, paused=True)


@ui_router.post("/queue/resume", summary="Resume from the page")
def queue_resume_form(request: Request) -> RedirectResponse:
    """The Resume button."""
    return _control_form(request, paused=False, draining=False)


@ui_router.post("/queue/drain", summary="Drain from the page")
def queue_drain_form(request: Request) -> RedirectResponse:
    """The Drain button."""
    return _control_form(request, draining=True)
