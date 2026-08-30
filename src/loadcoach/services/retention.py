"""loadcoach.services.retention — content retention for finished jobs (spec §14, data model §3).

Prompts and responses are stored as hashes by default; the full text is kept only while a caller
can still reasonably want it. A queued job keeps its transcript until it has run (P5-15), and a
finished job keeps its text for ``[storage] content_retention_hours``; then the sweep replaces
text with nothing, leaving the hashes, the tokens, the timings and the routing intact. Setting
``retain_content = true`` keeps everything for ever, and that switch is config-only (spec §14:
full content only when explicitly enabled).

What is scrubbed, on a terminal job older than the retention: ``prompt_text``, ``response_text``,
``structured_output_json``, ``tool_calls_json``, ``reasoning_summary``, the ``messages`` inside
``request_json`` (its task, format, sampling and overrides stay, so the job is still explicable),
and the ``output``/``reasoning`` of its persisted ``result`` event. A scrubbed job records when,
so the page and the API can say "content removed by retention" rather than showing nothing.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from baseaicore.timeutil import to_rfc3339
from sqlalchemy import select

from loadcoach.domain.queue_state import TERMINAL_STATES
from loadcoach.infrastructure.db.models import Job, JobEvent

if TYPE_CHECKING:
    from datetime import datetime

    from loadcoach.services.database import Database

__all__ = ["SCRUBBED_MARKER", "RetentionOutcome", "scrub_content"]

SCRUBBED_MARKER = "content_scrubbed_at"
"""The key written into ``request_json`` when a job's text has been removed by retention."""

_SCRUBBED_EVENT_KEYS = ("output", "reasoning")


@dataclass(frozen=True, slots=True)
class RetentionOutcome:
    """What one sweep did."""

    scrubbed_jobs: int
    scrubbed_events: int
    cutoff: datetime


def scrub_content(
    database: Database, *, now: datetime, retention_hours: int, batch_size: int = 500
) -> RetentionOutcome:
    """Remove prompt and response text from jobs finished before ``now - retention_hours``.

    Idempotent: a job already scrubbed carries the marker and is not selected again. Jobs that
    are not terminal are never touched, whatever their age.

    Args:
        database: The application's database handle.
        now: The sweep instant.
        retention_hours: How long finished text is kept.
        batch_size: The most jobs one sweep scrubs, so a first sweep over a large history is
            bounded; the next sweep continues.

    Returns:
        The :class:`RetentionOutcome`.
    """
    cutoff = now - timedelta(hours=retention_hours)
    scrubbed_jobs = 0
    scrubbed_events = 0
    with database.write() as session:
        jobs = (
            session.execute(
                select(Job)
                .where(
                    Job.state.in_([state.value for state in TERMINAL_STATES]),
                    Job.completed_at.is_not(None),
                    Job.completed_at <= cutoff,
                )
                .order_by(Job.completed_at)
                .limit(batch_size * 4)
            )
            .scalars()
            .all()
        )
        for job in jobs:
            request = dict(job.request_json) if isinstance(job.request_json, dict) else {}
            if SCRUBBED_MARKER in request:
                continue
            if scrubbed_jobs >= batch_size:
                break
            request.pop("messages", None)
            request.pop("prompt", None)
            request.pop("system", None)
            request[SCRUBBED_MARKER] = to_rfc3339(now)
            job.request_json = request
            job.prompt_text = None
            job.response_text = None
            job.structured_output_json = None
            job.tool_calls_json = None
            job.reasoning_summary = None
            scrubbed_jobs += 1
            events = (
                session.execute(
                    select(JobEvent).where(
                        JobEvent.job_id == job.id,
                        JobEvent.event_type.in_(["result", "job.completed"]),
                    )
                )
                .scalars()
                .all()
            )
            for event in events:
                data: Any = event.data_json
                if not isinstance(data, dict):
                    continue
                if not any(key in data for key in _SCRUBBED_EVENT_KEYS):
                    continue
                cleaned = {k: v for k, v in data.items() if k not in _SCRUBBED_EVENT_KEYS}
                cleaned[SCRUBBED_MARKER] = to_rfc3339(now)
                event.data_json = cleaned
                scrubbed_events += 1
    return RetentionOutcome(
        scrubbed_jobs=scrubbed_jobs, scrubbed_events=scrubbed_events, cutoff=cutoff
    )
