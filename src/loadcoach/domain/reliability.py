"""loadcoach.domain.reliability — production statistics with stated bounds (routing §6, §11).

Every completed attempt on a ``(model, task_profile)`` pair, and every piece of caller feedback
about one of its jobs, feeds the statistics this module computes: counts by outcome, rates, latency
percentiles, throughput, acceptance and quality — per rolling window (``7d``, ``30d``, ``all``,
data model §2). Three consumers read them: the ``reliability_factor`` (routing §6), the circuit
breaker (queue §7) and regression detection (routing §11).

**A statistic with no stated bound is a bug.** Every rate, mean and percentile here carries the
sample count that produced it and a documented minimum below which it is *absent* — ``None`` with a
reason — never a plausible-looking number over three attempts (ADR-0016 rule 6). The reliability
factor is exactly ``1.0`` below :data:`PRODUCTION_MINIMUM_SAMPLES` and its record says why.

Pure: nothing here reads a clock or a database. Instants are supplied, and the service layer that
reads ``job_attempts`` and ``feedback`` folds them in through :class:`ReliabilityLedger` or
:func:`compute_stats` — the two are required to agree on every field, which is the property
``tests/unit/test_reliability_math.py`` checks over random sequences.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Final, Literal

__all__ = [
    "EDITED_ACCEPTANCE_CREDIT",
    "FEEDBACK_WEIGHT",
    "MINIMUM_MEAN_SAMPLES",
    "MINIMUM_PERCENTILE_SAMPLES",
    "MINIMUM_RATE_SAMPLES",
    "PRODUCTION_MINIMUM_SAMPLES",
    "REGRESSION_MINIMUM_DROP",
    "REGRESSION_MINIMUM_SAMPLES",
    "REGRESSION_Z_THRESHOLD",
    "RELIABILITY_FACTOR_FLOOR",
    "SUCCESS_OUTCOMES",
    "FACTOR_WINDOWS",
    "WINDOWS",
    "WINDOW_7D",
    "WINDOW_30D",
    "WINDOW_ALL",
    "AttemptOutcome",
    "FeedbackOutcome",
    "RegressionVerdict",
    "ReliabilityFactor",
    "ReliabilityLedger",
    "Statistic",
    "Window",
    "WindowStats",
    "acceptance_credit",
    "compute_stats",
    "counts_as_success",
    "detect_regression",
    "in_window",
    "neutral_factor",
    "percentile",
    "reliability_factor",
    "window_named",
]

# --------------------------------------------------------------------------------- constants

PRODUCTION_MINIMUM_SAMPLES: Final = 20
"""How many counted attempts a window needs before production evidence is used for *routing* at
all (routing §5.1 "used as soon as the minimum sample count is reached"; LCX4). Below it the
reliability factor is exactly 1.0 — absent, not weakly present — and the explanation says so. A
rate over three attempts is noise wearing a number's clothes."""

MINIMUM_RATE_SAMPLES: Final = 5
"""Below five counted attempts (or five pieces of feedback) a rate is not reported. One failure out
of one is 100 %, and a page that prints it is lying with a true number."""

MINIMUM_MEAN_SAMPLES: Final = 5
"""Below five measurements a mean is not reported, for the same reason as a rate."""

MINIMUM_PERCENTILE_SAMPLES: Final = 20
"""Below twenty latency samples no percentile is reported: the nearest-rank p95 of fewer than
twenty values is the maximum, which is a different statistic wearing the wrong name."""

RELIABILITY_FACTOR_FLOOR: Final = 0.5
"""Routing §6 bounds the factor to ``0.5–1.0``: production evidence can at most halve a
candidate's final score. It deprioritizes; exclusion is the circuit breaker's job (queue §7)."""

FEEDBACK_WEIGHT: Final = 0.5
"""How much caller feedback can move the factor on its own (routing §11: "folded in with its own
weight"). At 0.5, unanimously rejected output pulls the factor halfway to the floor and no
further — a caller's verdict on usefulness is real evidence, but it is not LoadCoach's own
validation and it should not be able to bury a model that is answering correctly."""

EDITED_ACCEPTANCE_CREDIT: Final = 0.5
"""An accepted-but-edited output earns half an acceptance: it was useful enough to keep and not
good enough to keep as it was."""

