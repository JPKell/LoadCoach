"""loadcoach.domain.queue_state — the job state machine (queue §2, ADR-0029 §3, ADR-0036).

The transition table below is normative and complete: every legal transition is listed, and a pair
that is not listed is rejected by :func:`check_transition` — and asserted as rejected by a test that
enumerates every pair. Nothing else in the application decides whether a job may move from one
state to another; a service that wants to move a job calls this module first and the database
second.

Pure: no I/O, no clock, no framework. The reasons attached to a transition (``lease_expired``,
``worker_lost``, ``MAX_WAIT_EXCEEDED`` …) are recorded by the caller on the job row and its event;
this module is only about which moves exist.
"""

from __future__ import annotations

from enum import StrEnum
from typing import ClassVar, Final

from baseaicore import SuiteError

__all__ = [
    "ACTIVE_STATES",
    "CANCELLABLE_STATES",
    "IN_FLIGHT_STATES",
    "LEASE_HOLDING_STATES",
    "TERMINAL_STATES",
    "TRANSITIONS",
    "WAITING_STATES",
    "IllegalTransition",
    "JobState",
    "cancel_target",
    "check_transition",
    "event_type_for",
    "is_legal",
    "recovery_target",
    "successors",
]


class JobState(StrEnum):
    """Every state a job can be in, spelled exactly as ``jobs.state`` stores it."""

    QUEUED = "queued"
    LEASED = "leased"
    ADMITTED = "admitted"
    WAITING_RESOURCES = "waiting_resources"
    EXECUTING = "executing"
    VALIDATING = "validating"
    RETRYING = "retrying"
    CANCELLING = "cancelling"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


TERMINAL_STATES: Final[frozenset[JobState]] = frozenset(
    {JobState.COMPLETED, JobState.FAILED, JobState.CANCELLED}
)
"""States with no successor. A terminal job is immutable."""

WAITING_STATES: Final[frozenset[JobState]] = frozenset(
    {JobState.QUEUED, JobState.WAITING_RESOURCES}
)
"""States in which a job holds no lease and is counted as waiting — the ageing sweep's domain."""

LEASE_HOLDING_STATES: Final[frozenset[JobState]] = frozenset(
    {
        JobState.LEASED,
        JobState.ADMITTED,
        JobState.EXECUTING,
        JobState.VALIDATING,
        JobState.RETRYING,
    }
)
"""States in which a worker owns the job under a lease. Lease expiry and startup recovery apply to
all of them uniformly (ADR-0036 §1)."""

IN_FLIGHT_STATES: Final[frozenset[JobState]] = LEASE_HOLDING_STATES | {JobState.CANCELLING}
"""Every state a worker may be actively touching — what the lease keeper renews and the
concurrency limit counts."""

ACTIVE_STATES: Final[frozenset[JobState]] = WAITING_STATES | IN_FLIGHT_STATES
"""Every non-terminal state — what queue depth reports."""

CANCELLABLE_STATES: Final[frozenset[JobState]] = ACTIVE_STATES
"""Cancellation is accepted from every non-terminal state (queue §8); ``cancelling`` accepts it
idempotently."""

