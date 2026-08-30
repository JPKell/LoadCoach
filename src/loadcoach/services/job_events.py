"""loadcoach.services.job_events — persisted job events, gap-free sequences and live fan-out.

The persisted half of the streaming contract (API standards §8): every state change is written to
``job_events`` **before** it is published, the store is the source of truth, and the in-memory
fan-out is only a latency optimization. ``GET /jobs/{id}/stream`` replays from the table and then
follows the broker, which is what makes a reconnect — and a restart — survivable.

**One sequence per job, shared by what is persisted and what is not.** Token deltas are fanned out
live but never stored (one row per token would dominate the database for no benefit a reconnecting
client can use: it resumes from the last event it saw and receives the terminal event, which
carries the whole output). They still consume sequence numbers, because MirrorWall's replay/live
handoff drops any live frame whose sequence is not above the last one delivered. The sink therefore
keeps, per job, the highest sequence it has handed out in this process — persisted or not — and the
next persisted event takes ``max(stored, handed out) + 1``. Only the process that streamed the
tokens knows that high-water mark; an event written by another process (a CLI cancel) takes the
stored maximum plus one, which a client still mid-stream would drop as already seen. That is the
one documented gap, and it is bounded by a single event.

Not in the Phase 5 file list verbatim. It exists because the queue service, the worker and the two
streaming routes all need the same three things — a sequence, a row and a publish — and putting them
in any one of those modules would make the others import it for the wrong reason.
"""

from __future__ import annotations

import threading
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from baseaicore import new_id
from baseaicore.timeutil import to_rfc3339
from mirrorwall import TOKEN_EVENT, Event, EventBroker
from sqlalchemy import func, select

from loadcoach.infrastructure.db.models import JobEvent

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping, Sequence
    from contextlib import AbstractContextManager
    from datetime import datetime

    from mirrorwall import Subscription
    from sqlalchemy.orm import Session

    from loadcoach.services.database import Database

__all__ = [
    "LIVE_BUFFER_SIZE",
    "TERMINAL_JOB_EVENTS",
    "TERMINAL_STREAM_EVENTS",
    "TOKEN_EVENT",
    "EventWriter",
    "JobEventSink",
    "JobEventSource",
    "is_job_event",
]

LIVE_BUFFER_SIZE = 4096
"""How many unpersisted frames (tokens, tool calls) the sink keeps per **live** job.

A subscriber that attaches after the first tokens were fanned out — the browser's stream response
starting a few milliseconds after the execution thread — would otherwise miss them for ever,
since they are never stored. The buffer closes that gap for the job's lifetime and is dropped
with the terminal event: after completion a reconnect replays the persisted frames only, which
is api.md §4's promise. Bounded, so a runaway generation costs a fixed amount of memory."""

# ``TOKEN_EVENT`` is re-exported from MirrorWall rather than spelled here: the one bare
# (un-enveloped) frame is the one MirrorWall's formatter keys on (ADR-0025 §3). api.md §5 lists it
# as ``job.token`` among a job's events; the frame name stays ``token`` because widening the
# exception to any other name is forbidden.

TERMINAL_JOB_EVENTS: frozenset[str] = frozenset({"job.completed", "job.failed", "job.cancelled"})
"""After any of these, a job's stream closes (API standards §8)."""

TERMINAL_STREAM_EVENTS: frozenset[str] = TERMINAL_JOB_EVENTS | {"result", "error"}
"""Every terminal frame: a queued job's, and a synchronous execution's (api.md §4)."""


def is_job_event(event_type: str) -> bool:
    """Whether ``event_type`` is one of api.md §5's ``job.*`` state events.

    Two frame shapes share the table. A ``job.*`` event's envelope payload is the event object —
    entity, timestamp, message, data (API standards §8). A synchronous execution's frames —
    ``routing``, ``result``, ``error``, ``tool_call`` — carry the document itself as the payload,
    exactly as api.md §4 shows (``payload: {…the full response object…}``), so a client reads
    ``payload["output"]``, not ``payload["data"]["output"]``.
    """
    return event_type.startswith("job.")