REGRESSION_MINIMUM_SAMPLES: Final = 20
"""Both the recent window and the baseline need this many counted attempts before a regression
verdict is attempted; below it the verdict is ``insufficient_samples``, never ``stable``."""

REGRESSION_MINIMUM_DROP: Final = 0.15
"""The smallest drop in validated-success rate that counts as a regression, in absolute terms.
Fifteen points is the floor because a model with thousands of attempts can show a statistically
significant two-point drift that no operator should be paged for — significance alone is not
severity."""

REGRESSION_Z_THRESHOLD: Final = 2.0
"""The two-proportion z-score a drop must reach as well: about 97.7 % one-sided, i.e. a drop this
large arises from noise on a stable model roughly one evaluation in forty. Measured against the
model's own baseline (routing §11), never against other models."""

SUCCESS_OUTCOMES: Final[frozenset[str]] = frozenset({"completed", "validation_failed"})
"""``job_attempts.outcome`` values where the provider answered. A validation failure is a
*quality* failure, not a *reliability* one: the model spoke, and what it said was checked. This is
the same rule the circuit breaker applies (queue §7), so the two never disagree about what a
failure is."""

_TIMEOUT_OUTCOMES: Final[frozenset[str]] = frozenset({"timeout"})
_CANCELLED_OUTCOMES: Final[frozenset[str]] = frozenset({"cancelled"})
_VALIDATED_OUTCOME: Final = "completed"


# ----------------------------------------------------------------------------------- windows


@dataclass(frozen=True, slots=True)
class Window:
    """One rolling window (data model §2).

    Attributes:
        name: ``"7d"``, ``"30d"`` or ``"all"`` — the ``reliability_stats.window`` value.
        seconds: How far back the window reaches, or ``None`` for all time.
    """

    name: str
    seconds: float | None

    def horizon(self, now: datetime) -> datetime | None:
        """The instant before which a sample no longer counts, or ``None`` for all time."""
        return None if self.seconds is None else now - timedelta(seconds=self.seconds)


WINDOW_7D: Final = Window("7d", 7 * 86_400.0)
WINDOW_30D: Final = Window("30d", 30 * 86_400.0)
WINDOW_ALL: Final = Window("all", None)
WINDOWS: Final[tuple[Window, ...]] = (WINDOW_7D, WINDOW_30D, WINDOW_ALL)
"""Every window ``reliability_stats`` stores, freshest first."""

FACTOR_WINDOWS: Final[tuple[Window, ...]] = (WINDOW_7D, WINDOW_30D)
"""The windows :func:`reliability_factor` may use, freshest first. ``all`` is deliberately not one
of them: a lightly used model would otherwise carry one bad day in its factor for ever, which is
the phase's named failure mode wearing arithmetic instead of a breaker. Thirty days is the bound
on adaptation; ``all`` remains for the page and as the regression baseline."""


def window_named(name: str) -> Window:
    """Return the window called ``name``.

    Raises:
        ValueError: ``name`` is not one of the three.
    """
    for window in WINDOWS:
        if window.name == name:
            return window
    message = f"unknown reliability window {name!r}; expected one of {[w.name for w in WINDOWS]}"
    raise ValueError(message)


def in_window(at: datetime, *, window: Window, now: datetime) -> bool:
    """Whether a sample taken at ``at`` counts in ``window`` as of ``now``.

    A sample counts when ``horizon < at <= now``. The lower bound is strict and the upper bound
    inclusive, on both the fold and the from-scratch paths — the boundary is the one place the two
    could disagree, so it is defined once. A sample in the future (clock skew, a restored backup)
    never counts.
    """
    if at > now:
        return False
    horizon = window.horizon(now)
    return horizon is None or at > horizon


# ----------------------------------------------------------------------------------- samples