TRANSITIONS: Final[frozenset[tuple[JobState, JobState]]] = frozenset(
    {
        # Queue §2's table, verbatim.
        (JobState.QUEUED, JobState.LEASED),
        (JobState.LEASED, JobState.ADMITTED),
        (JobState.LEASED, JobState.WAITING_RESOURCES),
        (JobState.LEASED, JobState.CANCELLING),
        (JobState.WAITING_RESOURCES, JobState.QUEUED),
        (JobState.ADMITTED, JobState.EXECUTING),
        (JobState.EXECUTING, JobState.VALIDATING),
        (JobState.VALIDATING, JobState.COMPLETED),
        (JobState.VALIDATING, JobState.RETRYING),
        (JobState.EXECUTING, JobState.RETRYING),
        (JobState.RETRYING, JobState.ADMITTED),
        (JobState.EXECUTING, JobState.CANCELLING),
        (JobState.VALIDATING, JobState.CANCELLING),
        (JobState.ADMITTED, JobState.CANCELLING),
        (JobState.QUEUED, JobState.CANCELLED),
        (JobState.WAITING_RESOURCES, JobState.CANCELLED),
        (JobState.CANCELLING, JobState.CANCELLED),
        (JobState.EXECUTING, JobState.FAILED),
        (JobState.VALIDATING, JobState.FAILED),
        (JobState.QUEUED, JobState.FAILED),
        (JobState.WAITING_RESOURCES, JobState.FAILED),
        (JobState.LEASED, JobState.QUEUED),
        (JobState.LEASED, JobState.FAILED),
        (JobState.EXECUTING, JobState.QUEUED),
        # ADR-0036: recovery from every lease-holding state, and cancel during backoff.
        (JobState.ADMITTED, JobState.QUEUED),
        (JobState.ADMITTED, JobState.FAILED),
        (JobState.VALIDATING, JobState.QUEUED),
        (JobState.RETRYING, JobState.QUEUED),
        (JobState.RETRYING, JobState.FAILED),
        (JobState.RETRYING, JobState.CANCELLING),
    }
)
"""Every legal ``(current, target)`` pair. Thirty edges; anything else is illegal."""


class IllegalTransition(SuiteError):
    """A move the state machine does not allow. ``details`` names both states."""

    code: ClassVar[str] = "ILLEGAL_TRANSITION"


def is_legal(current: JobState, target: JobState) -> bool:
    """Return whether ``current -> target`` is in the transition table.

    Args:
        current: The job's present state.
        target: The state it would move to.

    Returns:
        ``True`` for a listed edge. A self-transition is never legal; a terminal state has no
        successor at all.
    """
    return (current, target) in TRANSITIONS


def check_transition(current: JobState, target: JobState) -> None:
    """Refuse an illegal transition.

    Args:
        current: The job's present state.
        target: The state it would move to.

    Raises:
        IllegalTransition: ``current -> target`` is not a listed edge — including any move out
            of a terminal state, and any self-transition.
    """
    if not is_legal(current, target):
        raise IllegalTransition(
            f"A job cannot move from {current.value!r} to {target.value!r}.",
            details={"current": current.value, "target": target.value},
        )


def successors(state: JobState) -> frozenset[JobState]:
    """Return every state ``state`` may move to. Empty for a terminal state."""
    return frozenset(target for current, target in TRANSITIONS if current is state)


def recovery_target(state: JobState, *, idempotent: bool) -> JobState | None:
    """Return where lease expiry or startup recovery moves a job in ``state``.

    One rule for every lease-holding state (ADR-0036 §1): idempotent work returns to ``queued``,
    non-idempotent work fails with ``worker_lost``. ``cancelling`` completes to ``cancelled``.

    Args:
        state: The state the job was found in.
        idempotent: The job's declared idempotency.

    Returns:
        The target state, or ``None`` for a state recovery leaves alone (``queued``, a terminal
        state, and ``waiting_resources``, which is re-evaluated against telemetry rather than
        moved by rule).
    """
    if state in LEASE_HOLDING_STATES:
        return JobState.QUEUED if idempotent else JobState.FAILED
    if state is JobState.CANCELLING:
        return JobState.CANCELLED
    return None


def cancel_target(state: JobState) -> JobState | None:
    """Return the state a cancel request moves a job in ``state`` to.

    Args:
        state: The job's present state.

    Returns:
        ``cancelled`` for a job that holds no lease and can stop at once; ``cancelling`` for a job
        a worker is touching, which stops at the next chunk boundary; ``None`` for a terminal job
        (``JOB_NOT_CANCELLABLE``) and for a job already ``cancelling`` (idempotent — nothing to
        do).
    """
    if state in WAITING_STATES:
        return JobState.CANCELLED
    if state in LEASE_HOLDING_STATES:
        return JobState.CANCELLING
    return None


def event_type_for(state: JobState) -> str:
    """Return the ``job_events.event_type`` recorded when a job enters ``state`` (api.md §5)."""
    return f"job.{state.value}"
