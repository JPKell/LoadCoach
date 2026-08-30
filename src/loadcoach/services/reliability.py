"""loadcoach.services.reliability — ``reliability_stats`` from ``job_attempts`` and ``feedback``.

The statistics are :mod:`loadcoach.domain.reliability`'s; this module is the read-and-write
around them. **Incremental recomputation** (data model §2) is per ``(model, task_profile)``
pair: every terminal attempt and every feedback record refreshes the three window rows of the one
pair it touched, from that pair's raw rows, rather than every pair from everything. A full
recomputation (:func:`recompute_all`) walks every pair the same way, so the two agree by
construction — and ``tests/integration/test_feedback_affects_routing.py`` holds them to it over
a random sequence of attempts.

The circuit breaker's samples come from here too (:func:`breaker_samples`), classified by the
same rule the statistics use, so the breaker and the Reliability page never disagree about what
a failure is (P5-9's ``breaker_source`` seam).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from weightsdb import upsert

from loadcoach.domain.circuit_breaker import AttemptSample
from loadcoach.domain.reliability import (
    WINDOWS,
    AttemptOutcome,
    FeedbackOutcome,
    RegressionVerdict,
    ReliabilityFactor,
    WindowStats,
    compute_stats,
    counts_as_success,
    detect_regression,
    reliability_factor,
)
from loadcoach.infrastructure.db.models import (
    Feedback,
    Job,
    JobAttempt,
    Model,
    ReliabilityStat,
)

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping
    from datetime import datetime

    from sqlalchemy.orm import Session

    from loadcoach.domain.circuit_breaker import BreakerVerdict
    from loadcoach.services.database import Database

__all__ = [
    "ReliabilityEntry",
    "attempt_outcomes",
    "breaker_samples",
    "factors_for_task",
    "feedback_outcomes",
    "known_pairs",
    "record_breaker_verdicts",
    "recompute_all",
    "recompute_pair",
    "regression_warnings",
    "reliability_report",
    "stats_for",
]


def attempt_outcomes(
    session: Session, *, model_id: str, task_profile_id: str
) -> list[AttemptOutcome]:
    """Every completed attempt on ``model_id`` for jobs of ``task_profile_id``."""
    rows = session.execute(
        select(
            JobAttempt.completed_at,
            JobAttempt.outcome,
            JobAttempt.provider_ms,
            JobAttempt.output_tokens,
        )
        .join(Job, Job.id == JobAttempt.job_id)
        .where(
            JobAttempt.model_id == model_id,
            Job.task_profile_id == task_profile_id,
            JobAttempt.completed_at.is_not(None),
        )
    ).all()
    return [
        AttemptOutcome(
            at=completed_at, outcome=outcome, latency_ms=provider_ms, output_tokens=output_tokens
        )
        for completed_at, outcome, provider_ms, output_tokens in rows
        if completed_at is not None
    ]


def feedback_outcomes(
    session: Session, *, model_id: str, task_profile_id: str
) -> list[FeedbackOutcome]:
    """Every feedback record on a job that ran on ``model_id`` for ``task_profile_id``."""
    rows = session.execute(
        select(
            Feedback.updated_at,
            Feedback.accepted,
            Feedback.quality_score,
            Feedback.edited,
            Feedback.validation_passed,
        )
        .join(Job, Job.id == Feedback.job_id)
        .where(Job.selected_model_id == model_id, Job.task_profile_id == task_profile_id)
    ).all()
    return [
        FeedbackOutcome(
            at=updated_at,
            accepted=accepted,
            quality_score=quality_score,
            edited=edited,
            validation_passed=validation_passed,
        )
        for updated_at, accepted, quality_score, edited, validation_passed in rows
    ]


def _write_windows(
    session: Session,
    *,
    model_id: str,
    task_profile_id: str,
    stats: Iterable[WindowStats],
    now: datetime,
) -> None:
    for window in stats:
        upsert(
            session,
            ReliabilityStat,
            {
                "model_id": model_id,
                "task_profile_id": task_profile_id,
                "window": window.window,
                "attempts": window.attempts,
                "successes": window.successes,
                "validation_passes": window.validation_passes,
                "errors": window.errors,
                "timeouts": window.timeouts,
                "cancellations": window.cancellations,
                "latency_count": window.latency_count,
                "p50_latency_ms": window.p50_latency_ms,
                "p95_latency_ms": window.p95_latency_ms,
                "output_token_count": window.output_token_count,
                "mean_output_tokens": window.mean_output_tokens,
                "tokens_per_second_count": window.tokens_per_second_count,
                "mean_tokens_per_second": window.mean_tokens_per_second,
                "feedback_count": window.feedback_count,
                "acceptance_rate": window.acceptance_rate,
                "quality_count": window.quality_count,
                "mean_quality": window.mean_quality,
                "updated_at": now,
            },
            index_elements=["model_id", "task_profile_id", "window"],
        )


def recompute_pair(
    database: Database, *, model_id: str, task_profile_id: str, now: datetime
) -> dict[str, WindowStats]:
    """Recompute the three window rows for one ``(model, task_profile)`` from its raw rows.

    The incremental path: called once per terminal attempt and once per feedback record, for the
    one pair the event touched. The breaker columns are left alone — they belong to
    :func:`record_breaker_verdicts`.

    Args:
        database: The application's database handle.
        model_id: The model's registry ULID.
        task_profile_id: The profile's string id.
        now: The evaluation instant every window is measured back from.

    Returns:
        The freshly computed statistics keyed by window name.
    """
    with database.write() as session:
        attempts = attempt_outcomes(session, model_id=model_id, task_profile_id=task_profile_id)
        feedback = feedback_outcomes(session, model_id=model_id, task_profile_id=task_profile_id)
        stats = {
            window.name: compute_stats(attempts, feedback, window=window, now=now)
            for window in WINDOWS
        }
        _write_windows(
            session,
            model_id=model_id,
            task_profile_id=task_profile_id,
            stats=stats.values(),
            now=now,
        )
    return stats


def known_pairs(session: Session) -> set[tuple[str, str]]:
    """Every ``(model_id, task_profile_id)`` that has an attempt, a verdict, or a stats row."""
    attempted = session.execute(
        select(JobAttempt.model_id, Job.task_profile_id)
        .join(Job, Job.id == JobAttempt.job_id)
        .where(JobAttempt.model_id.is_not(None))
        .distinct()
    ).all()
    judged = session.execute(
        select(Job.selected_model_id, Job.task_profile_id)
        .join(Feedback, Feedback.job_id == Job.id)
        .where(Job.selected_model_id.is_not(None))
        .distinct()
    ).all()
    stored = session.execute(
        select(ReliabilityStat.model_id, ReliabilityStat.task_profile_id).distinct()
    ).all()
    pairs: set[tuple[str, str]] = set()
    for result in (attempted, judged, stored):
        for row in result:
            model_id, task_profile_id = row[0], row[1]
            if model_id is not None:
                pairs.add((str(model_id), str(task_profile_id)))
    return pairs


def recompute_all(database: Database, *, now: datetime) -> int:
    """Recompute every known pair from scratch — the full path the incremental one must equal.

    Returns:
        How many pairs were recomputed.
    """
    with database.read() as session:
        pairs = sorted(known_pairs(session))
    for model_id, task_profile_id in pairs:
        recompute_pair(database, model_id=model_id, task_profile_id=task_profile_id, now=now)
    return len(pairs)


def _stats_of(row: ReliabilityStat) -> WindowStats:
    return WindowStats(
        window=row.window,
        attempts=row.attempts,
        successes=row.successes,
        validation_passes=row.validation_passes,
        errors=row.errors,
        timeouts=row.timeouts,
        cancellations=row.cancellations,
        latency_count=row.latency_count,
        p50_latency_ms=row.p50_latency_ms,
        p95_latency_ms=row.p95_latency_ms,
        output_token_count=row.output_token_count,
        mean_output_tokens=row.mean_output_tokens,
        tokens_per_second_count=row.tokens_per_second_count,
        mean_tokens_per_second=row.mean_tokens_per_second,
        feedback_count=row.feedback_count,
        acceptance_rate=row.acceptance_rate,
        quality_count=row.quality_count,
        mean_quality=row.mean_quality,
    )


def stats_for(database: Database, *, model_id: str, task_profile_id: str) -> dict[str, WindowStats]:
    """The stored rows for one pair, keyed by window; missing windows are absent from the map."""
    with database.read() as session:
        rows = session.execute(
            select(ReliabilityStat).where(
                ReliabilityStat.model_id == model_id,
                ReliabilityStat.task_profile_id == task_profile_id,
            )
        ).scalars()
        return {row.window: _stats_of(row) for row in rows}


def factors_for_task(database: Database, *, task_profile_id: str) -> dict[str, ReliabilityFactor]:
    """Every model's :class:`ReliabilityFactor` for one task profile, keyed by model ULID.

    One query for the profile's rows, then the pure factor per model: the read routing makes on
    every decision (data model §4's lookup, on the uniqueness index). A model with no rows is
    simply absent, and routing treats absence as neutral.
    """
    with database.read() as session:
        rows = session.execute(
            select(ReliabilityStat).where(ReliabilityStat.task_profile_id == task_profile_id)
        ).scalars()
        by_model: dict[str, dict[str, WindowStats]] = {}
        for row in rows:
            by_model.setdefault(row.model_id, {})[row.window] = _stats_of(row)
    return {model_id: reliability_factor(windows) for model_id, windows in by_model.items()}


def breaker_samples(database: Database, *, since: datetime) -> dict[str, list[AttemptSample]]:
    """Attempt outcomes per model since ``since``, for the circuit breaker (queue §7).

    P5's ``breaker_source`` seam, now fed from here: the sample's success flag is
    :func:`~loadcoach.domain.reliability.counts_as_success`, the one rule the statistics use, so
    a breaker verdict and the Reliability page can never disagree about what failed. The window
    the breaker evaluates (ten minutes) is far shorter than any statistics window, so the samples
    are the raw attempt rows rather than the rolled-up counts.
    """
    with database.read() as session:
        rows = session.execute(
            select(Model.canonical_id, JobAttempt.completed_at, JobAttempt.outcome)
            .join(Model, Model.id == JobAttempt.model_id)
            .where(JobAttempt.completed_at > since, JobAttempt.outcome != "cancelled")
        ).all()
    samples: dict[str, list[AttemptSample]] = {}
    for canonical_id, completed_at, outcome in rows:
        if completed_at is None:
            continue
        samples.setdefault(canonical_id, []).append(
            AttemptSample(at=completed_at, succeeded=counts_as_success(outcome))
        )
    return samples


def record_breaker_verdicts(
    database: Database, verdicts: Iterable[BreakerVerdict], *, now: datetime
) -> int:
    """Persist each model's breaker verdict onto its ``reliability_stats`` rows (data model §2).

    The breaker lives in the serving process; these columns are how a one-shot command, the
    Reliability page and ``GET /reliability`` show its state and reason without it. A model with
    no rows yet has nothing to annotate and is skipped.

    Returns:
        How many rows were updated.
    """
    by_canonical = {verdict.canonical_id: verdict for verdict in verdicts}
    if not by_canonical:
        return 0
    updated = 0
    with database.write() as session:
        rows = session.execute(
            select(ReliabilityStat, Model.canonical_id)
            .join(Model, Model.id == ReliabilityStat.model_id)
            .where(Model.canonical_id.in_(list(by_canonical)))
        ).all()
        for row, canonical_id in rows:
            verdict = by_canonical[canonical_id]
            row.circuit_state = verdict.state.value
            row.circuit_opened_at = verdict.opened_at
            row.circuit_reason = verdict.reason
            row.updated_at = now
            updated += 1
    return updated


@dataclass(frozen=True, slots=True)
class ReliabilityEntry:
    """One ``(model, task_profile)`` as ``GET /reliability`` and the Reliability page show it.

    Attributes:
        model_id: The model's registry ULID.
        canonical_id: The model's canonical ID.
        task_profile_id: The profile.
        windows: The stored statistics keyed by window; a missing window is an empty one.
        factor: The reliability factor routing applies to this pair right now.
        regression: The verdict of the ``7d`` window against everything before it.
        circuit_state: The breaker's persisted state for the model.
        circuit_opened_at: When it opened, if it is open.
        circuit_reason: The breaker's own line.
        updated_at: When the rows were last recomputed.
    """

    model_id: str
    canonical_id: str
    task_profile_id: str
    windows: Mapping[str, WindowStats]
    factor: ReliabilityFactor
    regression: RegressionVerdict
    circuit_state: str
    circuit_opened_at: datetime | None
    circuit_reason: str | None
    updated_at: datetime

    def as_json(self) -> dict[str, Any]:
        """The API record: every statistic bounded, every absence with its reason."""
        from baseaicore.timeutil import to_rfc3339

        return {
            "model": {"canonical_id": self.canonical_id, "model_ref": self.model_id},
            "task_profile_id": self.task_profile_id,
            "windows": {name: stats.as_json() for name, stats in self.windows.items()},
            "factor": self.factor.as_json(),
            "regression": self.regression.as_json(),
            "circuit_breaker": {
                "state": self.circuit_state,
                "opened_at": (
                    None if self.circuit_opened_at is None else to_rfc3339(self.circuit_opened_at)
                ),
                "reason": self.circuit_reason,
            },
            "updated_at": to_rfc3339(self.updated_at),
        }


def reliability_report(
    database: Database,
    *,
    task_profile_id: str | None = None,
    canonical_id: str | None = None,
) -> tuple[ReliabilityEntry, ...]:
    """Every known pair's statistics, factor, regression verdict and breaker state.

    Args:
        database: The application's database handle.
        task_profile_id: Restrict to one profile, or ``None`` for all.
        canonical_id: Restrict to one model, or ``None`` for all.

    Returns:
        Entries ordered by canonical ID then profile. A pair is present once any window row
        exists for it; the windows it has no row for are reported empty, not omitted.
    """
    query = select(ReliabilityStat, Model.canonical_id).join(
        Model, Model.id == ReliabilityStat.model_id
    )
    if task_profile_id is not None:
        query = query.where(ReliabilityStat.task_profile_id == task_profile_id)
    if canonical_id is not None:
        query = query.where(Model.canonical_id == canonical_id)
    grouped: dict[tuple[str, str, str], list[ReliabilityStat]] = {}
    with database.read() as session:
        for row, model_canonical_id in session.execute(query).all():
            grouped.setdefault((model_canonical_id, row.task_profile_id, row.model_id), []).append(
                row
            )
        entries: list[ReliabilityEntry] = []
        for (model_canonical_id, profile, model_id), rows in sorted(grouped.items()):
            windows = {row.window: _stats_of(row) for row in rows}
            for window in WINDOWS:
                windows.setdefault(window.name, WindowStats(window=window.name))
            latest = max(rows, key=lambda row: row.updated_at)
            entries.append(
                ReliabilityEntry(
                    model_id=model_id,
                    canonical_id=model_canonical_id,
                    task_profile_id=profile,
                    windows=windows,
                    factor=reliability_factor(windows),
                    regression=detect_regression(recent=windows["7d"], all_time=windows["all"]),
                    circuit_state=latest.circuit_state,
                    circuit_opened_at=latest.circuit_opened_at,
                    circuit_reason=latest.circuit_reason,
                    updated_at=latest.updated_at,
                )
            )
    return tuple(entries)


def regression_warnings(database: Database) -> tuple[ReliabilityEntry, ...]:
    """The pairs whose recent validated-success rate has regressed (routing §11, spec §17)."""
    return tuple(entry for entry in reliability_report(database) if entry.regression.regressed)