@dataclass(frozen=True, slots=True)
class AttemptOutcome:
    """One completed attempt, as the statistics see it.

    Attributes:
        at: When it completed (``job_attempts.completed_at``). An attempt still in flight has no
            instant and is not a sample.
        outcome: ``job_attempts.outcome`` — ``completed``, ``validation_failed``,
            ``provider_error``, ``timeout``, ``context_exceeded`` or ``cancelled``.
        latency_ms: Provider time for the attempt, or ``None`` when it was not measured.
        output_tokens: Tokens the provider produced, or ``None`` when it did not report them.
    """

    at: datetime
    outcome: str
    latency_ms: int | None = None
    output_tokens: int | None = None

    @property
    def counted(self) -> bool:
        """Whether this attempt says anything about the model. A cancellation does not."""
        return self.outcome not in _CANCELLED_OUTCOMES

    @property
    def tokens_per_second(self) -> float | None:
        """Provider-time throughput, or ``None`` when either input is missing or zero."""
        if self.latency_ms is None or self.latency_ms <= 0 or self.output_tokens is None:
            return None
        return self.output_tokens / (self.latency_ms / 1000.0)


@dataclass(frozen=True, slots=True)
class FeedbackOutcome:
    """One caller's verdict on one job (api.md §6).

    Attributes:
        at: When the feedback was last updated.
        accepted: Whether the caller used the output.
        quality_score: The caller's optional score in ``[0, 1]``.
        edited: Whether the caller changed the output before using it.
        validation_passed: The caller's *own* validation verdict, when it has one. ``False`` is a
            rejection whatever ``accepted`` says: output that failed the caller's check was not
            usable as delivered.
    """

    at: datetime
    accepted: bool
    quality_score: float | None = None
    edited: bool = False
    validation_passed: bool | None = None


def counts_as_success(outcome: str) -> bool:
    """Whether an attempt outcome means the provider answered (queue §7's failure rule)."""
    return outcome in SUCCESS_OUTCOMES


def acceptance_credit(feedback: FeedbackOutcome) -> float:
    """How much of one acceptance a piece of feedback is worth: 1, 0.5 (edited) or 0.

    Rejected output, and output the caller's own validation failed, earn nothing.
    """
    if feedback.validation_passed is False or not feedback.accepted:
        return 0.0
    return EDITED_ACCEPTANCE_CREDIT if feedback.edited else 1.0


def percentile(values: Sequence[int], q: float) -> int:
    """The nearest-rank ``q``-th percentile of ``values`` (``0 < q <= 1``).

    Nearest rank rather than interpolation: the answer is always a latency that actually happened,
    which is what a person reading "p95 = 1 900 ms" assumes.

    Raises:
        ValueError: ``values`` is empty, or ``q`` is outside ``(0, 1]``.
    """
    if not values:
        message = "percentile() of an empty sequence is undefined"
        raise ValueError(message)
    if not 0.0 < q <= 1.0:
        message = f"percentile q must be in (0, 1]; got {q}"
        raise ValueError(message)
    ordered = sorted(values)
    rank = math.ceil(q * len(ordered))
    return ordered[rank - 1]


# -------------------------------------------------------------------------------- statistics


@dataclass(frozen=True, slots=True)
class Statistic:
    """One reported number with the sample count behind it and the bound it had to clear.

    ``value`` is ``None`` when ``samples < minimum`` — absent with a reason, never a plausible
    number (ADR-0016 rules 3, 5 and 6).
    """

    value: float | None
    samples: int
    minimum: int

    @property
    def reason(self) -> str | None:
        """Why the value is absent, or ``None`` when it is present."""
        if self.value is not None:
            return None
        return f"{self.samples} sample(s); {self.minimum} needed"

    def as_json(self) -> dict[str, Any]:
        """``{"value", "samples", "minimum", "reason"}``."""
        return {
            "value": self.value,
            "samples": self.samples,
            "minimum": self.minimum,
            "reason": self.reason,
        }


def _bounded(value: float | None, samples: int, minimum: int) -> Statistic:
    return Statistic(value=None if samples < minimum else value, samples=samples, minimum=minimum)


