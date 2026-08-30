"""loadcoach.services.feedback — a caller's verdict on a job (api.md §6, spec §14).

Feedback is **caller input**: a statement by whoever used the output about whether it was useful,
never a fact LoadCoach established. It is stored per ``(job_id, source)`` — a second submission
from the same source updates its record, and two sources that disagree about one job are both kept
— and it feeds the ``reliability_factor`` and regression detection through
:mod:`loadcoach.domain.reliability`. It never touches benchmark evidence: production and benchmark
evidence are separate sources with separate confidence (routing §11).

``source`` is decided by the caller of this module from the authenticated token's name, or the
``X-Client-Name`` header on an open loopback bind (api.md §6); the body's own value is the last
resort. That is what stops one caller overwriting another's verdict.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from baseaicore import ValidationError
from baseaicore.timeutil import to_rfc3339
from sqlalchemy import select

from loadcoach.infrastructure.db.models import Feedback, Job
from loadcoach.services.queue import JobNotFound

if TYPE_CHECKING:
    from datetime import datetime

    from sqlalchemy.orm import Session

    from loadcoach.services.database import Database

__all__ = [
    "FeedbackOutcome",
    "FeedbackRecord",
    "FeedbackSubmission",
    "feedback_for_job",
    "list_feedback",
    "record_feedback",
]

MAX_SOURCE_LENGTH = 64
MAX_NOTES_LENGTH = 4000


@dataclass(frozen=True, slots=True)
class FeedbackSubmission:
    """What a caller said about one job (api.md §6's body, with ``source`` already decided).

    Attributes:
        source: Who is speaking — a token name, a client name, or ``"anonymous"``.
        accepted: Whether the caller used the output.
        quality_score: The caller's optional score in ``[0, 1]``.
        edited: Whether the caller changed the output before using it.
        validation_passed: The caller's own validation verdict, when it ran one.
        validation_detail: Whatever the caller's check reported, kept verbatim.
        notes: Free text.
    """

    source: str
    accepted: bool
    quality_score: float | None = None
    edited: bool = False
    validation_passed: bool | None = None
    validation_detail: Any = None
    notes: str | None = None


@dataclass(frozen=True, slots=True)
class FeedbackRecord:
    """One stored ``feedback`` row, as the API returns it."""

    feedback_id: str
    job_id: str
    source: str
    accepted: bool
    quality_score: float | None
    edited: bool
    validation_passed: bool | None
    validation_detail: Any
    notes: str | None
    created_at: datetime
    updated_at: datetime

    def as_json(self) -> dict[str, Any]:
        """The record as ``POST /jobs/{id}/feedback`` and ``GET /jobs/{id}`` carry it."""
        return {
            "feedback_id": self.feedback_id,
            "job_id": self.job_id,
            "source": self.source,
            "accepted": self.accepted,
            "quality_score": self.quality_score,
            "edited": self.edited,
            "validation": {"passed": self.validation_passed, "detail": self.validation_detail},
            "notes": self.notes,
            "created_at": to_rfc3339(self.created_at),
            "updated_at": to_rfc3339(self.updated_at),
        }


@dataclass(frozen=True, slots=True)
class FeedbackOutcome:
    """What :func:`record_feedback` did.

    Attributes:
        record: The stored record after the call.
        created: ``True`` on a source's first feedback for the job, ``False`` on an update.
        model_id: The job's selected model, or ``None`` when it has none yet — feedback on a
            job that has not run is stored, and folded into statistics once it can be attributed.
        task_profile_id: The job's task profile.
    """

    record: FeedbackRecord
    created: bool
    model_id: str | None
    task_profile_id: str


def _record(row: Feedback) -> FeedbackRecord:
    return FeedbackRecord(
        feedback_id=row.id,
        job_id=row.job_id,
        source=row.source,
        accepted=row.accepted,
        quality_score=row.quality_score,
        edited=row.edited,
        validation_passed=row.validation_passed,
        validation_detail=row.validation_detail_json,
        notes=row.notes,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _check(submission: FeedbackSubmission) -> None:
    """Refuse what the schema cannot express: an empty source, a score outside ``[0, 1]``."""
    problems: list[dict[str, str]] = []
    if not submission.source.strip() or len(submission.source) > MAX_SOURCE_LENGTH:
        problems.append(
            {"path": "source", "problem": f"1–{MAX_SOURCE_LENGTH} characters, not blank"}
        )
    if submission.quality_score is not None and not 0.0 <= submission.quality_score <= 1.0:
        problems.append({"path": "quality_score", "problem": "must be between 0 and 1"})
    if submission.notes is not None and len(submission.notes) > MAX_NOTES_LENGTH:
        problems.append({"path": "notes", "problem": f"at most {MAX_NOTES_LENGTH} characters"})
    if problems:
        raise ValidationError("Feedback failed validation.", details={"fields": problems})


def record_feedback(
    database: Database, job_id: str, submission: FeedbackSubmission, *, now: datetime
) -> FeedbackOutcome:
    """Store or update one source's feedback on one job, then refresh its reliability statistics.

    Idempotent per ``(job_id, source)``: the first call inserts, every later call from the same
    source overwrites that record in place and bumps ``updated_at``. Another source's record on
    the same job is untouched — conflicting verdicts are both retained (dev-plan P7 tests).

    Args:
        database: The application's database handle.
        job_id: The job the feedback is about. Any existing job, terminal or not.
        submission: The verdict, with ``source`` already decided by the caller.
        now: The instant, for ``created_at``/``updated_at``.

    Returns:
        The :class:`FeedbackOutcome`.

    Raises:
        JobNotFound: No such job.
        ValidationError: A blank or over-long ``source``, a ``quality_score`` outside ``[0, 1]``,
            or over-long ``notes``.
    """
    _check(submission)
    with database.write() as session:
        job = session.get(Job, job_id)
        if job is None:
            raise JobNotFound(f"No job {job_id!r}.", details={"job_id": job_id})
        row = session.execute(
            select(Feedback).where(Feedback.job_id == job_id, Feedback.source == submission.source)
        ).scalar_one_or_none()
        created = row is None
        if row is None:
            row = Feedback(job_id=job_id, source=submission.source, created_at=now)
            session.add(row)
        row.accepted = submission.accepted
        row.quality_score = submission.quality_score
        row.edited = submission.edited
        row.validation_passed = submission.validation_passed
        row.validation_detail_json = submission.validation_detail
        row.notes = submission.notes
        row.updated_at = now
        session.flush()
        outcome = FeedbackOutcome(
            record=_record(row),
            created=created,
            model_id=job.selected_model_id,
            task_profile_id=job.task_profile_id,
        )
    _refresh_statistics(database, outcome, now=now)
    return outcome


def _refresh_statistics(database: Database, outcome: FeedbackOutcome, *, now: datetime) -> None:
    """Fold the verdict into ``reliability_stats`` for the job's ``(model, task_profile)``.

    A job with no selected model yet has nothing to attribute the verdict to; the record is kept
    and picked up by the next recomputation once the job has run.
    """
    if outcome.model_id is None:
        return
    from loadcoach.services.reliability import recompute_pair

    recompute_pair(
        database, model_id=outcome.model_id, task_profile_id=outcome.task_profile_id, now=now
    )


def feedback_for_job(session: Session, job_id: str) -> list[FeedbackRecord]:
    """Every source's feedback on ``job_id``, oldest first, within the caller's session."""
    rows = session.execute(
        select(Feedback)
        .where(Feedback.job_id == job_id)
        .order_by(Feedback.created_at, Feedback.source)
    ).scalars()
    return [_record(row) for row in rows]


def list_feedback(database: Database, job_id: str) -> tuple[FeedbackRecord, ...]:
    """Every source's feedback on ``job_id``, oldest first.

    Raises:
        JobNotFound: No such job.
    """
    with database.read() as session:
        if session.get(Job, job_id) is None:
            raise JobNotFound(f"No job {job_id!r}.", details={"job_id": job_id})
        return tuple(feedback_for_job(session, job_id))
