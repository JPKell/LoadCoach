"""loadcoach.web.routes.jobs — the ``/jobs`` API (api.md §5) and the Jobs pages.

Route handlers contain no business logic: each calls one service function and renders.
``GET /jobs/{id}/explanation`` is a **lookup** of the routing decision whose ``job_id`` matches —
the explanation lives on the decision row and nowhere else (LCX3). ``GET /jobs/{id}/stream``
replays from the persisted ``job_events`` and follows the live broker, and names its own
terminal events so the connection closes when the job finishes (LCX16).
"""

from __future__ import annotations

import base64
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Query, Request, Response, status
from fastapi.responses import HTMLResponse
from mirrorwall import clamp_limit, paginated_response, sse_response
from pydantic import Field
from sqlalchemy import select

from loadcoach.domain.priority import JobClass
from loadcoach.domain.queue_state import JobState
from loadcoach.infrastructure.db.models import RoutingDecision
from loadcoach.services.job_events import TERMINAL_JOB_EVENTS
from loadcoach.services.queue import (
    JobNotFound,
    JobSubmission,
    cancel_job,
    enqueue,
    get_job,
    job_document,
    list_jobs,
)
from loadcoach.web.rendering import render
from loadcoach.web.routes.generate import (
    GENERATOR,
    GenerateBody,
    messages_of,
    overrides_of,
    source_of,
)

if TYPE_CHECKING:
    from starlette.responses import StreamingResponse

__all__ = ["JobBody", "router", "ui_router"]

router = APIRouter(tags=["jobs"])
ui_router = APIRouter(tags=["ui"], include_in_schema=False)

_STREAM_TERMINAL = TERMINAL_JOB_EVENTS | {"result", "error"}
"""A queued job ends with ``job.completed``/``job.failed``/``job.cancelled``; a synchronous
job's stream ends with ``result``/``error``. Both close the connection."""


class JobBody(GenerateBody):
    """``POST /jobs``'s body: ``/generate``'s plus class, priority, wait bound and idempotency."""

    job_class: str = Field(
        default="normal", alias="class", pattern="^(interactive|normal|background|batch)$"
    )
    priority: int | None = Field(default=None, ge=0, le=999)
    max_wait_seconds: int | None = Field(default=None, ge=1)
    idempotent: bool = Field(default=True)
    stream: bool = Field(default=False)


def _submission(body: JobBody, *, source: str) -> JobSubmission:
    return JobSubmission(
        task=body.task,
        prompt=body.prompt,
        system=body.system,
        messages=messages_of(body),
        response_format=body.response_format,
        sampling=dict(body.sampling),
        overrides=overrides_of(body.overrides),
        job_class=JobClass(body.job_class),
        priority=body.priority,
        max_wait_seconds=body.max_wait_seconds,
        idempotent=body.idempotent,
        idempotency_key=body.idempotency_key,
        source=source,
        stream=body.stream,
    )


@router.post("/jobs", status_code=status.HTTP_202_ACCEPTED, summary="Submit a job")
def post_job(request: Request, body: JobBody, response: Response) -> dict[str, Any]:
    """Enqueue ``body`` and return the job. ``202``; a repeated key returns the original job.

    Errors: ``TASK_PROFILE_NOT_FOUND``, ``VALIDATION_ERROR`` (a priority outside the class's
    band), ``QUEUE_FULL`` (429).
    """
    app = request.app
    runtime = app.state.queue_runtime
    outcome = enqueue(
        app.state.database,
        _submission(body, source=source_of(request)),
        now=datetime.now(UTC),
        queue_settings=app.state.settings.queue,
        execution_settings=app.state.settings.execution,
        sink=app.state.event_sink,
        wakeup=None if runtime is None else runtime.wakeup,
    )
    response.headers["X-Idempotent-Replay"] = "false" if outcome.created else "true"
    return job_document(app.state.database, outcome.job_id)


def _encode_cursor(created_at: datetime, job_id: str) -> str:
    raw = f"{created_at.astimezone(UTC).isoformat()}|{job_id}".encode()
    return base64.urlsafe_b64encode(raw).decode("ascii")


def _decode_cursor(cursor: str | None) -> datetime | None:
    if not cursor:
        return None
    try:
        raw = base64.urlsafe_b64decode(cursor.encode("ascii")).decode("utf-8")
        stamp, _, _ = raw.partition("|")
        return datetime.fromisoformat(stamp)
    except (ValueError, UnicodeDecodeError):
        return None