@dataclass(frozen=True, slots=True)
class WindowStats:
    """The ``reliability_stats`` row for one ``(model, task_profile, window)`` (data model §2).

    Raw counts are stored; every rate is derived through a :class:`Statistic` that carries its
    sample count and bound. ``attempts`` includes cancellations; ``counted`` excludes them, and is
    the denominator of every rate — a cancelled attempt says nothing about the model.

    Attributes:
        window: ``"7d"``, ``"30d"`` or ``"all"``.
        attempts: Every attempt in the window, cancellations included.
        successes: Attempts where the provider answered (:data:`SUCCESS_OUTCOMES`).
        validation_passes: Attempts that answered *and* passed validation (``completed``).
        errors: Provider and context errors — everything that is neither a success, a timeout
            nor a cancellation.
        timeouts: Provider timeouts.
        cancellations: Cancelled attempts.
        latency_count: Attempts carrying a ``latency_ms``.
        p50_latency_ms: Nearest-rank median provider time, or ``None`` below the bound.
        p95_latency_ms: Nearest-rank 95th percentile, or ``None`` below the bound.
        output_token_count: Attempts carrying ``output_tokens``.
        mean_output_tokens: Their mean, or ``None`` below the bound.
        tokens_per_second_count: Attempts where throughput could be computed.
        mean_tokens_per_second: Their mean throughput, or ``None`` below the bound.
        feedback_count: Feedback records in the window.
        acceptance_rate: Mean :func:`acceptance_credit`, or ``None`` below the bound.
        quality_count: Feedback records carrying a ``quality_score``.
        mean_quality: Their mean, or ``None`` below the bound.
    """

    window: str
    attempts: int = 0
    successes: int = 0
    validation_passes: int = 0
    errors: int = 0
    timeouts: int = 0
    cancellations: int = 0
    latency_count: int = 0
    p50_latency_ms: int | None = None
    p95_latency_ms: int | None = None
    output_token_count: int = 0
    mean_output_tokens: float | None = None
    tokens_per_second_count: int = 0
    mean_tokens_per_second: float | None = None
    feedback_count: int = 0
    acceptance_rate: float | None = None
    quality_count: int = 0
    mean_quality: float | None = None

    @property
    def counted(self) -> int:
        """Attempts that say something about the model: everything but cancellations."""
        return self.attempts - self.cancellations

    @property
    def failures(self) -> int:
        """Errors plus timeouts — the breaker's failure count (queue §7)."""
        return self.errors + self.timeouts

    def success_rate(self) -> Statistic:
        """Answered attempts over counted attempts."""
        return _bounded(
            None if self.counted == 0 else self.successes / self.counted,
            self.counted,
            MINIMUM_RATE_SAMPLES,
        )

    def validation_pass_rate(self) -> Statistic:
        """Validated attempts over *answered* attempts — a quality rate, not a reliability one."""
        return _bounded(
            None if self.successes == 0 else self.validation_passes / self.successes,
            self.successes,
            MINIMUM_RATE_SAMPLES,
        )

    def validated_success_rate(self) -> Statistic:
        """Validated attempts over counted attempts: the one number regression detection watches.

        Answered-and-correct over everything the model was asked, so a model that starts erroring
        and one that starts answering wrongly both move it.
        """
        return _bounded(
            None if self.counted == 0 else self.validation_passes / self.counted,
            self.counted,
            MINIMUM_RATE_SAMPLES,
        )

    def error_rate(self) -> Statistic:
        """Errors over counted attempts."""
        return _bounded(
            None if self.counted == 0 else self.errors / self.counted,
            self.counted,
            MINIMUM_RATE_SAMPLES,
        )

    def timeout_rate(self) -> Statistic:
        """Timeouts over counted attempts."""
        return _bounded(
            None if self.counted == 0 else self.timeouts / self.counted,
            self.counted,
            MINIMUM_RATE_SAMPLES,
        )

    def acceptance(self) -> Statistic:
        """The stored acceptance rate with its sample count and bound."""
        return Statistic(self.acceptance_rate, self.feedback_count, MINIMUM_RATE_SAMPLES)

    def quality(self) -> Statistic:
        """The stored mean quality with its sample count and bound."""
        return Statistic(self.mean_quality, self.quality_count, MINIMUM_MEAN_SAMPLES)

    def p50(self) -> Statistic:
        """The stored median latency with its sample count and bound."""
        return Statistic(self.p50_latency_ms, self.latency_count, MINIMUM_PERCENTILE_SAMPLES)

    def p95(self) -> Statistic:
        """The stored p95 latency with its sample count and bound."""
        return Statistic(self.p95_latency_ms, self.latency_count, MINIMUM_PERCENTILE_SAMPLES)

    def output_tokens(self) -> Statistic:
        """The stored mean output tokens with its sample count and bound."""
        return Statistic(self.mean_output_tokens, self.output_token_count, MINIMUM_MEAN_SAMPLES)

    def tokens_per_second(self) -> Statistic:
        """The stored mean throughput with its sample count and bound."""
        return Statistic(
            self.mean_tokens_per_second, self.tokens_per_second_count, MINIMUM_MEAN_SAMPLES
        )

    def as_json(self) -> dict[str, Any]:
        """The window as ``GET /reliability`` reports it: counts flat, statistics bounded."""
        return {
            "window": self.window,
            "attempts": self.attempts,
            "counted": self.counted,
            "successes": self.successes,
            "validation_passes": self.validation_passes,
            "errors": self.errors,
            "timeouts": self.timeouts,
            "cancellations": self.cancellations,
            "success_rate": self.success_rate().as_json(),
            "validation_pass_rate": self.validation_pass_rate().as_json(),
            "validated_success_rate": self.validated_success_rate().as_json(),
            "error_rate": self.error_rate().as_json(),
            "timeout_rate": self.timeout_rate().as_json(),
            "p50_latency_ms": self.p50().as_json(),
            "p95_latency_ms": self.p95().as_json(),
            "mean_output_tokens": self.output_tokens().as_json(),
            "mean_tokens_per_second": self.tokens_per_second().as_json(),
            "acceptance_rate": self.acceptance().as_json(),
            "mean_quality": self.quality().as_json(),
        }


