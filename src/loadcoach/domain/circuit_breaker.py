"""loadcoach.domain.circuit_breaker — the per-model breaker over a window of attempt outcomes.

Queue §7: a model exceeding a failure-rate threshold within a window is deprioritized
(``recently_failing``) and excluded from candidacy until a cool-down elapses, after which it is
re-probed with a single job. The open state, its reason and its expiry are visible in the routing
explanation of any decision that skipped the model.

The *mechanism* is this module's: a state machine per model — ``closed``, ``open``,
``half_open`` — driven by samples the caller supplies. Phase 5 feeds it ``job_attempts`` outcomes
in the window; Phase 7 will drive it from ``reliability_stats`` and add the re-probe prompt record.
The input is a sequence of ``(instant, succeeded)`` pairs precisely so that swap costs nothing
here. Thresholds are named constants with a reason each, the same way routing's priors are.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, replace
from datetime import timedelta
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Final

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence
    from datetime import datetime

__all__ = [
    "DEFAULT_COOLDOWN_SECONDS",
    "DEFAULT_FAILURE_RATE",
    "DEFAULT_MIN_SAMPLES",
    "DEFAULT_WINDOW_SECONDS",
    "AttemptSample",
    "BreakerState",
    "BreakerVerdict",
    "CircuitBreakers",
    "evaluate",
]

DEFAULT_WINDOW_SECONDS: Final = 600.0
"""Ten minutes of outcomes decide the state: long enough that one bad minute is a blip, short
enough that a model that recovered is not punished for an hour."""

DEFAULT_MIN_SAMPLES: Final = 5
"""Below five attempts a failure rate is noise, not evidence — one failure out of one is 100%."""

DEFAULT_FAILURE_RATE: Final = 0.5
"""Half of the window's attempts failing opens the breaker."""

DEFAULT_COOLDOWN_SECONDS: Final = 300.0
"""Five minutes excluded before one probe is allowed through."""


class BreakerState(StrEnum):
    """The three states."""

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass(frozen=True, slots=True)
class AttemptSample:
    """One attempt's outcome on one model."""

    at: datetime
    succeeded: bool


@dataclass(frozen=True, slots=True)
class BreakerVerdict:
    """A model's breaker state and the numbers behind it.

    Attributes:
        canonical_id: The model.
        state: The state.
        failure_rate: Failures over attempts in the window, or ``None`` with no attempts.
        samples: Attempts in the window.
        opened_at: When the breaker last opened, or ``None`` if it never did.
        expires_at: When the cool-down ends, or ``None`` when not open.
        reason: One line for the UI and the explanation.
        probe_in_flight: Whether a half-open probe has been allowed through and not yet reported.
        closed_at: When a successful probe last closed the breaker, or ``None``. Samples before
            it no longer count — otherwise the failures that opened it would re-open it at once.
        probe_started_at: When the probe was let through, or ``None``. A probe that has not
            reported within one cool-down is presumed lost (a crashed worker, a lease that
            expired) and released, so a model is never excluded for ever by a probe nobody will
            finish — the phase's own named failure mode, in its quietest form.
    """

    canonical_id: str
    state: BreakerState
    failure_rate: float | None
    samples: int
    opened_at: datetime | None
    expires_at: datetime | None
    reason: str
    probe_in_flight: bool = False
    closed_at: datetime | None = None
    probe_started_at: datetime | None = None

    @property
    def excludes(self) -> bool:
        """Whether routing must skip the model right now."""
        return self.state is BreakerState.OPEN or (
            self.state is BreakerState.HALF_OPEN and self.probe_in_flight
        )

    def as_json(self) -> dict[str, Any]:
        """The status/explanation record."""
        from baseaicore.timeutil import to_rfc3339

        return {
            "canonical_id": self.canonical_id,
            "state": self.state.value,
            "failure_rate": self.failure_rate,
            "samples": self.samples,
            "opened_at": None if self.opened_at is None else to_rfc3339(self.opened_at),
            "expires_at": None if self.expires_at is None else to_rfc3339(self.expires_at),
            "reason": self.reason,
            "probe_in_flight": self.probe_in_flight,
            "probe_started_at": (
                None if self.probe_started_at is None else to_rfc3339(self.probe_started_at)
            ),
        }


