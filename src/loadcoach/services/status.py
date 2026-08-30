"""loadcoach.services.status — queue §11's observability report, shared by the API, UI and CLI.

Not in the Phase 5 file list verbatim. It exists for the same reason ``services/health.py`` does:
``GET /queue``, ``GET /system/status``, the Queue page and ``loadcoach queue status`` must report
identical numbers, and web and CLI may not import each other.
"""

from __future__ import annotations

import statistics
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from baseaicore.timeutil import to_rfc3339
from sqlalchemy import func, select

from loadcoach.infrastructure.db.models import Job, Model, Residency
from loadcoach.services.queue import queue_flags, queue_snapshot

if TYPE_CHECKING:
    from datetime import datetime

    from loadcoach.config import Settings
    from loadcoach.services.database import Database
    from loadcoach.services.worker import QueueRuntime

__all__ = ["queue_status", "residency_rows"]

_THROUGHPUT_WINDOW = timedelta(minutes=5)


def residency_rows(database: Database) -> list[dict[str, Any]]:
    """Every open residency episode with its model, device and idle time inputs."""
    with database.read() as session:
        rows = session.execute(
            select(Residency, Model.canonical_id)
            .join(Model, Model.id == Residency.model_id)
            .where(Residency.resident.is_(True))
            .order_by(Residency.gpu_index, Residency.last_used_at.desc())
        ).all()
        return [
            {
                "canonical_id": canonical_id,
                "gpu_index": row.gpu_index,
                "loaded_at": to_rfc3339(row.loaded_at),
                "last_used_at": to_rfc3339(row.last_used_at),
                "vram_bytes": None if row.vram_bytes is None else int(row.vram_bytes),
                "vram_bytes_unavailable_reason": row.vram_bytes_unavailable_reason,
            }
            for row, canonical_id in rows
        ]


def queue_status(
    database: Database,
    *,
    settings: Settings,
    runtime: QueueRuntime | None,
    now: datetime,
) -> dict[str, Any]:
    """Build queue §11's report.

    What the database knows — depth, ages, starvation, residency, throughput, the control flags —
    is reported from any process; what only the running process knows — active executions,
    dispatch latency, circuit breakers, the last recovery — is reported when ``runtime`` is given
    and ``null`` otherwise, which is a different statement from zero.
    """
    snapshot = queue_snapshot(
        database, now=now, default_max_wait_seconds=settings.queue.max_wait_seconds
    )
    flags = queue_flags(database)
    with database.read() as session:
        completed_recently = int(
            session.execute(
                select(func.count())
                .select_from(Job)
                .where(Job.state == "completed", Job.completed_at > now - _THROUGHPUT_WINDOW)
            ).scalar_one()
        )
    residency = residency_rows(database)
    for row in residency:
        from baseaicore.timeutil import from_rfc3339

        row["idle_seconds"] = (now - from_rfc3339(row["last_used_at"])).total_seconds()

    report: dict[str, Any] = {
        "depth_by_state": snapshot.depth_by_state,
        "depth_by_class": snapshot.depth_by_class,
        "active": snapshot.active,
        "max_depth": settings.queue.max_depth,
        "oldest_queued_age_seconds": snapshot.oldest_queued_age_seconds,
        "starving": snapshot.starving,
        "throughput": {"completed_last_5m": completed_recently},
        "residency": residency,
        "flags": {"paused": flags["queue.paused"], "draining": flags["queue.draining"]},
        "executions": None,
        "dispatch_latency_ms": None,
        "circuit_breakers": None,
        "last_recovery": None,
        "checked_at": to_rfc3339(now),
    }
    if runtime is not None:
        samples = list(runtime.dispatch_samples_ms)
        report["executions"] = [
            {
                "job_id": entry.job_id,
                "worker": entry.worker_id,
                "state": entry.state,
                "class": entry.job_class,
                "canonical_id": entry.canonical_id,
                "target_gpu_index": entry.target_gpu_index,
                "claimed_at": to_rfc3339(entry.claimed_at),
            }
            for entry in runtime.in_flight.snapshot()
        ]
        report["dispatch_latency_ms"] = {
            "samples": len(samples),
            "median": statistics.median(samples) if samples else None,
            "max": max(samples) if samples else None,
        }
        report["circuit_breakers"] = [verdict.as_json() for verdict in runtime.breakers.verdicts()]
        report["last_recovery"] = (
            None if runtime.last_recovery is None else runtime.last_recovery.as_json()
        )
    return report