def compute_stats(
    attempts: Iterable[AttemptOutcome],
    feedback: Iterable[FeedbackOutcome] = (),
    *,
    window: Window,
    now: datetime,
) -> WindowStats:
    """Compute one window's statistics from scratch.

    Args:
        attempts: Every attempt known for the pair, any order, any age — the window is applied
            here.
        feedback: Every feedback record known for the pair, likewise.
        window: Which window to compute.
        now: The evaluation instant.

    Returns:
        The :class:`WindowStats`, every statistic bounded by its minimum sample count.
    """
    counted_attempts = [a for a in attempts if in_window(a.at, window=window, now=now)]
    counted_feedback = [f for f in feedback if in_window(f.at, window=window, now=now)]

    successes = sum(1 for a in counted_attempts if counts_as_success(a.outcome))
    validation_passes = sum(1 for a in counted_attempts if a.outcome == _VALIDATED_OUTCOME)
    timeouts = sum(1 for a in counted_attempts if a.outcome in _TIMEOUT_OUTCOMES)
    cancellations = sum(1 for a in counted_attempts if a.outcome in _CANCELLED_OUTCOMES)
    errors = len(counted_attempts) - successes - timeouts - cancellations

    latencies = [a.latency_ms for a in counted_attempts if a.latency_ms is not None]
    tokens = [a.output_tokens for a in counted_attempts if a.output_tokens is not None]
    throughputs = [
        tps for tps in (a.tokens_per_second for a in counted_attempts) if tps is not None
    ]
    credits = [acceptance_credit(f) for f in counted_feedback]
    qualities = [f.quality_score for f in counted_feedback if f.quality_score is not None]

    return WindowStats(
        window=window.name,
        attempts=len(counted_attempts),
        successes=successes,
        validation_passes=validation_passes,
        errors=errors,
        timeouts=timeouts,
        cancellations=cancellations,
        latency_count=len(latencies),
        p50_latency_ms=(
            None if len(latencies) < MINIMUM_PERCENTILE_SAMPLES else percentile(latencies, 0.50)
        ),
        p95_latency_ms=(
            None if len(latencies) < MINIMUM_PERCENTILE_SAMPLES else percentile(latencies, 0.95)
        ),
        output_token_count=len(tokens),
        mean_output_tokens=(
            None if len(tokens) < MINIMUM_MEAN_SAMPLES else sum(tokens) / len(tokens)
        ),
        tokens_per_second_count=len(throughputs),
        mean_tokens_per_second=(
            None if len(throughputs) < MINIMUM_MEAN_SAMPLES else sum(throughputs) / len(throughputs)
        ),
        feedback_count=len(credits),
        acceptance_rate=(
            None if len(credits) < MINIMUM_RATE_SAMPLES else sum(credits) / len(credits)
        ),
        quality_count=len(qualities),
        mean_quality=(
            None if len(qualities) < MINIMUM_MEAN_SAMPLES else sum(qualities) / len(qualities)
        ),
    )