def evaluate(
    canonical_id: str,
    samples: Sequence[AttemptSample],
    *,
    now: datetime,
    previous: BreakerVerdict | None = None,
    window_seconds: float = DEFAULT_WINDOW_SECONDS,
    min_samples: int = DEFAULT_MIN_SAMPLES,
    failure_rate_threshold: float = DEFAULT_FAILURE_RATE,
    cooldown_seconds: float = DEFAULT_COOLDOWN_SECONDS,
) -> BreakerVerdict:
    """Compute a model's next verdict from its samples and its previous verdict.

    ``closed -> open`` when the window holds at least ``min_samples`` attempts and their failure
    rate reaches the threshold. ``open -> half_open`` when the cool-down has elapsed. In
    ``half_open`` the first attempt after the opening decides: success closes, failure re-opens
    with a fresh cool-down. A probe let through more than ``cooldown_seconds`` ago without an
    attempt reporting is released, so the next job may probe instead. Samples older than the
    window are ignored.

    Args:
        canonical_id: The model.
        samples: Its attempt outcomes, any order.
        now: The evaluation instant.
        previous: Its last verdict, or ``None`` on first sight.
        window_seconds: How far back samples count.
        min_samples: How many attempts a rate needs before it means anything.
        failure_rate_threshold: The rate that opens the breaker.
        cooldown_seconds: How long an open breaker excludes the model.

    Returns:
        The new :class:`BreakerVerdict`.
    """
    horizon = now - timedelta(seconds=window_seconds)
    closed_at = None if previous is None else previous.closed_at
    origin = horizon if closed_at is None else max(horizon, closed_at)
    recent = sorted((s for s in samples if s.at > origin and s.at <= now), key=lambda s: s.at)
    count = len(recent)
    failures = sum(1 for s in recent if not s.succeeded)
    rate = None if count == 0 else failures / count
    state = BreakerState.CLOSED if previous is None else previous.state
    opened_at = None if previous is None else previous.opened_at
    expires_at = None if previous is None else previous.expires_at
    probe = False if previous is None else previous.probe_in_flight
    probe_started_at = None if previous is None else previous.probe_started_at

    if state is BreakerState.OPEN and expires_at is not None and now >= expires_at:
        state = BreakerState.HALF_OPEN
        probe = False
        probe_started_at = None

    if (
        state is BreakerState.HALF_OPEN
        and probe
        and probe_started_at is not None
        and now >= probe_started_at + timedelta(seconds=cooldown_seconds)
    ):
        probe = False
        probe_started_at = None

    if state is BreakerState.HALF_OPEN and opened_at is not None:
        after = [s for s in recent if s.at > opened_at]
        if after:
            if after[-1].succeeded:
                state = BreakerState.CLOSED
                closed_at = after[-1].at
                recent = [s for s in recent if s.at > closed_at]
                count = len(recent)
                failures = sum(1 for s in recent if not s.succeeded)
                rate = None if count == 0 else failures / count
                opened_at = None
                expires_at = None
                probe = False
                probe_started_at = None
            else:
                state = BreakerState.OPEN
                opened_at = after[-1].at
                expires_at = opened_at + timedelta(seconds=cooldown_seconds)
                probe = False
                probe_started_at = None

    if (
        state is BreakerState.CLOSED
        and rate is not None
        and count >= min_samples
        and rate >= failure_rate_threshold
    ):
        state = BreakerState.OPEN
        opened_at = recent[-1].at
        expires_at = opened_at + timedelta(seconds=cooldown_seconds)
        probe = False

    if state is BreakerState.OPEN:
        reason = (
            f"{failures} of {count} attempts in the last {int(window_seconds)} s failed"
            f" ({rate:.0%}); excluded until {expires_at}"
        )
    elif state is BreakerState.HALF_OPEN:
        reason = (
            "cool-down elapsed; probe in flight"
            if probe
            else "cool-down elapsed; one probe allowed"
        )
    else:
        reason = "closed" if rate is None else f"{failures} of {count} attempts failed ({rate:.0%})"
    return BreakerVerdict(
        canonical_id=canonical_id,
        state=state,
        failure_rate=rate,
        samples=count,
        opened_at=opened_at,
        expires_at=expires_at,
        reason=reason,
        probe_in_flight=probe,
        closed_at=closed_at,
        probe_started_at=probe_started_at,
    )


