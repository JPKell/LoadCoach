"""loadcoach.services.recovery — startup recovery, idempotent, before any work is accepted.

Queue §10, in order, as one function the runtime calls before it starts a single worker:

1. Release every lease whose owner is not a worker of **this** process. In a single-process
   design every lease found at startup belongs to a process that is gone, whether or not it has
   expired yet — waiting for expiry would only delay recovery by up to ``lease_seconds``.
2. Jobs in a lease-holding state — ``leased``, ``admitted``, ``executing``, ``validating``,
   ``retrying`` (ADR-0036 §1) — return to ``queued`` if idempotent, or fail with ``worker_lost``.
   ``attempt`` is untouched, so the next attempt continues the sequence.
3. ``cancelling`` jobs complete to ``cancelled``: the worker that was stopping them is gone.
4. ``waiting_resources`` jobs are re-evaluated against current telemetry, through the same
   function the scheduler runs every few seconds.
5. The ageing sweep runs — the same statement the scheduler runs every thirty seconds, not a
   startup-only path (ADR-0029 §1).
6. The reconciliation is logged and every affected job has its event.

Running it twice changes nothing: the second pass finds no foreign lease, no ``cancelling`` job
and nothing to age.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from loadcoach.domain.queue_state import (
    LEASE_HOLDING_STATES,
    JobState,
    event_type_for,
    recovery_target,
)
from loadcoach.infrastructure.db.models import Job
from loadcoach.services.queue import ageing_sweep, transition

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import datetime

    from loadcoach.config import QueueSettings
    from loadcoach.services.database import Database
    from loadcoach.services.job_events import JobEventSink

__all__ = ["RecoverySummary", "recover"]

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class RecoverySummary:
    """What one recovery pass did — the reconciliation summary queue §10 asks for."""

    requeued: tuple[str, ...] = ()
    failed: tuple[str, ...] = ()
    cancelled: tuple[str, ...] = ()
    reevaluated: tuple[str, ...] = ()
    aged: int = 0
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def touched(self) -> int:
        """How many jobs changed state."""
        return len(self.requeued) + len(self.failed) + len(self.cancelled) + len(self.reevaluated)

    def as_json(self) -> dict[str, Any]:
        """The summary as ``/system/status`` reports it."""
        return {
            "requeued": list(self.requeued),
            "failed": list(self.failed),
            "cancelled": list(self.cancelled),
            "reevaluated": list(self.reevaluated),
            "aged": self.aged,
            "touched": self.touched,
        }


def recover(
    database: Database,
    sink: JobEventSink,
    *,
    now: datetime,
    owner_prefix: str,
    queue_settings: QueueSettings,
    reevaluate: Callable[[datetime], tuple[str, ...]] | None = None,
) -> RecoverySummary:
    """Run queue §10's recovery pass.

    Args:
        database: The application's database handle.
        sink: Where the events go.
        now: The recovery instant.
        owner_prefix: This process's lease owner prefix; any other owner is dead.
        queue_settings: The ageing policy for step 5.
        reevaluate: The scheduler's ``waiting_resources`` re-evaluation, or ``None`` to skip
            step 4 (a caller with no telemetry to evaluate against).

    Returns:
        The :class:`RecoverySummary`.
    """
    requeued: list[str] = []
    failed: list[str] = []
    cancelled: list[str] = []
    with sink.write(database) as (session, events):
        rows = session.execute(
            select(Job.id, Job.state, Job.idempotent, Job.lease_owner).where(
                Job.state.in_([s.value for s in LEASE_HOLDING_STATES | {JobState.CANCELLING}])
            )
        ).all()
        for job_id, state_value, idempotent, owner in rows:
            if owner is not None and owner.startswith(f"{owner_prefix}/"):
                continue  # one of ours: a live worker holds it
            state = JobState(state_value)
            target = recovery_target(state, idempotent=idempotent)
            if target is None:  # pragma: no cover — every state selected above has a target
                continue
            reason = (
                "recovered_cancelling"
                if target is JobState.CANCELLED
                else "recovered"
                if target is JobState.QUEUED
                else "worker_lost"
            )
            values: dict[str, Any] = {}
            if target is JobState.FAILED:
                values = {
                    "error_code": "WORKER_LOST",
                    "error_text": f"recovered at startup from {state.value}; lease held by {owner}",
                }
            elif target is JobState.CANCELLED:
                values = {"error_code": "GENERATION_CANCELLED"}
            transition(
                session, job_id, current=state, target=target, now=now, reason=reason, values=values
            )
            events.append(
                job_id,
                event_type_for(target),
                now=now,
                message=(
                    f"startup recovery: {state.value} -> {target.value} (lease held by {owner})"
                ),
                data={"reason": reason, "previous_state": state.value, "lease_owner": owner},
            )
            bucket = (
                requeued
                if target is JobState.QUEUED
                else cancelled
                if target is JobState.CANCELLED
                else failed
            )
            bucket.append(job_id)

    reevaluated = tuple(reevaluate(now)) if reevaluate is not None else ()
    aged = ageing_sweep(database, now=now, settings=queue_settings)
    summary = RecoverySummary(
        requeued=tuple(requeued),
        failed=tuple(failed),
        cancelled=tuple(cancelled),
        reevaluated=reevaluated,
        aged=aged,
    )
    logger.info("queue.recovered", extra=summary.as_json())
    return summary