@dataclass(frozen=True, slots=True)
class ReliabilityLedger:
    """The samples one window still needs, folded one at a time.

    Incremental recomputation (data model §2) is a fold over events followed by a trim of what
    the window can never count again. Percentiles cannot be maintained from running totals, so
    the ledger keeps the samples themselves — bounded by the window for ``7d`` and ``30d``, and by
    the retained history for ``all``, which is the same bound ``job_attempts`` has. The contract is
    that ``ledger.stats(...)`` equals :func:`compute_stats` over the full original sequence, for
    every field, at every instant; the property test holds it to that.
    """

    window: Window
    attempts: tuple[AttemptOutcome, ...] = field(default=())
    feedback: tuple[FeedbackOutcome, ...] = field(default=())

    def with_attempt(self, attempt: AttemptOutcome) -> ReliabilityLedger:
        """Fold one attempt in."""
        return ReliabilityLedger(self.window, (*self.attempts, attempt), self.feedback)

    def with_feedback(self, feedback: FeedbackOutcome) -> ReliabilityLedger:
        """Fold one feedback record in."""
        return ReliabilityLedger(self.window, self.attempts, (*self.feedback, feedback))

    def trimmed(self, *, now: datetime) -> ReliabilityLedger:
        """Drop every sample that has left the window as of ``now``.

        A sample dated after ``now`` is kept: it is excluded from the statistics until the clock
        reaches it, not forgotten.
        """
        horizon = self.window.horizon(now)
        if horizon is None:
            return self
        return ReliabilityLedger(
            self.window,
            tuple(a for a in self.attempts if a.at > horizon),
            tuple(f for f in self.feedback if f.at > horizon),
        )

    def stats(self, *, now: datetime) -> WindowStats:
        """This window's statistics as of ``now``."""
        return compute_stats(self.attempts, self.feedback, window=self.window, now=now)


# ------------------------------------------------------------------------------------ factor


@dataclass(frozen=True, slots=True)
class ReliabilityFactor:
    """The ``reliability_factor`` (routing §6) with every input it was computed from.

    Attributes:
        value: In ``[RELIABILITY_FACTOR_FLOOR, 1.0]``; exactly ``1.0`` when neutral.
        window: The window the factor came from, or ``None`` when no window had enough samples.
        attempts: Counted attempts in that window (0 when neutral).
        reason: One line for the explanation, always present.
        success_rate: Answered over counted, when the factor is live.
        validation_pass_rate: Validated over answered, when live.
        error_rate: Errors over counted, when live.
        timeout_rate: Timeouts over counted, when live.
        acceptance_rate: Caller acceptance, when at least :data:`MINIMUM_RATE_SAMPLES` feedback
            records exist in the window; ``None`` otherwise, and then it did not move the factor.
        feedback_count: Feedback records in the window.
    """

    value: float
    window: str | None
    attempts: int
    reason: str
    success_rate: float | None = None
    validation_pass_rate: float | None = None
    error_rate: float | None = None
    timeout_rate: float | None = None
    acceptance_rate: float | None = None
    feedback_count: int = 0

    @property
    def neutral(self) -> bool:
        """Whether production evidence played no part in this factor."""
        return self.window is None

    def as_json(self) -> dict[str, Any]:
        """The ``reliability_detail`` object the explanation carries beside ``factors``."""
        return {
            "source": "production",
            "value": self.value,
            "neutral": self.neutral,
            "window": self.window,
            "attempts": self.attempts,
            "minimum_samples": PRODUCTION_MINIMUM_SAMPLES,
            "success_rate": self.success_rate,
            "validation_pass_rate": self.validation_pass_rate,
            "error_rate": self.error_rate,
            "timeout_rate": self.timeout_rate,
            "acceptance_rate": self.acceptance_rate,
            "feedback_count": self.feedback_count,
            "reason": self.reason,
        }