class CircuitBreakers:
    """The breakers of every model this process has seen fail or succeed. Thread-safe.

    Holds the previous verdicts the state machine needs and nothing else; the samples come from
    the caller on every :meth:`update`, which is what makes the source swappable.
    """

    def __init__(
        self,
        *,
        window_seconds: float = DEFAULT_WINDOW_SECONDS,
        min_samples: int = DEFAULT_MIN_SAMPLES,
        failure_rate_threshold: float = DEFAULT_FAILURE_RATE,
        cooldown_seconds: float = DEFAULT_COOLDOWN_SECONDS,
    ) -> None:
        """Create an empty registry with the given thresholds."""
        self.window_seconds = window_seconds
        self.min_samples = min_samples
        self.failure_rate_threshold = failure_rate_threshold
        self.cooldown_seconds = cooldown_seconds
        self._verdicts: dict[str, BreakerVerdict] = {}
        self._lock = threading.Lock()

    def update(
        self, samples_by_model: Mapping[str, Iterable[AttemptSample]], *, now: datetime
    ) -> dict[str, BreakerVerdict]:
        """Re-evaluate every model in ``samples_by_model`` (and every known one) at ``now``."""
        with self._lock:
            names = set(samples_by_model) | set(self._verdicts)
            for canonical_id in names:
                self._verdicts[canonical_id] = evaluate(
                    canonical_id,
                    tuple(samples_by_model.get(canonical_id, ())),
                    now=now,
                    previous=self._verdicts.get(canonical_id),
                    window_seconds=self.window_seconds,
                    min_samples=self.min_samples,
                    failure_rate_threshold=self.failure_rate_threshold,
                    cooldown_seconds=self.cooldown_seconds,
                )
            return dict(self._verdicts)

    def excluded(self) -> frozenset[str]:
        """Canonical IDs routing must skip: open, or half-open with a probe already out."""
        with self._lock:
            return frozenset(cid for cid, v in self._verdicts.items() if v.excludes)

    def allow_probe(self, canonical_id: str, *, now: datetime) -> bool:
        """Let one job through a half-open breaker; ``False`` if it is not half-open or busy.

        Called by the worker at the moment it starts executing on the model — not when routing
        merely ranked it — because a probe marked for a fallback that never runs would exclude
        the model until nothing ever reported.
        """
        with self._lock:
            verdict = self._verdicts.get(canonical_id)
            if verdict is None or verdict.state is not BreakerState.HALF_OPEN:
                return False
            if verdict.probe_in_flight:
                return False
            self._verdicts[canonical_id] = replace(
                verdict,
                probe_in_flight=True,
                probe_started_at=now,
                reason="cool-down elapsed; probe in flight",
            )
            return True

    def release_probe(self, canonical_id: str) -> bool:
        """Give a probe back without a verdict — the attempt was cancelled before it reported.

        Returns:
            ``True`` if a probe was in flight and is now released.
        """
        with self._lock:
            verdict = self._verdicts.get(canonical_id)
            if verdict is None or not verdict.probe_in_flight:
                return False
            self._verdicts[canonical_id] = replace(
                verdict,
                probe_in_flight=False,
                probe_started_at=None,
                reason="cool-down elapsed; one probe allowed",
            )
            return True

    def verdicts(self) -> tuple[BreakerVerdict, ...]:
        """Every known verdict, for status pages."""
        with self._lock:
            return tuple(self._verdicts.values())

    def details(self) -> dict[str, dict[str, Any]]:
        """The excluded models' records, keyed by canonical ID, for the routing rejection."""
        with self._lock:
            return {cid: v.as_json() for cid, v in self._verdicts.items() if v.excludes}
