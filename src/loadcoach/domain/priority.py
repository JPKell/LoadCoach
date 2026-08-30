"""loadcoach.domain.priority — job classes, priority bands and the ageing arithmetic (queue §1, §4).

Everything here is pure arithmetic over values the caller supplies. The ageing *sweep* — the
set-based ``UPDATE`` that keeps ``jobs.effective_priority`` current — lives in the queue service,
and computes exactly what :func:`effective_priority` computes; the two are kept in step by a test
that compares them row for row, so the SQL cannot drift from the definition.
"""

from __future__ import annotations

import math
from enum import StrEnum
from typing import Final

from baseaicore import ValidationError

__all__ = [
    "AGEING_EPSILON_POINTS",
    "BANDS",
    "STARVATION_FRACTION_OF_MAX_WAIT",
    "JobClass",
    "ageing_cap",
    "band_of",
    "base_priority",
    "effective_priority",
    "starvation_threshold_seconds",
]


class JobClass(StrEnum):
    """The four job classes, spelled exactly as ``jobs.class`` stores them."""

    INTERACTIVE = "interactive"
    NORMAL = "normal"
    BACKGROUND = "background"
    BATCH = "batch"


BANDS: Final[dict[JobClass, tuple[int, int]]] = {
    JobClass.INTERACTIVE: (800, 999),
    JobClass.NORMAL: (400, 799),
    JobClass.BACKGROUND: (100, 399),
    JobClass.BATCH: (0, 99),
}
"""Inclusive ``(bottom, top)`` priority band per class (queue §1). A caller may choose a priority
within its class's band; the band itself is not escapable."""

AGEING_EPSILON_POINTS: Final[float] = 1e-6
"""Added before flooring the aged points, in SQL and here alike.

SQLite's ``julianday`` arithmetic carries a few tens of microseconds of floating-point error, so at
an exact minute boundary ``minutes x rate`` can land at 0.9999997 and floor one point low. One
millionth of a point is sixty microseconds at one point per minute — far below the sweep's 30 s
granularity — and it makes the set-based statement and this function agree on every row.
"""

STARVATION_FRACTION_OF_MAX_WAIT: Final[float] = 0.5
"""A job counts as starving once it has waited this fraction of its own ``max_wait_seconds``.

Queue §4 names a starvation counter of "jobs waiting beyond a threshold" and no threshold. Half of
the job's absolute bound is the honest choice among the available numbers: it is the point at which
health should say *degraded* before any job actually fails with ``MAX_WAIT_EXCEEDED``, and it needs
no setting of its own. The ageing horizon was considered and rejected — under the shipped policy a
``background`` job reaches its cap after 399 minutes, long after its 60-minute ``max_wait``, so a
counter defined that way could never be non-zero on a default install.
"""


def band_of(job_class: JobClass) -> tuple[int, int]:
    """Return the inclusive ``(bottom, top)`` band for ``job_class``."""
    return BANDS[job_class]


def base_priority(job_class: JobClass, requested: int | None = None) -> int:
    """Resolve the base priority a job is enqueued with.

    Args:
        job_class: The job's class.
        requested: The caller's priority within the band, or ``None`` for the band's bottom —
            the class alone is the ordinary way to submit; a number is for a caller that wants to
            order its own work within its class.

    Returns:
        ``requested`` when given, else the band's bottom.

    Raises:
        ValidationError: ``requested`` lies outside the class's band. The band is not escapable
            (queue §1): a ``background`` job cannot be submitted at an ``interactive`` priority.
    """
    bottom, top = band_of(job_class)
    if requested is None:
        return bottom
    if requested < bottom or requested > top:
        raise ValidationError(
            f"Priority {requested} is outside the {job_class.value!r} band {bottom}-{top}.",
            details={
                "field": "priority",
                "job_class": job_class.value,
                "requested": requested,
                "band": [bottom, top],
            },
        )
    return requested


def ageing_cap(job_class: JobClass, *, overflow_allowance: int) -> int:
    """Return the highest effective priority ageing can reach: the band's top plus the overflow.

    Args:
        job_class: The job's class.
        overflow_allowance: ``queue.overflow_allowance``. The default (100) lets an aged
            ``background`` job outrank a fresh ``normal`` one and never a fresh ``interactive``
            one (queue §4).
    """
    return band_of(job_class)[1] + overflow_allowance


def effective_priority(
    *,
    base: int,
    job_class: JobClass,
    waiting_seconds: float,
    ageing_priority_per_minute: float,
    overflow_allowance: int,
) -> int:
    """Compute queue §4's ``effective_priority`` for one job.

    ``base + floor(waiting_minutes x ageing_priority_per_minute)``, capped at the class band's top
    plus ``overflow_allowance``. ``waiting_seconds`` is measured from ``queued_at``, so time in
    ``waiting_resources`` counts (ADR-0029 §1).

    Args:
        base: The job's ``base_priority``.
        job_class: The job's class, which decides the cap.
        waiting_seconds: ``now - queued_at``. Negative values (a clock that stepped backwards)
            age nothing rather than lowering the priority below its base.
        ageing_priority_per_minute: ``queue.ageing_priority_per_minute``.
        overflow_allowance: ``queue.overflow_allowance``.

    Returns:
        The effective priority — never below ``base``, never above the cap.
    """
    minutes = max(waiting_seconds, 0.0) / 60.0
    aged = base + math.floor(minutes * ageing_priority_per_minute + AGEING_EPSILON_POINTS)
    return min(aged, ageing_cap(job_class, overflow_allowance=overflow_allowance))


def starvation_threshold_seconds(max_wait_seconds: int) -> float:
    """Return how long a job may wait before the starvation counter counts it.

    Args:
        max_wait_seconds: The job's own ``max_wait_seconds`` (the configured default, or the
            caller's override).

    Returns:
        :data:`STARVATION_FRACTION_OF_MAX_WAIT` of the bound.
    """
    return max_wait_seconds * STARVATION_FRACTION_OF_MAX_WAIT