def neutral_factor(attempts_by_window: Mapping[str, int] | None = None) -> ReliabilityFactor:
    """The factor with no production evidence behind it: exactly ``1.0``, and it says why."""
    seen = (
        ""
        if not attempts_by_window
        else (
            "; counted attempts: "
            + ", ".join(f"{name}={count}" for name, count in attempts_by_window.items())
        )
    )
    return ReliabilityFactor(
        value=1.0,
        window=None,
        attempts=0 if not attempts_by_window else max(attempts_by_window.values(), default=0),
        reason=(
            f"neutral: fewer than {PRODUCTION_MINIMUM_SAMPLES} counted attempts in the last "
            f"{' and '.join(w.name for w in FACTOR_WINDOWS)}{seen}"
        ),
    )


def reliability_factor(stats_by_window: Mapping[str, WindowStats]) -> ReliabilityFactor:
    """Compute the factor from the freshest window with enough samples.

    ``factor = floor + (1 - floor) × success_rate × validation_pass_rate × feedback_term``, where
    ``feedback_term = 1 - FEEDBACK_WEIGHT × (1 - acceptance_rate)`` when the window carries at
    least :data:`MINIMUM_RATE_SAMPLES` feedback records and ``1`` otherwise. Every term is in
    ``[0, 1]``, so the product is, and the factor lands in ``[floor, 1]`` without clamping — a
    clamp would hide an input that had escaped its range.

    The windows are searched freshest first (``7d``, then ``30d`` — never ``all``, see
    :data:`FACTOR_WINDOWS`) and the first with at least :data:`PRODUCTION_MINIMUM_SAMPLES` counted
    attempts decides; a model that was busy last month and idle this week is judged on last month
    rather than on nothing, and one that was bad two months ago is judged on nothing.

    Args:
        stats_by_window: The pair's rows keyed by window name; a missing window is an empty one.

    Returns:
        The :class:`ReliabilityFactor`, neutral when no window qualifies.
    """
    counts = {
        w.name: stats_by_window[w.name].counted for w in FACTOR_WINDOWS if w.name in stats_by_window
    }
    for window in FACTOR_WINDOWS:
        stats = stats_by_window.get(window.name)
        if stats is None or stats.counted < PRODUCTION_MINIMUM_SAMPLES:
            continue
        success = stats.successes / stats.counted
        validation = 1.0 if stats.successes == 0 else stats.validation_passes / stats.successes
        acceptance = stats.acceptance_rate if stats.feedback_count >= MINIMUM_RATE_SAMPLES else None
        feedback_term = 1.0 if acceptance is None else 1.0 - FEEDBACK_WEIGHT * (1.0 - acceptance)
        value = RELIABILITY_FACTOR_FLOOR + (1.0 - RELIABILITY_FACTOR_FLOOR) * (
            success * validation * feedback_term
        )
        parts = [
            f"{stats.successes} of {stats.counted} attempts answered ({success:.0%})",
            f"{stats.validation_passes} of {stats.successes} validated ({validation:.0%})",
        ]
        if acceptance is not None:
            parts.append(f"caller acceptance {acceptance:.0%} over {stats.feedback_count} verdicts")
        return ReliabilityFactor(
            value=value,
            window=window.name,
            attempts=stats.counted,
            reason=f"{window.name} window: " + "; ".join(parts) + f" → factor {value:.3f}",
            success_rate=success,
            validation_pass_rate=validation if stats.successes else None,
            error_rate=stats.errors / stats.counted,
            timeout_rate=stats.timeouts / stats.counted,
            acceptance_rate=acceptance,
            feedback_count=stats.feedback_count,
        )
    return neutral_factor(counts)


# -------------------------------------------------------------------------------- regression