@router.get("/jobs", summary="List jobs")
def get_jobs(
    request: Request,
    state: str | None = Query(default=None),
    job_class: str | None = Query(default=None, alias="class"),
    task: str | None = Query(default=None),
    source: str | None = Query(default=None),
    limit: int | None = Query(default=None),
    cursor: str | None = Query(default=None),
) -> Response:
    """Jobs newest first, filtered by state, class, task and source; cursor-paginated."""
    effective = clamp_limit(limit, maximum=200)
    states = None if state is None else [JobState(s) for s in state.split(",") if s]
    records = list_jobs(
        request.app.state.database,
        states=states,
        job_class=None if job_class is None else JobClass(job_class),
        task=task,
        source=source,
        limit=effective + 1,
        before=_decode_cursor(cursor),
    )
    page = records[:effective]
    has_more = len(records) > effective
    next_cursor = _encode_cursor(page[-1].created_at, page[-1].job_id) if has_more else None
    documents = [job_document(request.app.state.database, record.job_id) for record in page]
    return paginated_response(
        documents,
        limit=effective,
        next_cursor=next_cursor,
        has_more=has_more,
        request_id=getattr(request.state, "request_id", None),
    )


@router.get("/jobs/{job_id}", summary="One job in full")
def get_job_document(request: Request, job_id: str) -> dict[str, Any]:
    """The job: state, attempts, routing summary, usage, timings, validation, degradations."""
    return job_document(request.app.state.database, job_id)


@router.get("/jobs/{job_id}/stream", summary="The job's event stream")
async def get_job_stream(request: Request, job_id: str) -> StreamingResponse:
    """SSE: state changes, tokens when streaming was requested, and the terminal event.

    Replays the persisted events after ``Last-Event-ID`` and follows the live broker; closes on
    the terminal event.
    """
    import anyio

    app = request.app
    await anyio.to_thread.run_sync(get_job, app.state.database, job_id)  # 404 before streaming
    return sse_response(
        app.state.event_sink.source(app.state.database, job_id),
        stream_id=job_id,
        last_event_id=request.headers.get("last-event-id"),
        generator=GENERATOR,
        heartbeat_seconds=15.0,
        poll_interval_seconds=0.01,
        terminal_events=_STREAM_TERMINAL,
    )


@router.post("/jobs/{job_id}/cancel", status_code=status.HTTP_202_ACCEPTED, summary="Cancel a job")
def post_cancel(request: Request, job_id: str) -> dict[str, Any]:
    """Cancel: at once for a waiting job, at the next chunk boundary for an executing one.

    ``409 JOB_NOT_CANCELLABLE`` for a terminal job; idempotent otherwise.
    """
    app = request.app
    runtime = app.state.queue_runtime
    outcome = cancel_job(
        app.state.database,
        app.state.event_sink,
        job_id,
        now=datetime.now(UTC),
        on_request=None if runtime is None else runtime.in_flight.request_cancel,
    )
    return {"job_id": outcome.job_id, "state": outcome.state.value, "already": outcome.already}


@router.get("/jobs/{job_id}/explanation", summary="The job's routing explanation")
def get_job_explanation(request: Request, job_id: str) -> dict[str, Any]:
    """The routing decision whose ``job_id`` matches — a lookup, never a copy (LCX3)."""
    database = request.app.state.database
    get_job(database, job_id)  # JOB_NOT_FOUND if there is no such job at all
    with database.read() as session:
        row = session.execute(
            select(RoutingDecision.explanation_json)
            .where(RoutingDecision.job_id == job_id)
            .order_by(RoutingDecision.requested_at.desc())
            .limit(1)
        ).scalar_one_or_none()
    if not isinstance(row, dict):
        raise JobNotFound(
            f"Job {job_id!r} has no routing decision yet.", details={"job_id": job_id}
        )
    return row


@ui_router.get("/jobs", summary="Jobs page", response_class=HTMLResponse)
def jobs_page(request: Request) -> HTMLResponse:
    """Render the most recent jobs."""
    records = list_jobs(request.app.state.database, limit=100)
    return HTMLResponse(render("jobs/index.html", page="jobs", jobs=records))


@ui_router.get("/jobs/{job_id}", summary="Job page", response_class=HTMLResponse)
def job_page(request: Request, job_id: str) -> HTMLResponse:
    """Render one job: its document, attempts and event history."""
    from loadcoach.infrastructure.db.models import JobEvent

    database = request.app.state.database
    document = job_document(database, job_id)
    with database.read() as session:
        events = [
            {
                "sequence": row.sequence,
                "timestamp": row.timestamp,
                "type": row.event_type,
                "message": row.message,
            }
            for row in session.execute(
                select(JobEvent).where(JobEvent.job_id == job_id).order_by(JobEvent.sequence)
            ).scalars()
        ]
    return HTMLResponse(render("jobs/detail.html", page="jobs", job=document, events=events))