def _payload(
    *,
    event_id: str,
    job_id: str,
    timestamp: datetime,
    message: str | None,
    data: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """The event object inside the envelope's ``payload`` (API standards §8)."""
    return {
        "event_id": event_id,
        "entity": {"kind": "job", "id": job_id},
        "timestamp": to_rfc3339(timestamp),
        "message": message,
        "data": dict(data or {}),
    }


@dataclass
class EventWriter:
    """Stages events inside one write transaction; the sink publishes them after commit."""

    session: Session
    sink: JobEventSink
    staged: list[tuple[str, Event]] = field(default_factory=list)

    def append(
        self,
        job_id: str,
        event_type: str,
        *,
        now: datetime,
        message: str | None = None,
        data: Mapping[str, Any] | None = None,
    ) -> Event:
        """Persist one event with the job's next sequence and stage it for publication.

        Args:
            job_id: The job.
            event_type: ``job.<state>`` or another documented name.
            now: The event's timestamp.
            message: A one-line human summary, or ``None``.
            data: The event body; must be JSON-serializable.

        Returns:
            The event as it will be published.
        """
        sequence = self.sink.next_sequence(self.session, job_id)
        event_id = new_id()
        self.session.add(
            JobEvent(
                id=event_id,
                job_id=job_id,
                sequence=sequence,
                timestamp=now,
                event_type=event_type,
                message=message,
                data_json=dict(data or {}),
            )
        )
        event = Event(
            sequence=sequence,
            type=event_type,
            payload=_payload(
                event_id=event_id, job_id=job_id, timestamp=now, message=message, data=data
            )
            if is_job_event(event_type)
            else dict(data or {}),
        )
        self.staged.append((job_id, event))
        return event


class JobEventSink:
    """Where every job event goes: the table first, then the broker. One per process."""

    def __init__(self, *, broker: EventBroker | None = None) -> None:
        """Create a sink over ``broker`` (a fresh one when ``None``)."""
        self.broker = broker if broker is not None else EventBroker()
        self._high: dict[str, int] = {}
        self._live: dict[str, deque[Event]] = {}
        self._lock = threading.Lock()

    @contextmanager
    def write(self, database: Database) -> Iterator[tuple[Session, EventWriter]]:
        """One write transaction whose staged events are published once it has committed.

        Publishing after commit, never inside the transaction, is what keeps the store the source
        of truth: a subscriber can never see an event whose row was rolled back.
        """
        with database.write() as session:
            writer = EventWriter(session=session, sink=self)
            yield session, writer
        for job_id, event in writer.staged:
            self.broker.publish(job_id, event)
            if event.type in TERMINAL_STREAM_EVENTS:
                self.forget(job_id)

    def next_sequence(self, session: Session, job_id: str) -> int:
        """Allocate the job's next sequence: above every stored and every handed-out number."""
        stored = session.execute(
            select(func.coalesce(func.max(JobEvent.sequence), 0)).where(JobEvent.job_id == job_id)
        ).scalar_one()
        with self._lock:
            sequence = max(int(stored), self._high.get(job_id, 0)) + 1
            self._high[job_id] = sequence
        return sequence

    def publish_token(self, job_id: str, payload: Mapping[str, Any]) -> Event:
        """Fan out one token delta without persisting it. Takes the next sequence number."""
        return self.publish_live(job_id, TOKEN_EVENT, payload)

    def publish_live(self, job_id: str, event_type: str, payload: Mapping[str, Any]) -> Event:
        """Fan out one unpersisted frame (a token, a tool call), buffered while the job lives."""
        with self._lock:
            sequence = self._high.get(job_id, 0) + 1
            self._high[job_id] = sequence
            event = Event(sequence=sequence, type=event_type, payload=dict(payload))
            self._live.setdefault(job_id, deque(maxlen=LIVE_BUFFER_SIZE)).append(event)
        self.broker.publish(job_id, event)
        return event

    def forget(self, job_id: str) -> None:
        """Drop the in-memory counter and live buffer of a finished job so both stay bounded."""
        with self._lock:
            self._high.pop(job_id, None)
            self._live.pop(job_id, None)

    def replay(
        self, database: Database, job_id: str, *, after_sequence: int, limit: int
    ) -> Sequence[Event]:
        """Read up to ``limit`` persisted events after ``after_sequence``, ascending."""
        with database.read() as session:
            rows = (
                session.execute(
                    select(JobEvent)
                    .where(JobEvent.job_id == job_id, JobEvent.sequence > after_sequence)
                    .order_by(JobEvent.sequence)
                    .limit(limit)
                )
                .scalars()
                .all()
            )
            persisted = [
                Event(
                    sequence=row.sequence,
                    type=row.event_type,
                    payload=_payload(
                        event_id=row.id,
                        job_id=row.job_id,
                        timestamp=row.timestamp,
                        message=row.message,
                        data=row.data_json if isinstance(row.data_json, dict) else None,
                    )
                    if is_job_event(row.event_type)
                    else dict(row.data_json)
                    if isinstance(row.data_json, dict)
                    else {},
                )
                for row in rows
            ]
        with self._lock:
            live = [e for e in self._live.get(job_id, ()) if e.sequence > after_sequence]
        if not live:
            return persisted
        # Merge the live buffer in: a client attaching mid-generation gets the tokens already
        # fanned out. Persisted rows win a sequence collision, which cannot happen while the
        # counter is this process's; the sort keeps the batch ascending either way.
        merged = {event.sequence: event for event in live}
        merged.update({event.sequence: event for event in persisted})
        return [merged[sequence] for sequence in sorted(merged)][:limit]

    def source(self, database: Database, job_id: str) -> JobEventSource:
        """Build the MirrorWall event source for one job's stream."""
        return JobEventSource(self, database, job_id)


class JobEventSource:
    """A MirrorWall ``EventSource`` over one job: replay from the table, live from the broker."""

    def __init__(self, sink: JobEventSink, database: Database, job_id: str) -> None:
        """Bind to one job."""
        self._sink = sink
        self._database = database
        self.job_id = job_id

    def replay(self, *, stream_id: str, after_sequence: int, limit: int) -> Sequence[Event]:
        """Persisted events after ``after_sequence``, in bounded batches."""
        return self._sink.replay(
            self._database, stream_id, after_sequence=after_sequence, limit=limit
        )

    def subscribe(self, *, stream_id: str) -> AbstractContextManager[Subscription]:
        """Open a live subscription on the broker."""
        return self._sink.broker.subscribe(stream_id=stream_id)