@dataclass(frozen=True, slots=True)
class RegressionVerdict:
    """Whether a model's recent validated-success rate has dropped against its own baseline.

    Attributes:
        status: ``"regressed"``, ``"stable"`` or ``"insufficient_samples"``.
        recent_rate: The recent window's validated-success rate, when computable.
        baseline_rate: The rate over everything *before* the recent window, when computable.
        drop: ``baseline_rate - recent_rate`` (positive means worse), when both exist.
        z_score: The two-proportion z-score of the drop, when both exist.
        recent_samples: Counted attempts in the recent window.
        baseline_samples: Counted attempts in the baseline.
        reason: One line for health and the UI.
    """

    status: Literal["regressed", "stable", "insufficient_samples"]
    recent_rate: float | None
    baseline_rate: float | None
    drop: float | None
    z_score: float | None
    recent_samples: int
    baseline_samples: int
    reason: str

    @property
    def regressed(self) -> bool:
        """Whether the verdict is a regression."""
        return self.status == "regressed"

    def as_json(self) -> dict[str, Any]:
        """The record health and ``GET /reliability`` carry."""
        return {
            "status": self.status,
            "recent_rate": self.recent_rate,
            "baseline_rate": self.baseline_rate,
            "drop": self.drop,
            "z_score": self.z_score,
            "recent_samples": self.recent_samples,
            "baseline_samples": self.baseline_samples,
            "minimum_samples": REGRESSION_MINIMUM_SAMPLES,
            "minimum_drop": REGRESSION_MINIMUM_DROP,
            "z_threshold": REGRESSION_Z_THRESHOLD,
            "reason": self.reason,
        }


def detect_regression(*, recent: WindowStats, all_time: WindowStats) -> RegressionVerdict:
    """Compare the recent window against the model's own history before it (routing §11).

    The baseline is ``all_time`` minus ``recent`` — counts subtract exactly because the recent
    window is a subset of all time — so the recent samples do not drag down the baseline they are
    being compared with. A regression needs **both** an absolute drop of at least
    :data:`REGRESSION_MINIMUM_DROP` and a two-proportion z-score of at least
    :data:`REGRESSION_Z_THRESHOLD`; noise with the same underlying rate clears neither, and a tiny
    but significant drift over thousands of attempts clears only the second.

    Args:
        recent: The ``7d`` row.
        all_time: The ``all`` row for the same pair.

    Returns:
        The :class:`RegressionVerdict`; ``insufficient_samples`` below the bound on either side.
    """
    recent_n = recent.counted
    baseline_n = all_time.counted - recent.counted
    baseline_passes = all_time.validation_passes - recent.validation_passes
    if recent_n < REGRESSION_MINIMUM_SAMPLES or baseline_n < REGRESSION_MINIMUM_SAMPLES:
        return RegressionVerdict(
            status="insufficient_samples",
            recent_rate=None if recent_n == 0 else recent.validation_passes / recent_n,
            baseline_rate=None if baseline_n <= 0 else baseline_passes / baseline_n,
            drop=None,
            z_score=None,
            recent_samples=recent_n,
            baseline_samples=max(baseline_n, 0),
            reason=(
                f"not evaluated: {recent_n} recent and {max(baseline_n, 0)} baseline attempts; "
                f"{REGRESSION_MINIMUM_SAMPLES} of each needed"
            ),
        )
    recent_rate = recent.validation_passes / recent_n
    baseline_rate = baseline_passes / baseline_n
    drop = baseline_rate - recent_rate
    pooled = (recent.validation_passes + baseline_passes) / (recent_n + baseline_n)
    variance = pooled * (1.0 - pooled) * (1.0 / recent_n + 1.0 / baseline_n)
    z_score = 0.0 if variance == 0.0 else drop / math.sqrt(variance)
    regressed = drop >= REGRESSION_MINIMUM_DROP and z_score >= REGRESSION_Z_THRESHOLD
    summary = (
        f"validated success {recent_rate:.0%} over {recent_n} recent attempts vs "
        f"{baseline_rate:.0%} over {baseline_n} before (drop {drop:+.0%}, z {z_score:.2f})"
    )
    return RegressionVerdict(
        status="regressed" if regressed else "stable",
        recent_rate=recent_rate,
        baseline_rate=baseline_rate,
        drop=drop,
        z_score=z_score,
        recent_samples=recent_n,
        baseline_samples=baseline_n,
        reason=("regression: " if regressed else "stable: ") + summary,
    )
