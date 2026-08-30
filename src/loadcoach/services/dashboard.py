"""loadcoach.services.dashboard — the numbers behind the Dashboard page (dev-plan P8).

Current activity, queue health, recent decisions, model mix and degradations — each read from the
service the corresponding page already uses (queue status, health, routing history, the job list,
reliability), so a figure on the dashboard is the same figure on its own page and is one click from
the record that produced it (UI standards §5).
"""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy import func, select

from loadcoach.infrastructure.db.models import Job, Model
from loadcoach.services.health import get_health_report
from loadcoach.services.queue import list_jobs
from loadcoach.services.reliability import regression_warnings
from loadcoach.services.routing import recent_decisions
from loadcoach.services.status import queue_status

if TYPE_CHECKING:
    from datetime import datetime

    from modelrack.provider import Provider

    from loadcoach.config import Settings
    from loadcoach.services.database import Database
    from loadcoach.services.worker import QueueRuntime

__all__ = ["MODEL_MIX_WINDOW", "dashboard_report"]

MODEL_MIX_WINDOW = timedelta(hours=24)
"""The model mix and throughput look back one day: long enough to show a pattern, short enough
that yesterday's incident does not colour today's page."""

_ACTIVE_STATES = ("queued", "leased", "admitted", "waiting_resources", "executing", "validating")


def _model_mix(database: Database, *, since: datetime) -> list[dict[str, Any]]:
    """Jobs per selected model since ``since``, split by terminal outcome."""
    with database.read() as session:
        rows = session.execute(
            select(Model.canonical_id, Job.state, func.count())
            .join(Model, Model.id == Job.selected_model_id)
            .where(Job.created_at > since)
            .group_by(Model.canonical_id, Job.state)
            .order_by(Model.canonical_id)
        ).all()
    mix: dict[str, dict[str, Any]] = {}
    for canonical_id, state, count in rows:
        entry = mix.setdefault(
            canonical_id,
            {"canonical_id": canonical_id, "jobs": 0, "completed": 0, "failed": 0, "other": 0},
        )
        entry["jobs"] += count
        if state == "completed":
            entry["completed"] += count
        elif state == "failed":
            entry["failed"] += count
        else:
            entry["other"] += count
    return sorted(mix.values(), key=lambda entry: (-entry["jobs"], entry["canonical_id"]))


def _throughput(database: Database, *, since: datetime) -> dict[str, int]:
    with database.read() as session:
        completed = session.execute(
            select(func.count())
            .select_from(Job)
            .where(Job.state == "completed", Job.completed_at > since)
        ).scalar_one()
        failed = session.execute(
            select(func.count())
            .select_from(Job)
            .where(Job.state == "failed", Job.completed_at > since)
        ).scalar_one()
    return {"completed": int(completed), "failed": int(failed)}


def dashboard_report(
    database: Database,
    *,
    settings: Settings,
    runtime: QueueRuntime | None,
    provider: Provider | None,
    now: datetime,
) -> dict[str, Any]:
    """Build the dashboard.

    Args:
        database: The application's database handle.
        settings: The application settings.
        runtime: The serving process's queue runtime, or ``None`` outside one.
        provider: The provider handle, for the health report.
        now: The current instant.

    Returns:
        ``activity`` (executing, queued, waiting, active total), ``queue`` (the queue §11 report),
        ``health`` (the component list), ``degradations`` (every component that is not ``ok``,
        every open breaker, every regression, in one list with a link each), ``decisions`` and
        ``jobs`` (the ten most recent), ``model_mix`` and ``throughput`` over the last 24 hours.
    """
    queue = queue_status(database, settings=settings, runtime=runtime, now=now)
    health = get_health_report(
        database=database, provider=provider, settings=settings, queue_runtime=runtime
    )
    depth = queue["depth_by_state"]
    executing = sum(depth.get(state, 0) for state in ("executing", "validating"))
    waiting = depth.get("waiting_resources", 0)
    queued = sum(depth.get(state, 0) for state in ("queued", "leased", "admitted"))

    degradations: list[dict[str, str]] = [
        {
            "kind": "health",
            "name": component.name,
            "status": component.status,
            "detail": component.detail,
            "href": "/api/v1/health",
        }
        for component in health.components
        if component.status not in ("ok", "not_configured")
    ]
    for breaker in queue.get("circuit_breakers") or ():
        if breaker["state"] != "closed":
            degradations.append(
                {
                    "kind": "circuit_breaker",
                    "name": breaker["canonical_id"],
                    "status": breaker["state"],
                    "detail": breaker["reason"],
                    "href": "/queue",
                }
            )
    for entry in regression_warnings(database):
        degradations.append(
            {
                "kind": "regression",
                "name": f"{entry.canonical_id} / {entry.task_profile_id}",
                "status": "regressed",
                "detail": entry.regression.reason,
                "href": f"/reliability?task={entry.task_profile_id}",
            }
        )
    if queue["flags"]["paused"]:
        degradations.append(
            {
                "kind": "queue",
                "name": "queue",
                "status": "paused",
                "detail": "dispatch is paused; queued jobs wait",
                "href": "/queue",
            }
        )
    if queue["flags"]["draining"]:
        degradations.append(
            {
                "kind": "queue",
                "name": "queue",
                "status": "draining",
                "detail": "nothing new is claimed; in-flight work finishes",
                "href": "/queue",
            }
        )

    since = now - MODEL_MIX_WINDOW
    return {
        "checked_at": queue["checked_at"],
        "activity": {
            "executing": executing,
            "queued": queued,
            "waiting_resources": waiting,
            "active": queue["active"],
            "max_depth": queue["max_depth"],
            "starving": queue["starving"],
            "oldest_queued_age_seconds": queue["oldest_queued_age_seconds"],
            "executions": queue["executions"],
        },
        "queue": queue,
        "health": {
            "status": health.status,
            "components": [component.model_dump() for component in health.components],
        },
        "degradations": degradations,
        "decisions": recent_decisions(database, limit=10),
        "jobs": list_jobs(database, limit=10),
        "model_mix": _model_mix(database, since=since),
        "throughput": _throughput(database, since=since),
        "window_hours": int(MODEL_MIX_WINDOW.total_seconds() // 3600),
    }
