"""loadcoach.services.queue — enqueue, the atomic claim, leases, the ageing sweep (queue §3–§4).

Every statement here is the one the documents describe, written once:

* **The claim** is one ``UPDATE … WHERE id = (SELECT … ORDER BY effective_priority DESC,
  created_at LIMIT 1) RETURNING``, inside ``BEGIN IMMEDIATE`` on SQLite and with
  ``FOR UPDATE SKIP LOCKED`` on PostgreSQL. It sets state, owner and expiry and **does not touch
  ``attempt``** (ADR-0029 §2) — the executor is that column's only writer.
* **The ageing sweep** is one set-based ``UPDATE`` over ``state IN ('queued','waiting_resources')``
  with ``queued_at`` as the origin (ADR-0029 §1). Startup recovery calls this same function.
* **Every transition** goes through :func:`transition`: a compare-and-set on ``state`` (and, for a
  leased job, on ``lease_owner``) plus the event, in one transaction. A worker whose lease was
  reclaimed finds its next transition refused rather than overwriting the reclaimer's work.
* **Lease reaping** selects on ``lease_expires_at`` alone. The invariant that makes that honest:
  ``lease_expires_at`` is ``NULL`` in every state that holds no lease, so the predicate needs no
  state filter and uses its own index (data model §4).

Durable idempotency lives here too (api.md §4): ``(source, idempotency_key)`` is unique,
``idempotency_expires_at`` is written on enqueue, and an expired key is released for reuse.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import timedelta
from typing import TYPE_CHECKING, Any, ClassVar, Protocol, cast

from baseaicore import SuiteError, new_id
from modelrack import Message, Role
from sqlalchemy import Integer, case, func, select, update
from sqlalchemy.exc import IntegrityError
from weightsdb import UtcDateTime

from loadcoach.domain.authorization import Principal, authorize
from loadcoach.domain.priority import (
    AGEING_EPSILON_POINTS,
    JobClass,
    base_priority,
    starvation_threshold_seconds,
)
from loadcoach.domain.queue_state import (
    ACTIVE_STATES,
    LEASE_HOLDING_STATES,
    TERMINAL_STATES,
    WAITING_STATES,
    JobState,
    cancel_target,
    check_transition,
    event_type_for,
    recovery_target,
)
from loadcoach.domain.routing.subject import RuntimeOverrides
from loadcoach.infrastructure.db.models import Job
from loadcoach.services.execution import GenerateRequest
from loadcoach.services.retention import SCRUBBED_MARKER
from loadcoach.services.routing import load_task_profile

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping, Sequence
    from datetime import datetime

    from baseaicore import RuntimeProfile
    from sqlalchemy.engine import CursorResult
    from sqlalchemy.orm import Session

    from loadcoach.config import ExecutionSettings, QueueSettings
    from loadcoach.services.database import Database
    from loadcoach.services.job_events import JobEventSink

__all__ = [
    "AffinityHint",
    "CancelOutcome",
    "ClaimedJob",
    "EnqueueOutcome",
    "JobNotCancellable",
    "JobNotFound",
    "JobRecord",
    "JobSubmission",
    "QueueFull",
    "QueueSnapshot",
    "ReapSummary",
    "TransitionRefused",
    "Wakeup",
    "ageing_sweep",
    "claim",
    "enqueue",
    "expire_max_wait",
    "get_job",
    "job_document",
    "list_jobs",
    "move",
    "queue_snapshot",
    "reap_expired_leases",
    "renew_leases",
    "cancel_job",
    "cancelling_since",
    "queue_flags",
    "resolve_model_id",
    "set_queue_flag",
    "transition",
    "waiting_deferrals",
]

logger = logging.getLogger(__name__)


class Wakeup(Protocol):
    """The workers' wake-up — ``threading.Event``'s shape (ADR-0010's in-process signal).

    :func:`enqueue` sets it after commit so an idle worker claims at once instead of at its next
    poll. Production hands the workers a ``threading.Event``; the scheduling simulator hands them
    its own implementation over a fake clock, and neither the queue nor the worker can tell.
    """

    def wait(self, timeout: float) -> bool:
        """Block until set or until ``timeout`` seconds pass; return whether it was set."""
        ...

    def set(self) -> None:
        """Wake every waiter and leave the flag set until :meth:`clear`."""
        ...

    def clear(self) -> None:
        """Reset the flag so the next :meth:`wait` blocks."""
        ...

    def is_set(self) -> bool:
        """Whether the flag is currently set."""
        ...


class QueueFull(SuiteError):
    """The queue holds ``queue.max_depth`` active jobs; the submission is refused (429)."""

    code: ClassVar[str] = "QUEUE_FULL"


class JobNotFound(SuiteError):
    """No job with that ID."""

    code: ClassVar[str] = "JOB_NOT_FOUND"


class JobNotCancellable(SuiteError):
    """The job is terminal; there is nothing to cancel (409, queue §8)."""

    code: ClassVar[str] = "JOB_NOT_CANCELLABLE"


class TransitionRefused(SuiteError):
    """The compare-and-set found the job in another state, or under another lease.

    For a worker this means the lease was lost — the job was reclaimed, recovered or cancelled
    by someone else — and the only correct response is to stop touching it.
    """

    code: ClassVar[str] = "TRANSITION_REFUSED"


@dataclass(frozen=True, slots=True)
class JobSubmission:
    """One ``POST /jobs`` body, validated by the web layer (api.md §5).

    Attributes:
        task: The task profile to route for.
        prompt: The caller's user turn. Mutually exclusive with ``messages``.
        system: The caller's system turn, with ``prompt``.
        messages: A full transcript. Mutually exclusive with ``prompt``.
        response_format: ``"text"``, ``"json"`` or ``"json_schema"``, overriding the profile.
        sampling: Overrides for the profile's execution parameters.
        overrides: Routing §10's overrides.
        job_class: The job's class (queue §1).
        priority: A priority within the class's band, or ``None`` for the band's bottom.
        max_wait_seconds: The absolute wait bound, or ``None`` for ``queue.max_wait_seconds``.
        idempotent: Whether lost-lease recovery may re-run the job. Plain generation is; a job
            with a caller-supplied side effect is not (queue §3).
        idempotency_key: Makes a retried submission safe, scoped per ``source``.
        source: The calling application, for the idempotency scope and the job record.
        stream: Whether the caller wants token deltas on the job's stream.
    """

    task: str
    prompt: str | None = None
    system: str | None = None
    messages: tuple[Message, ...] | None = None
    response_format: str | None = None
    sampling: Mapping[str, Any] = field(default_factory=dict)
    overrides: RuntimeOverrides | None = None
    job_class: JobClass = JobClass.NORMAL
    priority: int | None = None
    max_wait_seconds: int | None = None
    idempotent: bool = True
    idempotency_key: str | None = None
    source: str = "anonymous"
    stream: bool = False

    def transcript(self) -> tuple[Message, ...]:
        """Exactly the messages the provider will be sent — never rewritten (spec §9)."""
        if self.messages is not None:
            return self.messages
        turns: list[Message] = []
        if self.system is not None:
            turns.append(Message(role=Role.SYSTEM, content=self.system))
        turns.append(Message(role=Role.USER, content=self.prompt or ""))
        return tuple(turns)

    def as_request_json(self) -> dict[str, Any]:
        """The persisted form (``jobs.request_json``): everything needed to execute later.

        A queued job runs after its submitter has gone, possibly after a restart, so the
        transcript itself is stored here rather than only its hash.
        """
        overrides = self.overrides
        profile = None if overrides is None else overrides.runtime_profile
        return {
            "task": self.task,
            "messages": [
                {
                    "role": turn.role.value,
                    "content": turn.content,
                    "tool_call_id": turn.tool_call_id,
                }
                for turn in self.transcript()
            ],
            "response_format": self.response_format,
            "sampling": dict(self.sampling),
            "overrides": None
            if overrides is None
            else {
                "model": overrides.model,
                "runtime_profile": None
                if profile is None
                else {
                    "context_size": profile.context_size,
                    "kv_cache_precision": profile.kv_cache_precision,
                    "flash_attention": profile.flash_attention,
                    "keep_alive": profile.keep_alive,
                },
                "disallow_fallback": overrides.disallow_fallback,
                "require_evidence": overrides.require_evidence,
            },
            "stream": self.stream,
        }

    @classmethod
    def from_request_json(
        cls,
        payload: Mapping[str, Any],
        *,
        job_class: JobClass,
        priority: int,
        max_wait_seconds: int | None,
        idempotent: bool,
        idempotency_key: str | None,
        source: str,
    ) -> JobSubmission:
        """Rebuild a submission from ``jobs.request_json`` and the row's own columns."""
        from baseaicore import RuntimeProfile

        raw_overrides = cast("Mapping[str, Any] | None", payload.get("overrides"))
        overrides: RuntimeOverrides | None = None
        if raw_overrides is not None:
            raw_profile = cast("Mapping[str, Any] | None", raw_overrides.get("runtime_profile"))
            profile: RuntimeProfile | None = (
                None
                if raw_profile is None
                else RuntimeProfile(
                    context_size=raw_profile.get("context_size"),
                    kv_cache_precision=raw_profile.get("kv_cache_precision"),
                    flash_attention=raw_profile.get("flash_attention"),
                    keep_alive=raw_profile.get("keep_alive"),
                )
            )
            overrides = RuntimeOverrides(
                model=raw_overrides.get("model"),
                runtime_profile=profile,
                disallow_fallback=bool(raw_overrides.get("disallow_fallback", False)),
                require_evidence=bool(raw_overrides.get("require_evidence", False)),
            )
        messages = tuple(
            Message(
                role=Role(str(turn["role"])),
                content=str(turn["content"]),
                tool_call_id=cast("str | None", turn.get("tool_call_id")),
            )
            for turn in cast("Sequence[Mapping[str, Any]]", payload.get("messages", ()))
        )
        return cls(
            task=str(payload["task"]),
            messages=messages,
            response_format=cast("str | None", payload.get("response_format")),
            sampling=dict(cast("Mapping[str, Any]", payload.get("sampling", {}))),
            overrides=overrides,
            job_class=job_class,
            priority=priority,
            max_wait_seconds=max_wait_seconds,
            idempotent=idempotent,
            idempotency_key=idempotency_key,
            source=source,
            stream=bool(payload.get("stream", False)),
        )

    def to_generate_request(self) -> GenerateRequest:
        """The executor's view of this submission."""
        return GenerateRequest(
            task=self.task,
            messages=self.transcript(),
            response_format=self.response_format,
            sampling=dict(self.sampling),
            overrides=self.overrides,
            source=self.source,
            idempotency_key=self.idempotency_key,
            stream=self.stream,
        )


@dataclass(frozen=True, slots=True)
class EnqueueOutcome:
    """What :func:`enqueue` returns: the job, and whether this call created it."""

    job_id: str
    state: JobState
    created: bool


@dataclass(frozen=True, slots=True)
class ClaimedJob:
    """A job this worker now holds under a lease, with everything execution needs."""

    job_id: str
    job_class: JobClass
    base_priority: int
    effective_priority: int
    attempt: int
    max_attempts: int
    idempotent: bool
    cancel_requested: bool
    created_at: datetime
    queued_at: datetime
    scheduled_for: datetime
    lease_expires_at: datetime
    source: str
    submission: JobSubmission
    selected_model_id: str | None
    target_gpu_index: int | None
    by_affinity: bool = False


@dataclass(frozen=True, slots=True)
class AffinityHint:
    """What the affinity claim needs: which models are resident, and how long the streak is."""

    resident_model_ids: frozenset[str]
    streak: int
    max_streak: int


@dataclass(frozen=True, slots=True)
class CancelOutcome:
    """What a cancel request did.

    Attributes:
        job_id: The job.
        state: The job's state after the request: ``cancelled`` at once for a waiting job,
            ``cancelling`` for one a worker holds (it stops at its next chunk boundary).
        already: Whether the job was already on its way — the request was idempotent.
    """

    job_id: str
    state: JobState
    already: bool


@dataclass(frozen=True, slots=True)
class ReapSummary:
    """What lease reaping did."""

    requeued: tuple[str, ...]
    failed: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class QueueSnapshot:
    """Depth by state and class, the oldest queued age and the starvation counter (queue §11)."""

    depth_by_state: dict[str, int]
    depth_by_class: dict[str, int]
    oldest_queued_age_seconds: float | None
    starving: int
    active: int


@dataclass(frozen=True, slots=True)
class JobRecord:
    """One job as the API and CLI read it (a projection of the row, never the row itself)."""

    job_id: str
    state: JobState
    state_reason: str | None
    job_class: JobClass
    base_priority: int
    effective_priority: int
    source: str
    task_profile_id: str
    task_profile_version: str
    attempt: int
    max_attempts: int
    idempotent: bool
    idempotency_key: str | None
    cancel_requested: bool
    created_at: datetime
    queued_at: datetime | None
    scheduled_for: datetime | None
    started_at: datetime | None
    completed_at: datetime | None
    max_wait_seconds: int | None
    lease_owner: str | None
    lease_expires_at: datetime | None
    selected_model_id: str | None
    target_gpu_index: int | None
    runtime_profile_hash: str | None
    served_context: int | None
    served_context_source: str | None
    response_text: str | None
    structured_output: Any
    tool_calls: Any
    reasoning_available: bool
    reasoning_summary: str | None
    queue_wait_ms: int | None
    provider_ms: int | None
    loadcoach_overhead_ms: int | None
    total_ms: int | None
    ttft_ms: int | None
    input_tokens: int | None
    output_tokens: int | None
    cache_write_tokens: int | None
    cache_read_tokens: int | None
    thinking_tokens: int | None
    validation_passed: bool | None
    degradations: tuple[str, ...]
    error_code: str | None
    error_text: str | None
    request: Mapping[str, Any] | None


# ------------------------------------------------------------------------------------ enqueue


def enqueue(
    database: Database,
    submission: JobSubmission,
    *,
    now: datetime,
    queue_settings: QueueSettings,
    execution_settings: ExecutionSettings,
    sink: JobEventSink,
    wakeup: Wakeup | None = None,
    principal: Principal | None = None,
) -> EnqueueOutcome:
    """Persist a new job in ``queued``, or return the job an unexpired idempotency key names.

    Args:
        database: The application's database handle.
        submission: The validated submission.
        now: The enqueue instant; ``queued_at``, ``scheduled_for`` and the idempotency expiry
            all derive from it.
        queue_settings: ``max_depth``, ``max_wait_seconds`` and ``idempotency_ttl_hours``.
        execution_settings: ``max_attempts`` — the job's total attempt bound across leases.
        sink: Where the ``job.queued`` event goes.
        wakeup: The workers' wake-up; set after commit so an idle worker claims at once.

    Returns:
        The job and whether it was created by this call.

    Raises:
        TaskProfileNotFound: No such enabled task profile.
        ValidationError: ``priority`` lies outside the class's band.
        QueueFull: ``max_depth`` active jobs already exist. Checked before the insert, and never
            counted against a replayed idempotent submission.
    """
    authorize(principal, "write")
    profile = load_task_profile(database, submission.task)
    priority = base_priority(submission.job_class, submission.priority)
    if submission.idempotency_key is not None:
        existing = _existing_by_key(database, submission, now=now)
        if existing is not None:
            return existing

    active = _active_count(database)
    if active >= queue_settings.max_depth:
        raise QueueFull(
            f"The queue holds {active} active jobs; max_depth is {queue_settings.max_depth}.",
            details={"active": active, "max_depth": queue_settings.max_depth},
        )
    per_source = queue_settings.max_active_per_source
    if per_source:
        held = _active_count(database, source=submission.source)
        if held >= per_source:
            raise QueueFull(
                f"Source {submission.source!r} already holds {held} active job(s); the "
                f"per-source cap is {per_source} (spec §14).",
                details={
                    "source": submission.source,
                    "active": held,
                    "max_active_per_source": per_source,
                },
            )

    job_id = new_id()
    transcript = submission.transcript()
    pinned = submission.overrides.model if submission.overrides is not None else None
    # A pinned model is recorded at enqueue so the affinity claim (queue §6) can see it before
    # the job has ever been routed; a job that names no model gains affinity once routed.
    pinned_model_id = None if pinned is None else resolve_model_id(database, pinned)
    try:
        with sink.write(database) as (session, events):
            session.add(
                Job(
                    id=job_id,
                    selected_model_id=pinned_model_id,
                    task_profile_id=profile.profile_id,
                    task_profile_version=profile.version,
                    job_class=submission.job_class.value,
                    base_priority=priority,
                    effective_priority=priority,
                    source=submission.source,
                    state=JobState.QUEUED.value,
                    idempotency_key=submission.idempotency_key,
                    idempotent=submission.idempotent,
                    idempotency_expires_at=None
                    if submission.idempotency_key is None
                    else now + timedelta(hours=queue_settings.idempotency_ttl_hours),
                    request_json=submission.as_request_json(),
                    prompt_hash=_sha256(
                        "\n".join(f"{m.role.value}:{m.content}" for m in transcript)
                    ),
                    attempt=0,
                    max_attempts=execution_settings.max_attempts,
                    created_at=now,
                    scheduled_for=now,
                    queued_at=now,
                    max_wait_seconds=submission.max_wait_seconds or queue_settings.max_wait_seconds,
                    degradations_json=[],
                )
            )
            session.flush()
            events.append(
                job_id,
                event_type_for(JobState.QUEUED),
                now=now,
                message=f"queued as {submission.job_class.value} at priority {priority}",
                data={
                    "class": submission.job_class.value,
                    "base_priority": priority,
                    "task_profile_id": profile.profile_id,
                    "source": submission.source,
                },
            )
    except IntegrityError:
        # Two submissions raced on one key; the other one won the unique index.
        existing = _existing_by_key(database, submission, now=now)
        if existing is None:  # pragma: no cover — the row that won cannot have vanished
            raise
        return existing
    if wakeup is not None:
        wakeup.set()
    return EnqueueOutcome(job_id=job_id, state=JobState.QUEUED, created=True)


def _sha256(text: str) -> str:
    import hashlib

    return f"sha256:{hashlib.sha256(text.encode('utf-8')).hexdigest()}"


def _existing_by_key(
    database: Database, submission: JobSubmission, *, now: datetime
) -> EnqueueOutcome | None:
    """Return the unexpired job holding this caller's key, releasing an expired one."""
    from loadcoach.services.execution import existing_job_for_key

    found = existing_job_for_key(
        database, source=submission.source, key=submission.idempotency_key or "", now=now
    )
    if found is None:
        return None
    return EnqueueOutcome(job_id=found[0], state=JobState(found[1]), created=False)


def _active_count(database: Database, *, source: str | None = None) -> int:
    """Active jobs in the queue — all of them, or one source's (the per-source cap's input)."""
    with database.read() as session:
        statement = (
            select(func.count())
            .select_from(Job)
            .where(Job.state.in_([state.value for state in ACTIVE_STATES]))
        )
        if source is not None:
            statement = statement.where(Job.source == source)
        return int(session.execute(statement).scalar_one())


# -------------------------------------------------------------------------------------- claim


def claim(
    database: Database,
    *,
    owner: str,
    now: datetime,
    lease_seconds: int,
    sink: JobEventSink,
    affinity: AffinityHint | None = None,
) -> ClaimedJob | None:
    """Atomically take the highest-priority eligible job under a lease, or return ``None``.

    Order: ``effective_priority DESC, created_at ASC`` — FIFO among equals, which the starvation
    bound depends on. ``scheduled_for <= now`` gates a job whose retry backoff has not elapsed.

    With an ``affinity`` hint and a streak below its bound, a job of the **same top priority**
    whose ``selected_model_id`` is resident is preferred (queue §6): affinity reorders within a
    tie and never across priorities, so it cannot become a starvation source, and the streak bound
    stops it monopolising the tie group.

    Args:
        database: The application's database handle.
        owner: This worker's lease owner string.
        now: The claim instant.
        lease_seconds: ``queue.lease_seconds``.
        sink: Where the ``job.leased`` event goes.
        affinity: Residency and streak, or ``None`` to claim strictly by priority.

    Returns:
        The claimed job, or ``None`` when nothing is eligible. Never touches ``attempt``.
    """
    with sink.write(database) as (session, events):
        chosen: str | None = None
        by_affinity = False
        if (
            affinity is not None
            and affinity.resident_model_ids
            and (affinity.streak < affinity.max_streak)
        ):
            top = session.execute(
                select(func.max(Job.effective_priority)).where(
                    Job.state == JobState.QUEUED.value, Job.scheduled_for <= now
                )
            ).scalar_one()
            if top is not None:
                chosen = _claim_matching(
                    session,
                    owner=owner,
                    now=now,
                    lease_seconds=lease_seconds,
                    extra=(
                        Job.effective_priority == top,
                        Job.selected_model_id.in_(sorted(affinity.resident_model_ids)),
                    ),
                )
                by_affinity = chosen is not None
        if chosen is None:
            chosen = _claim_matching(
                session, owner=owner, now=now, lease_seconds=lease_seconds, extra=()
            )
        if chosen is None:
            return None
        job = session.get_one(Job, chosen)
        events.append(
            chosen,
            event_type_for(JobState.LEASED),
            now=now,
            message=f"leased by {owner}" + (" (affinity)" if by_affinity else ""),
            data={"lease_owner": owner, "lease_seconds": lease_seconds, "affinity": by_affinity},
        )
        return _claimed(job, by_affinity=by_affinity)


def _claim_matching(
    session: Session,
    *,
    owner: str,
    now: datetime,
    lease_seconds: int,
    extra: tuple[Any, ...],
) -> str | None:
    """The claim statement: one ``UPDATE … RETURNING`` over an ordered ``LIMIT 1`` subquery."""
    candidate = (
        select(Job.id)
        .where(Job.state == JobState.QUEUED.value, Job.scheduled_for <= now, *extra)
        .order_by(Job.effective_priority.desc(), Job.created_at.asc())
        .limit(1)
    )
    if session.get_bind().dialect.name == "postgresql":
        candidate = candidate.with_for_update(skip_locked=True)
    statement = (
        update(Job)
        .where(Job.id == candidate.scalar_subquery(), Job.state == JobState.QUEUED.value)
        .values(
            state=JobState.LEASED.value,
            state_reason=None,
            lease_owner=owner,
            lease_expires_at=now + timedelta(seconds=lease_seconds),
        )
        .returning(Job.id)
    )
    return session.execute(statement).scalar_one_or_none()


def _claimed(job: Job, *, by_affinity: bool) -> ClaimedJob:
    assert job.queued_at is not None and job.lease_expires_at is not None  # noqa: S101 — invariant
    submission = JobSubmission.from_request_json(
        cast("Mapping[str, Any]", job.request_json or {}),
        job_class=JobClass(job.job_class),
        priority=job.base_priority,
        max_wait_seconds=job.max_wait_seconds,
        idempotent=job.idempotent,
        idempotency_key=job.idempotency_key,
        source=job.source,
    )
    return ClaimedJob(
        job_id=job.id,
        job_class=JobClass(job.job_class),
        base_priority=job.base_priority,
        effective_priority=job.effective_priority,
        attempt=job.attempt,
        max_attempts=job.max_attempts,
        idempotent=job.idempotent,
        cancel_requested=job.cancel_requested,
        created_at=job.created_at,
        queued_at=job.queued_at,
        scheduled_for=job.scheduled_for or job.queued_at,
        lease_expires_at=job.lease_expires_at,
        source=job.source,
        submission=submission,
        selected_model_id=job.selected_model_id,
        target_gpu_index=job.target_gpu_index,
        by_affinity=by_affinity,
    )


# --------------------------------------------------------------------------------- transitions


def transition(
    session: Session,
    job_id: str,
    *,
    current: JobState,
    target: JobState,
    now: datetime,
    owner: str | None = None,
    reason: str | None = None,
    values: Mapping[str, Any] | None = None,
) -> None:
    """Move a job ``current -> target`` by compare-and-set, inside the caller's transaction.

    Args:
        session: The open write session (from :meth:`JobEventSink.write`).
        job_id: The job.
        current: The state the job must be in.
        target: The state to move to. Checked against the domain table first.
        now: The transition instant.
        owner: When given, the row must also carry this ``lease_owner`` — a worker's fence.
        reason: ``state_reason`` to record (``None`` clears it).
        values: Extra columns to set in the same statement.

    Raises:
        IllegalTransition: ``current -> target`` is not in the transition table.
        TransitionRefused: The row is not in ``current``, or is leased to someone else.
    """
    check_transition(current, target)
    assignments: dict[str, Any] = {"state": target.value, "state_reason": reason}
    if target in WAITING_STATES or target in TERMINAL_STATES:
        # Only a lease-holding state carries a lease — the reaper's predicate depends on it.
        assignments["lease_owner"] = None
        assignments["lease_expires_at"] = None
    if target is JobState.QUEUED:
        assignments["scheduled_for"] = now
    if target in TERMINAL_STATES:
        assignments["completed_at"] = now
    if values:
        assignments.update(values)
    conditions = [Job.id == job_id, Job.state == current.value]
    if owner is not None:
        conditions.append(Job.lease_owner == owner)
    result = cast(
        "CursorResult[Any]", session.execute(update(Job).where(*conditions).values(**assignments))
    )
    if result.rowcount != 1:
        raise TransitionRefused(
            f"Job {job_id} could not move {current.value} -> {target.value}: it is no longer in "
            f"{current.value}" + (f" under lease {owner!r}" if owner else "") + ".",
            details={
                "job_id": job_id,
                "current": current.value,
                "target": target.value,
                "owner": owner,
            },
        )


def move(
    database: Database,
    sink: JobEventSink,
    job_id: str,
    *,
    current: JobState,
    target: JobState,
    now: datetime,
    owner: str | None = None,
    reason: str | None = None,
    message: str | None = None,
    data: Mapping[str, Any] | None = None,
    values: Mapping[str, Any] | None = None,
) -> None:
    """:func:`transition` plus its event, as one committed unit of work.

    Raises:
        IllegalTransition: As :func:`transition`.
        TransitionRefused: As :func:`transition`.
    """
    with sink.write(database) as (session, events):
        transition(
            session,
            job_id,
            current=current,
            target=target,
            now=now,
            owner=owner,
            reason=reason,
            values=values,
        )
        payload = dict(data or {})
        if reason is not None:
            payload.setdefault("reason", reason)
        events.append(
            job_id,
            event_type_for(target),
            now=now,
            message=message or f"{current.value} -> {target.value}",
            data=payload,
        )


# -------------------------------------------------------------------------------- cancellation


def cancel_job(
    database: Database,
    sink: JobEventSink,
    job_id: str,
    *,
    now: datetime,
    on_request: Callable[[str], object] | None = None,
    principal: Principal | None = None,
) -> CancelOutcome:
    """Request a job's cancellation, transactionally (queue §8).

    A waiting job (``queued``, ``waiting_resources``) is cancelled at once. A job a worker holds
    moves to ``cancelling`` with ``cancel_requested`` set; the worker stops at its next chunk
    boundary and completes the transition, and the watchdog forces it if the worker does not.
    Idempotent: a job already ``cancelling`` reports ``already``.

    Args:
        database: The application's database handle.
        sink: Where the event goes.
        job_id: The job.
        now: The request instant.
        on_request: Called with the job ID after the flag is committed, for the caller to cancel
            the in-process provider call at once (the in-flight registry's ``request_cancel``).
            A request from another process has no such hook; the lease keeper carries the flag
            across within one renewal interval.

    Returns:
        The :class:`CancelOutcome`.

    Raises:
        JobNotFound: No such job.
        JobNotCancellable: The job is terminal.
    """
    authorize(principal, "write")
    with sink.write(database) as (session, events):
        row = session.execute(select(Job.state).where(Job.id == job_id)).scalar_one_or_none()
        if row is None:
            raise JobNotFound(f"No job {job_id!r}.", details={"job_id": job_id})
        current = JobState(row)
        if current in TERMINAL_STATES:
            raise JobNotCancellable(
                f"Job {job_id} is already {current.value}; nothing to cancel.",
                details={"job_id": job_id, "state": current.value},
            )
        target = cancel_target(current)
        if target is None:
            if on_request is not None:
                on_request(job_id)
            return CancelOutcome(job_id=job_id, state=current, already=True)
        transition(
            session,
            job_id,
            current=current,
            target=target,
            now=now,
            reason="cancel_requested" if target is JobState.CANCELLING else "GENERATION_CANCELLED",
            values={"cancel_requested": True}
            | ({"error_code": "GENERATION_CANCELLED"} if target is JobState.CANCELLED else {}),
        )
        events.append(
            job_id,
            event_type_for(target),
            now=now,
            message=f"cancel requested in {current.value}",
            data={"previous_state": current.value, "reason": "cancel_requested"},
        )
    if on_request is not None:
        on_request(job_id)
    return CancelOutcome(job_id=job_id, state=target, already=False)


def cancelling_since(database: Database) -> tuple[tuple[str, datetime | None], ...]:
    """Every ``cancelling`` job with the instant it entered the state — the watchdog's input."""
    from loadcoach.infrastructure.db.models import JobEvent

    with database.read() as session:
        job_ids = (
            session.execute(select(Job.id).where(Job.state == JobState.CANCELLING.value))
            .scalars()
            .all()
        )
        found: list[tuple[str, datetime | None]] = []
        for job_id in job_ids:
            entered = session.execute(
                select(JobEvent.timestamp)
                .where(
                    JobEvent.job_id == job_id,
                    JobEvent.event_type == event_type_for(JobState.CANCELLING),
                )
                .order_by(JobEvent.sequence.desc())
                .limit(1)
            ).scalar_one_or_none()
            found.append((job_id, entered))
        return tuple(found)


# -------------------------------------------------------------------------------------- leases


def renew_leases(
    database: Database,
    *,
    owner: str,
    job_ids: Iterable[str],
    now: datetime,
    lease_seconds: int,
) -> frozenset[str]:
    """Extend every lease this owner still holds; return the IDs it turned out **not** to hold.

    The keeper's statement. A job that is no longer in a lease-holding state under this owner
    was reclaimed, recovered, cancelled or finished; the keeper reports it so the worker stops.
    """
    ids = sorted(set(job_ids))
    if not ids:
        return frozenset()
    with database.write() as session:
        renewed = (
            session.execute(
                update(Job)
                .where(
                    Job.id.in_(ids),
                    Job.lease_owner == owner,
                    Job.state.in_(
                        [state.value for state in LEASE_HOLDING_STATES | {JobState.CANCELLING}]
                    ),
                )
                .values(lease_expires_at=now + timedelta(seconds=lease_seconds))
                .returning(Job.id)
            )
            .scalars()
            .all()
        )
    return frozenset(ids) - frozenset(renewed)


def reap_expired_leases(database: Database, *, now: datetime, sink: JobEventSink) -> ReapSummary:
    """Recover every job whose lease expired: requeue idempotent work, fail the rest (queue §3).

    Selects on ``lease_expires_at`` alone (its index; data model §4) — every row with a lease is
    in a lease-holding state by invariant. ``attempt`` is untouched, so a re-claim continues the
    sequence. ``cancelling`` jobs whose lease expired complete to ``cancelled``.
    """
    requeued: list[str] = []
    failed: list[str] = []
    with sink.write(database) as (session, events):
        rows = session.execute(
            select(Job.id, Job.state, Job.idempotent, Job.lease_owner).where(
                Job.lease_expires_at < now
            )
        ).all()
        for job_id, state_value, idempotent, owner in rows:
            state = JobState(state_value)
            target = recovery_target(state, idempotent=idempotent)
            if target is None:  # pragma: no cover — the invariant above excludes this
                continue
            reason = (
                "cancelled"
                if target is JobState.CANCELLED
                else "lease_expired"
                if target is JobState.QUEUED
                else "worker_lost"
            )
            transition(
                session,
                job_id,
                current=state,
                target=target,
                now=now,
                reason=reason,
                values={"error_code": "WORKER_LOST", "error_text": f"lease held by {owner} expired"}
                if target is JobState.FAILED
                else None,
            )
            events.append(
                job_id,
                event_type_for(target),
                now=now,
                message=f"lease held by {owner} expired in {state.value}: {reason}",
                data={"reason": reason, "previous_state": state.value, "lease_owner": owner},
            )
            (requeued if target is JobState.QUEUED else failed).append(job_id)
    if requeued or failed:
        logger.warning(
            "queue.leases_reaped",
            extra={"requeued": len(requeued), "failed": len(failed)},
        )
    return ReapSummary(requeued=tuple(requeued), failed=tuple(failed))


# --------------------------------------------------------------------------------------- sweep


def _waiting_seconds(session: Session, now_param: Any) -> Any:
    """``now - queued_at`` in seconds, in the dialect's own date arithmetic (ADR-0029 §1)."""
    if session.get_bind().dialect.name == "sqlite":
        return (func.julianday(now_param) - func.julianday(Job.queued_at)) * 86400.0
    return func.extract("epoch", now_param - Job.queued_at)


def ageing_sweep(database: Database, *, now: datetime, settings: QueueSettings) -> int:
    """Bring every waiting job's ``effective_priority`` up to date in one statement (queue §4).

    ``min(base + floor(minutes x rate), band_top + overflow)`` over
    ``state IN ('queued', 'waiting_resources')``, with ``queued_at`` as the origin. Rows already
    current are not written. Idempotent: a second run at the same instant changes nothing.

    Args:
        database: The application's database handle.
        now: The sweep instant.
        settings: ``ageing_priority_per_minute`` and ``overflow_allowance``.

    Returns:
        How many rows changed.
    """
    from sqlalchemy import bindparam

    with database.write() as session:
        dialect = session.get_bind().dialect.name
        now_param = bindparam("now", value=now, type_=UtcDateTime())
        minutes = _waiting_seconds(session, now_param) / 60.0
        points = minutes * settings.ageing_priority_per_minute + AGEING_EPSILON_POINTS
        non_negative = func.max(points, 0.0) if dialect == "sqlite" else func.greatest(points, 0.0)
        floored = (
            func.cast(non_negative, Integer) if dialect == "sqlite" else func.floor(non_negative)
        )
        aged = Job.base_priority + floored
        cap = (
            case(
                (Job.job_class == JobClass.INTERACTIVE.value, 999),
                (Job.job_class == JobClass.NORMAL.value, 799),
                (Job.job_class == JobClass.BACKGROUND.value, 399),
                else_=99,
            )
            + settings.overflow_allowance
        )
        fresh = func.min(aged, cap) if dialect == "sqlite" else func.least(aged, cap)
        result = cast(
            "CursorResult[Any]",
            session.execute(
                update(Job)
                .where(
                    Job.state.in_([state.value for state in WAITING_STATES]),
                    Job.effective_priority != fresh,
                )
                .values(effective_priority=fresh)
            ),
        )
        return int(result.rowcount)


def expire_max_wait(
    database: Database, *, now: datetime, default_max_wait_seconds: int, sink: JobEventSink
) -> tuple[str, ...]:
    """Fail every waiting job whose absolute bound has passed with ``MAX_WAIT_EXCEEDED``."""
    from sqlalchemy import bindparam

    with sink.write(database) as (session, events):
        now_param = bindparam("now", value=now, type_=UtcDateTime())
        waited = _waiting_seconds(session, now_param)
        rows = session.execute(
            select(Job.id, Job.state, Job.max_wait_seconds).where(
                Job.state.in_([state.value for state in WAITING_STATES]),
                waited > func.coalesce(Job.max_wait_seconds, default_max_wait_seconds),
            )
        ).all()
        expired: list[str] = []
        for job_id, state_value, max_wait in rows:
            bound = max_wait or default_max_wait_seconds
            transition(
                session,
                job_id,
                current=JobState(state_value),
                target=JobState.FAILED,
                now=now,
                reason="MAX_WAIT_EXCEEDED",
                values={
                    "error_code": "MAX_WAIT_EXCEEDED",
                    "error_text": f"waited longer than max_wait_seconds ({bound})",
                },
            )
            events.append(
                job_id,
                event_type_for(JobState.FAILED),
                now=now,
                message=f"max_wait_seconds ({bound}) exceeded in {state_value}",
                data={
                    "reason": "MAX_WAIT_EXCEEDED",
                    "previous_state": state_value,
                    "max_wait_seconds": bound,
                },
            )
            expired.append(job_id)
    return tuple(expired)


# ------------------------------------------------------------------------------------- reading


def queue_snapshot(
    database: Database, *, now: datetime, default_max_wait_seconds: int
) -> QueueSnapshot:
    """Depth by state and class, oldest queued age and the starvation counter (queue §4, §11)."""
    with database.read() as session:
        by_state = {
            state: int(count)
            for state, count in session.execute(
                select(Job.state, func.count())
                .where(Job.state.in_([state.value for state in ACTIVE_STATES]))
                .group_by(Job.state)
            ).all()
        }
        by_class = {
            job_class: int(count)
            for job_class, count in session.execute(
                select(Job.job_class, func.count())
                .where(Job.state.in_([state.value for state in ACTIVE_STATES]))
                .group_by(Job.job_class)
            ).all()
        }
        waiting = session.execute(
            select(Job.queued_at, Job.max_wait_seconds).where(
                Job.state.in_([state.value for state in WAITING_STATES])
            )
        ).all()
    oldest: float | None = None
    starving = 0
    for queued_at, max_wait in waiting:
        if queued_at is None:
            continue
        age = (now - queued_at).total_seconds()
        oldest = age if oldest is None else max(oldest, age)
        if age >= starvation_threshold_seconds(max_wait or default_max_wait_seconds):
            starving += 1
    return QueueSnapshot(
        depth_by_state=by_state,
        depth_by_class=by_class,
        oldest_queued_age_seconds=oldest,
        starving=starving,
        active=sum(by_state.values()),
    )


def _record(job: Job) -> JobRecord:
    return JobRecord(
        job_id=job.id,
        state=JobState(job.state),
        state_reason=job.state_reason,
        job_class=JobClass(job.job_class),
        base_priority=job.base_priority,
        effective_priority=job.effective_priority,
        source=job.source,
        task_profile_id=job.task_profile_id,
        task_profile_version=job.task_profile_version,
        attempt=job.attempt,
        max_attempts=job.max_attempts,
        idempotent=job.idempotent,
        idempotency_key=job.idempotency_key,
        cancel_requested=job.cancel_requested,
        created_at=job.created_at,
        queued_at=job.queued_at,
        scheduled_for=job.scheduled_for,
        started_at=job.started_at,
        completed_at=job.completed_at,
        max_wait_seconds=job.max_wait_seconds,
        lease_owner=job.lease_owner,
        lease_expires_at=job.lease_expires_at,
        selected_model_id=job.selected_model_id,
        target_gpu_index=job.target_gpu_index,
        runtime_profile_hash=job.runtime_profile_hash,
        served_context=job.served_context,
        served_context_source=job.served_context_source,
        response_text=job.response_text,
        structured_output=job.structured_output_json,
        tool_calls=job.tool_calls_json,
        reasoning_available=job.reasoning_available,
        reasoning_summary=job.reasoning_summary,
        queue_wait_ms=job.queue_wait_ms,
        provider_ms=job.provider_ms,
        loadcoach_overhead_ms=job.loadcoach_overhead_ms,
        total_ms=job.total_ms,
        ttft_ms=job.ttft_ms,
        input_tokens=job.input_tokens,
        output_tokens=job.output_tokens,
        cache_write_tokens=job.cache_write_tokens,
        cache_read_tokens=job.cache_read_tokens,
        thinking_tokens=job.thinking_tokens,
        validation_passed=job.validation_passed,
        degradations=tuple(cast("list[str]", job.degradations_json or [])),
        error_code=job.error_code,
        error_text=job.error_text,
        request=cast("Mapping[str, Any] | None", job.request_json),
    )


def get_job(database: Database, job_id: str) -> JobRecord:
    """Read one job.

    Raises:
        JobNotFound: No such job.
    """
    with database.read() as session:
        job = session.get(Job, job_id)
        if job is None:
            raise JobNotFound(f"No job {job_id!r}.", details={"job_id": job_id})
        return _record(job)


def job_document(database: Database, job_id: str) -> dict[str, Any]:
    """The full job as the API returns it (api.md §5): state, attempts, routing summary, usage,
    timings, validation, degradations — a superset of ``POST /generate``'s response shape, so a
    repeated idempotency key can return the original job in a form the caller already reads.

    Raises:
        JobNotFound: No such job.
    """
    from baseaicore.timeutil import to_rfc3339

    from loadcoach.infrastructure.db.models import JobAttempt, Model, RoutingDecision
    from loadcoach.services.feedback import feedback_for_job

    record = get_job(database, job_id)
    with database.read() as session:
        attempts = session.execute(
            select(JobAttempt, Model.canonical_id)
            .outerjoin(Model, Model.id == JobAttempt.model_id)
            .where(JobAttempt.job_id == job_id)
            .order_by(JobAttempt.attempt)
        ).all()
        feedback = [item.as_json() for item in feedback_for_job(session, job_id)]
        decision = session.execute(
            select(RoutingDecision.id, RoutingDecision.selected_score, RoutingDecision.flags_json)
            .where(RoutingDecision.job_id == job_id)
            .order_by(RoutingDecision.requested_at.desc())
            .limit(1)
        ).first()
        canonical = (
            session.execute(
                select(Model.canonical_id).where(Model.id == record.selected_model_id)
            ).scalar_one_or_none()
            if record.selected_model_id is not None
            else None
        )

    def stamp(value: datetime | None) -> str | None:
        return None if value is None else to_rfc3339(value)

    return {
        "job_id": record.job_id,
        "status": record.state.value,
        "state": record.state.value,
        "state_reason": record.state_reason,
        "class": record.job_class.value,
        "priority": {"base": record.base_priority, "effective": record.effective_priority},
        "source": record.source,
        "task": {"id": record.task_profile_id, "version": record.task_profile_version},
        "idempotent": record.idempotent,
        "idempotency_key": record.idempotency_key,
        "cancel_requested": record.cancel_requested,
        "max_wait_seconds": record.max_wait_seconds,
        "lease": {"owner": record.lease_owner, "expires_at": stamp(record.lease_expires_at)},
        "timestamps": {
            "created_at": stamp(record.created_at),
            "queued_at": stamp(record.queued_at),
            "scheduled_for": stamp(record.scheduled_for),
            "started_at": stamp(record.started_at),
            "completed_at": stamp(record.completed_at),
        },
        "output": {
            "text": record.response_text,
            "structured": record.structured_output,
            "tool_calls": record.tool_calls or [],
        },
        "reasoning": {
            "available": record.reasoning_available,
            "summary": record.reasoning_summary,
            "source": "provider" if record.reasoning_available else None,
        },
        "model": {
            "canonical_id": canonical,
            "model_ref": record.selected_model_id,
            "runtime_profile_hash": record.runtime_profile_hash,
            "served_context": record.served_context,
            "served_context_source": record.served_context_source,
            "target_gpu_index": record.target_gpu_index,
        },
        "routing": {
            "decision_id": None if decision is None else decision[0],
            "final_score": None if decision is None else decision[1],
            "flags": [] if decision is None else list(cast("list[str]", decision[2] or [])),
            "explanation_url": f"/api/v1/jobs/{record.job_id}/explanation",
        },
        "usage": {
            "input_tokens": record.input_tokens,
            "output_tokens": record.output_tokens,
            # The same four-class shape ExecutionOutcome.as_json renders (api.md §4 and §5 carry
            # one usage object, not two): `0` is a count the adapter reported, "unsupported" is
            # a class it never reported (ADR-0016 rule 4, ADR-0070 decision 7).
            "cache_write_tokens": record.cache_write_tokens
            if record.cache_write_tokens is not None
            else "unsupported",
            "cache_read_tokens": record.cache_read_tokens
            if record.cache_read_tokens is not None
            else "unsupported",
            "thinking_tokens": record.thinking_tokens
            if record.thinking_tokens is not None
            else "unsupported",
        },
        "timing": {
            "total_ms": record.total_ms,
            "provider_ms": record.provider_ms,
            "loadcoach_overhead_ms": record.loadcoach_overhead_ms,
            "ttft_ms": record.ttft_ms,
            "queue_wait_ms": record.queue_wait_ms,
        },
        "validation": {"passed": record.validation_passed, "attempts": len(attempts)},
        # Retention's promise (F9/M5C-9): a scrubbed job says so, rather than showing nothing
        # where its text was. The marker is written by services.retention's sweep.
        "retention": {
            "content_scrubbed_at": None
            if record.request is None
            else record.request.get(SCRUBBED_MARKER)
        },
        "feedback": feedback,
        "attempts": [
            {
                "attempt": row.attempt,
                "model": canonical_id,
                "runtime_profile_hash": row.runtime_profile_hash,
                "rank": row.rank,
                "outcome": row.outcome,
                "provider_ms": row.provider_ms,
                "ttft_ms": row.ttft_ms,
                "error_code": row.error_code,
                "started_at": stamp(row.started_at),
                "completed_at": stamp(row.completed_at),
                "prompt_id": row.prompt_id,
                "prompt_version": row.prompt_version,
                "prompt_sha256": row.prompt_sha256,
            }
            for row, canonical_id in attempts
        ],
        "attempt": record.attempt,
        "max_attempts": record.max_attempts,
        "degradations": list(record.degradations),
        "error": None
        if record.error_code is None
        else {"code": record.error_code, "message": record.error_text},
    }


def list_jobs(
    database: Database,
    *,
    states: Iterable[JobState] | None = None,
    job_class: JobClass | None = None,
    task: str | None = None,
    source: str | None = None,
    limit: int = 50,
    before: datetime | None = None,
) -> tuple[JobRecord, ...]:
    """List jobs newest first, filtered; ``before`` is the cursor (``created_at`` exclusive)."""
    with database.read() as session:
        statement = select(Job).order_by(Job.created_at.desc(), Job.id.desc()).limit(limit)
        if states is not None:
            statement = statement.where(Job.state.in_([state.value for state in states]))
        if job_class is not None:
            statement = statement.where(Job.job_class == job_class.value)
        if task is not None:
            statement = statement.where(Job.task_profile_id == task)
        if source is not None:
            statement = statement.where(Job.source == source)
        if before is not None:
            statement = statement.where(Job.created_at < before)
        return tuple(_record(job) for job in session.execute(statement).scalars().all())


def waiting_deferrals(database: Database) -> tuple[tuple[str, dict[str, Any] | None], ...]:
    """Every ``waiting_resources`` job with its latest deferral record (queue §5 re-evaluation).

    The record is the ``job.waiting_resources`` event's data — the numbers admission recorded
    when it deferred the job. ``None`` when no such event exists (a job deferred by an older
    build), in which case the caller re-queues it and lets admission decide afresh.
    """
    from loadcoach.infrastructure.db.models import JobEvent

    with database.read() as session:
        job_ids = (
            session.execute(select(Job.id).where(Job.state == JobState.WAITING_RESOURCES.value))
            .scalars()
            .all()
        )
        found: list[tuple[str, dict[str, Any] | None]] = []
        for job_id in job_ids:
            data = session.execute(
                select(JobEvent.data_json)
                .where(
                    JobEvent.job_id == job_id,
                    JobEvent.event_type == event_type_for(JobState.WAITING_RESOURCES),
                )
                .order_by(JobEvent.sequence.desc())
                .limit(1)
            ).scalar_one_or_none()
            found.append((job_id, data if isinstance(data, dict) else None))
        return tuple(found)


QUEUE_FLAG_KEYS = ("queue.paused", "queue.draining")
"""The operator's control flags, kept in the ``settings`` table so a ``loadcoach queue pause``
from another process reaches the running scheduler and a restart honours it (api.md §8)."""


def set_queue_flag(  # noqa: FBT001 — the flag's value is the argument
    database: Database,
    name: str,
    value: bool,
    *,
    now: datetime,
    principal: Principal | None = None,
) -> None:
    """Set ``queue.paused`` or ``queue.draining`` durably.

    Raises:
        ValueError: ``name`` is not one of the two flags.
    """
    authorize(principal, "admin")
    from loadcoach.infrastructure.db.models import Setting

    if name not in QUEUE_FLAG_KEYS:
        message = f"unknown queue flag {name!r}"
        raise ValueError(message)
    with database.write() as session:
        row = session.get(Setting, name)
        if row is None:
            session.add(Setting(key=name, value_json=value, updated_at=now))
        else:
            row.value_json = value
            row.updated_at = now


def queue_flags(database: Database) -> dict[str, bool]:
    """Read both control flags (absent means ``False``)."""
    from loadcoach.infrastructure.db.models import Setting

    with database.read() as session:
        rows = {
            str(key): bool(value)
            for key, value in session.execute(
                select(Setting.key, Setting.value_json).where(Setting.key.in_(QUEUE_FLAG_KEYS))
            ).all()
        }
    return {name: rows.get(name, False) for name in QUEUE_FLAG_KEYS}


def resolve_model_id(database: Database, canonical_id: str) -> str | None:
    """Return the registry ULID for ``canonical_id``, or ``None`` — for the affinity column."""
    from loadcoach.infrastructure.db.models import Model

    with database.read() as session:
        return session.execute(
            select(Model.id).where(Model.canonical_id == canonical_id).limit(1)
        ).scalar_one_or_none()


def runtime_profile_for(overrides: RuntimeOverrides | None) -> RuntimeProfile | None:
    """The submission's runtime profile override, if any (a small helper for the worker)."""
    return None if overrides is None else overrides.runtime_profile
