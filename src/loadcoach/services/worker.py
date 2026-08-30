"""loadcoach.services.worker — worker threads, the scheduler thread and the lease keeper.

**Workers** claim atomically, then run one job through the state machine: ``leased → admitted →
executing → validating → completed``, or the retry, fallback, deferral and cancellation branches
queue §2 lists. Every transition is a compare-and-set fenced on this worker's lease
(:func:`~loadcoach.services.queue.transition`), so a worker whose lease was reclaimed finds its
next step refused and stops rather than writing over the reclaimer's work — the fence that turns
a lease race into a lost attempt instead of a double completion.

**The scheduler** is one thread that never blocks on a provider. Every ``poll_interval_ms`` it
runs whatever is due: the **lease keeper** (ADR-0029 §4) renews the leases of every job this
process is executing every ``lease_renewal_interval_seconds``; the reaper recovers expired
leases; the ageing sweep runs every ``ageing_interval_seconds``; max-wait expiry fails jobs that
have waited past their bound. The worker cannot renew its own lease — it is inside a blocking
provider call for up to ``default_timeout_seconds`` (300 s), five times the lease — so a
self-heartbeat would guarantee that every long generation lost its lease and was reclaimed, which
is precisely the double-execution defect the atomic claim exists to prevent.

**Polling** is adaptive (ADR-0010): 50 ms after a claim, doubling to 1 s while idle, plus the
in-process wake-up ``enqueue`` sets after commit, which is how dispatch latency stays inside its
budget without busy-waiting. Every blocking call a worker makes goes through an injected
primitive — the wake-up, ``sleep`` and the provider — so the scheduling simulator can run this
exact loop over a fake clock.
"""

from __future__ import annotations

import logging
import random
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Final, cast

from baseaicore import SuiteError, new_id
from modelrack import CancellationToken

from loadcoach.domain.admission import (
    Reservation,
    adjust_snapshot,
    classify_rejections,
    reserved_bytes_by_device,
    waiting_job_can_proceed,
)
from loadcoach.domain.circuit_breaker import CircuitBreakers
from loadcoach.domain.queue_state import IN_FLIGHT_STATES, JobState, event_type_for
from loadcoach.domain.retry_policy import (
    Action,
    FailureKind,
    backoff_seconds,
    classify_failure,
    next_action,
    next_candidate_index,
)
from loadcoach.domain.routing.constraints import free_vram_by_gpu
from loadcoach.domain.routing.context_budget import estimate_input_tokens
from loadcoach.domain.routing.subject import RuntimeOverrides
from loadcoach.services.execution import (
    AttemptRecord,
    AttemptRefused,
    ExecutionOutcome,
    StreamChunk,
    corrective_turns,
    identity_of,
    link_decision,
    load_task_schema,
    provider_facts_for,
    run_attempt,
    write_attempt,
)
from loadcoach.services.queue import (
    AffinityHint,
    ClaimedJob,
    TransitionRefused,
    Wakeup,
    ageing_sweep,
    breaker_samples,
    cancelling_since,
    claim,
    expire_max_wait,
    move,
    reap_expired_leases,
    renew_leases,
    transition,
    waiting_deferrals,
)
from loadcoach.services.recovery import RecoverySummary, recover
from loadcoach.services.residency import ResidencyService
from loadcoach.services.routing import (
    NoEligibleModel,
    RouteRequest,
    RoutingPolicy,
    RoutingResult,
    route,
)
from loadcoach.services.task_profiles import DEFAULT_SCHEMAS_DIR

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence
    from pathlib import Path

    from modelrack.provider import Provider
    from sweatmeter import TelemetrySnapshot

    from loadcoach.config import Settings
    from loadcoach.domain.admission import AdmissionVerdict
    from loadcoach.domain.circuit_breaker import AttemptSample
    from loadcoach.domain.routing.ranking import RankedCandidate
    from loadcoach.domain.routing.subject import ProviderFacts
    from loadcoach.domain.validation import ValidationOutcome
    from loadcoach.services.database import Database
from loadcoach.services.job_events import JobEventSink
from loadcoach.services.machine import machine_fingerprint

__all__ = [
    "POLL_BUSY_SECONDS",
    "POLL_IDLE_SECONDS",
    "InFlightEntry",
    "InFlightRegistry",
    "QueueFlags",
    "QueueRuntime",
    "Scheduler",
    "Worker",
    "build_runtime",
]

logger = logging.getLogger(__name__)

POLL_BUSY_SECONDS: Final = 0.05
"""The poll interval right after a claim (ADR-0010: 50 ms busy)."""

POLL_IDLE_SECONDS: Final = 1.0
"""The poll interval the backoff settles at while the queue is empty (ADR-0010: 1 s idle)."""

_REAPER_INTERVAL_SECONDS: Final = 1.0
_MAX_WAIT_INTERVAL_SECONDS: Final = 5.0
_FLAGS_INTERVAL_SECONDS: Final = 1.0
_REEVALUATE_INTERVAL_SECONDS: Final = 5.0
_EVICT_INTERVAL_SECONDS: Final = 10.0
_SYNC_INTERVAL_SECONDS: Final = 60.0
_BREAKER_INTERVAL_SECONDS: Final = 10.0
_WATCHDOG_INTERVAL_SECONDS: Final = 5.0


@dataclass
class InFlightEntry:
    """One job a worker of this process currently holds, as the keeper and the API see it."""

    job_id: str
    worker_id: str
    owner: str
    cancel: CancellationToken
    claimed_at: datetime
    job_class: str
    state: str = JobState.LEASED.value
    canonical_id: str | None = None
    model_id: str | None = None
    target_gpu_index: int | None = None
    estimated_bytes: int | None = None
    lost: bool = False


class InFlightRegistry:
    """Thread-safe: which jobs this process holds, keyed by ``(owner, job_id)``.

    Keyed by the pair, not by job alone: after a lease race two workers can each believe they
    hold the same job — the stale holder inside its provider call, the reclaimer executing it
    again — and the keeper must renew for the one that owns the lease while marking the other
    lost. One entry per job would let the stale worker's cleanup evict the live one's entry, after
    which its lease would silently stop being renewed.
    """

    def __init__(self) -> None:
        """Start empty."""
        self._entries: dict[tuple[str, str], InFlightEntry] = {}
        self._lock = threading.Lock()

    def register(self, entry: InFlightEntry) -> None:
        """Record a claim."""
        with self._lock:
            self._entries[(entry.owner, entry.job_id)] = entry

    def update(self, job_id: str, owner: str, **fields: Any) -> None:
        """Change an entry's fields (state, model, device, estimate) as the job progresses."""
        with self._lock:
            entry = self._entries.get((owner, job_id))
            if entry is not None:
                for name, value in fields.items():
                    setattr(entry, name, value)

    def remove(self, job_id: str, owner: str) -> InFlightEntry | None:
        """Forget one worker's hold on a job once that worker is done with it."""
        with self._lock:
            return self._entries.pop((owner, job_id), None)

    def get(self, job_id: str, owner: str) -> InFlightEntry | None:
        """Look one worker's hold on a job up."""
        with self._lock:
            return self._entries.get((owner, job_id))

    def entries_for_job(self, job_id: str) -> tuple[InFlightEntry, ...]:
        """Every hold on ``job_id`` — one normally; two briefly after a lease race."""
        with self._lock:
            return tuple(e for (_, jid), e in self._entries.items() if jid == job_id)

    def snapshot(self) -> tuple[InFlightEntry, ...]:
        """Every entry, for status pages and the admission aggregate."""
        with self._lock:
            return tuple(self._entries.values())

    def job_ids_for(self, owner: str) -> tuple[str, ...]:
        """The jobs one lease owner holds — what the keeper renews for that worker."""
        with self._lock:
            return tuple(jid for (own, jid) in self._entries if own == owner)

    def mark_lost(self, job_id: str, owner: str) -> None:
        """The keeper found this hold's lease gone: stop the provider call at its next chunk."""
        with self._lock:
            entry = self._entries.get((owner, job_id))
            if entry is None:
                return
            entry.lost = True
            entry.cancel.cancel()

    def request_cancel(self, job_id: str) -> bool:
        """Cancel every in-flight provider call for a job. Returns whether any was held here."""
        with self._lock:
            entries = [e for (_, jid), e in self._entries.items() if jid == job_id]
        for entry in entries:
            entry.cancel.cancel()
        return bool(entries)

    def __len__(self) -> int:
        """How many holds are in flight."""
        with self._lock:
            return len(self._entries)


@dataclass
class QueueFlags:
    """Operator control state, refreshed by the scheduler (queue §11, api.md §8).

    ``lock`` serializes the two writers of the in-memory copy: an operator's request (which
    writes the durable flag and then this copy) and the scheduler's refresh (which reads the
    durable flag and then writes this copy). Without it a refresh that read the old value just
    before the request committed could overwrite the request's assignment a moment later, and a
    worker would claim during a drain for up to a second.
    """

    paused: bool = False
    draining: bool = False
    lock: threading.Lock = field(default_factory=threading.Lock)

    @property
    def claiming(self) -> bool:
        """Whether workers may take new work."""
        return not (self.paused or self.draining)

    def update(
        self,
        database: Database,
        *,
        now: datetime,
        paused: bool | None = None,
        draining: bool | None = None,
    ) -> None:
        """Write the given flags durably and to this copy, atomically with any refresh."""
        from loadcoach.services.queue import set_queue_flag

        with self.lock:
            if paused is not None:
                set_queue_flag(database, "queue.paused", paused, now=now)
                self.paused = paused
            if draining is not None:
                set_queue_flag(database, "queue.draining", draining, now=now)
                self.draining = draining

    def refresh(self, database: Database) -> None:
        """Re-read the durable flags into this copy, atomically with any operator update."""
        from loadcoach.services.queue import queue_flags

        with self.lock:
            flags = queue_flags(database)
            self.paused = flags["queue.paused"]
            self.draining = flags["queue.draining"]


@dataclass
class QueueRuntime:
    """Everything the workers and the scheduler share, wired once per process.

    Attributes:
        settings: The application settings.
        database: The database handle.
        provider: The provider handle.
        sink: The job event sink.
        in_flight: The in-flight registry.
        snapshot: Takes one telemetry observation, or ``None``.
        clock: The clock.
        wakeup: The workers' wake-up.
        sleep: How a worker waits out a retry backoff.
        policy: The routing policy.
        schemas_dir: Where task profile schemas resolve.
        owner_prefix: The lease owner prefix — unique per process, so a lease from a previous
            process is recognisably not ours.
        flags: Operator control state.
        residency: The residency policy, or ``None`` before :func:`build_runtime` sets it.
        resources_changed: Set by a worker when a job leaves flight and by an unload — the
            scheduler re-evaluates ``waiting_resources`` jobs at once rather than on its cadence.
        resident_model_ids: Which registry models are resident, for the affinity claim.
        breakers: The circuit breakers (queue §7), refreshed from ``breaker_source``.
        breaker_source: Attempt samples per model since an instant — ``job_attempts`` in
            Phase 5, swappable for ``reliability_stats`` in Phase 7.
        jitter: A uniform draw in ``[0, 1)`` for backoff; injected so the simulator is
            reproducible.
        evidence_refresh: The scheduled FreeWeight pull (P6), or ``None`` when
            ``[evidence] freeweight_url`` is empty — *not configured*, which is a different
            state from unavailable and must not silently become a failing refresh.
    """

    settings: Settings
    database: Database
    provider: Provider
    sink: JobEventSink
    in_flight: InFlightRegistry
    snapshot: Callable[[], TelemetrySnapshot | None]
    clock: Callable[[], datetime]
    wakeup: Wakeup
    sleep: Callable[[float], None]
    policy: RoutingPolicy
    schemas_dir: Path
    owner_prefix: str
    flags: QueueFlags = field(default_factory=QueueFlags)
    residency: ResidencyService | None = None
    resources_changed: threading.Event = field(default_factory=threading.Event)
    resident_model_ids: Callable[[], frozenset[str]] = lambda: frozenset()
    breakers: CircuitBreakers = field(default_factory=CircuitBreakers)
    evidence_refresh: Callable[[datetime], None] | None = None
    breaker_source: Callable[[datetime], Mapping[str, Sequence[AttemptSample]]] | None = None
    jitter: Callable[[], float] = random.random
    last_recovery: RecoverySummary | None = None
    dispatch_samples_ms: deque[float] = field(default_factory=lambda: deque(maxlen=100))
    workers: list[Worker] = field(default_factory=list)
    scheduler: Scheduler | None = None
    _threads: list[threading.Thread] = field(default_factory=list)

    def provider_facts(self) -> ProviderFacts:
        """Read the provider's declared capabilities."""
        return provider_facts_for(self.provider)

    def replace_provider(self, provider: Provider) -> None:
        """Point the workers and the residency policy at ``provider`` from now on.

        For a test that scripts a provider after the application lifespan built the default one;
        the residency service is rebuilt because it holds its own handle.
        """
        self.provider = provider
        self.residency = ResidencyService(
            self.database, provider, settings=self.settings.residency, clock=self.clock
        )
        self.resident_model_ids = self.residency.resident_model_ids

    def refresh_breakers(self, now: datetime) -> None:
        """Re-evaluate every breaker from the sample source's last window."""
        source = self.breaker_source
        since = now - timedelta(seconds=self.breakers.window_seconds)
        samples = breaker_samples(self.database, since=since) if source is None else source(since)
        self.breakers.update(samples, now=now)

    def in_use_model_ids(self, *, excluding_job: str | None = None) -> frozenset[str]:
        """Registry models in-flight jobs are executing on — never evicted, never reserved twice."""
        return frozenset(
            entry.model_id
            for entry in self.in_flight.snapshot()
            if entry.model_id is not None and entry.job_id != excluding_job
        )

    def admission_snapshot(self, *, excluding_job: str | None = None) -> TelemetrySnapshot | None:
        """The telemetry admission evaluates against: reservations applied, idle residents freed.

        Per device (ADR-0027 §2): every other in-flight job's estimate on GPU 0 is reserved on
        GPU 0 — unless its model is already resident there, in which case the snapshot already
        counts it — and the memory held by idle resident models is counted as reclaimable.
        """
        resident_devices = self.residency.resident_devices() if self.residency else {}
        reservations = [
            Reservation(entry.job_id, entry.target_gpu_index, entry.estimated_bytes)
            for entry in self.in_flight.snapshot()
            if entry.job_id != excluding_job
            and entry.target_gpu_index is not None
            and entry.estimated_bytes is not None
            and entry.state in {state.value for state in IN_FLIGHT_STATES}
            and not (
                entry.canonical_id is not None
                and entry.target_gpu_index in resident_devices.get(entry.canonical_id, frozenset())
            )
        ]
        evictable = (
            self.residency.evictable_bytes_by_device(
                self.in_use_model_ids(excluding_job=excluding_job)
            )
            if self.residency
            else {}
        )
        return adjust_snapshot(
            self.snapshot(), reserved=reserved_bytes_by_device(reservations), evictable=evictable
        )

    def recover(self) -> RecoverySummary:
        """Run queue §10's recovery pass now. Idempotent; the scheduler re-evaluates waiters."""
        scheduler = self.scheduler
        if self.residency is not None:
            self.residency.sync(self.clock())
        summary = recover(
            self.database,
            self.sink,
            now=self.clock(),
            owner_prefix=self.owner_prefix,
            queue_settings=self.settings.queue,
            reevaluate=None if scheduler is None else scheduler.reevaluate_waiting,
        )
        self.last_recovery = summary
        return summary

    def start(self) -> None:
        """Recover, then start the scheduler thread and every worker thread (production only).

        Recovery runs before any worker can claim (queue §10: "before accepting work"), so a job
        a dead process held is requeued or failed before this process could ever race it.
        """
        scheduler = self.scheduler
        if scheduler is None:  # pragma: no cover — build_runtime always sets it
            message = "runtime has no scheduler"
            raise RuntimeError(message)
        self.recover()
        threads = [threading.Thread(target=scheduler.run, name="loadcoach-scheduler", daemon=True)]
        threads.extend(
            threading.Thread(target=worker.run, name=f"loadcoach-{worker.worker_id}", daemon=True)
            for worker in self.workers
        )
        for thread in threads:
            thread.start()
        self._threads = threads

    def stop(self, *, timeout_seconds: float = 10.0) -> None:
        """Ask every thread to stop and join it. Workers finish their current transition first."""
        if self.scheduler is not None:
            self.scheduler.stop()
        for worker in self.workers:
            worker.stop()
        self.wakeup.set()
        for thread in self._threads:
            thread.join(timeout_seconds)
        self._threads = []


class _Cancelled(Exception):  # noqa: N818 — control flow inside one worker, never surfaced
    """Raised inside the attempt loop once the job has been moved to ``cancelled``."""


@dataclass(frozen=True, slots=True)
class _Execution:
    """The bookkeeping of one job's run on one worker."""

    job: ClaimedJob
    routing: RoutingResult
    schema: dict[str, Any] | None
    started_perf: float


class Worker:
    """One worker: claims, runs one job at a time, and stops touching a job whose lease it lost."""

    def __init__(self, runtime: QueueRuntime, index: int) -> None:
        """Create worker ``index`` of the runtime."""
        self.runtime = runtime
        self.worker_id = f"worker-{index}"
        self.owner = f"{runtime.owner_prefix}/{self.worker_id}"
        self._stop = threading.Event()
        self._streak = 0

    def stop(self) -> None:
        """Ask the loop to exit after its current job."""
        self._stop.set()

    @property
    def stopping(self) -> bool:
        """Whether :meth:`stop` was called."""
        return self._stop.is_set()

    # ------------------------------------------------------------------------------- the loop

    def run(self) -> None:
        """The worker loop: claim, process, back off adaptively, wake on enqueue."""
        runtime = self.runtime
        backoff = POLL_BUSY_SECONDS
        while not self._stop.is_set():
            if not runtime.flags.claiming:
                runtime.wakeup.wait(POLL_IDLE_SECONDS)
                runtime.wakeup.clear()
                continue
            try:
                job = self._claim()
            except Exception:
                logger.exception("worker.claim_failed", extra={"worker": self.worker_id})
                job = None
            if job is None:
                woken = runtime.wakeup.wait(backoff)
                runtime.wakeup.clear()
                backoff = POLL_BUSY_SECONDS if woken else min(backoff * 2, POLL_IDLE_SECONDS)
                continue
            backoff = POLL_BUSY_SECONDS
            self.process(job)

    def _claim(self) -> ClaimedJob | None:
        runtime = self.runtime
        resident = runtime.resident_model_ids()
        hint = (
            AffinityHint(
                resident_model_ids=resident,
                streak=self._streak,
                max_streak=runtime.settings.queue.max_affinity_streak,
            )
            if resident
            else None
        )
        job = claim(
            runtime.database,
            owner=self.owner,
            now=runtime.clock(),
            lease_seconds=runtime.settings.queue.lease_seconds,
            sink=runtime.sink,
            affinity=hint,
        )
        if job is not None:
            self._streak = self._streak + 1 if job.by_affinity else 0
            # Dispatch latency (performance targets §3.3): eligible -> claimed, for the status
            # page. Bounded window, so it reflects the recent past rather than the process's life.
            waited = (runtime.clock() - job.scheduled_for).total_seconds() * 1000
            runtime.dispatch_samples_ms.append(max(waited, 0.0))
        return job

    # ---------------------------------------------------------------------------- one job

    def process(self, job: ClaimedJob) -> None:
        """Run one claimed job through the state machine to a terminal state, or let it go.

        Every failure of *this worker's* right to the job — a refused transition, a refused
        attempt write — ends here with the job untouched further; the reaper, the recovery pass
        or the reclaiming worker owns it now. Any other exception fails the job explicitly,
        fenced on the lease, so a bug cannot leave a job in ``executing`` for ever.
        """
        runtime = self.runtime
        entry = InFlightEntry(
            job_id=job.job_id,
            worker_id=self.worker_id,
            owner=self.owner,
            cancel=CancellationToken(),
            claimed_at=runtime.clock(),
            job_class=job.job_class.value,
        )
        runtime.in_flight.register(entry)
        try:
            self._run(job, entry)
        except _Cancelled:
            pass
        except (TransitionRefused, AttemptRefused) as exc:
            logger.warning(
                "worker.lease_lost",
                extra={"worker": self.worker_id, "job_id": job.job_id, "detail": exc.message},
            )
            entry.cancel.cancel()
        except Exception as exc:
            logger.exception("worker.job_crashed", extra={"job_id": job.job_id})
            self._fail_after_crash(job, exc)
        finally:
            runtime.in_flight.remove(job.job_id, self.owner)
            runtime.resources_changed.set()

    def _fail_after_crash(self, job: ClaimedJob, exc: Exception) -> None:
        """A bug in the pipeline must not strand the job: fail it from whatever state it is in."""
        runtime = self.runtime
        now = runtime.clock()
        code = exc.code if isinstance(exc, SuiteError) else "INTERNAL_ERROR"
        try:
            with runtime.sink.write(runtime.database) as (session, events):
                from sqlalchemy import select

                from loadcoach.infrastructure.db.models import Job

                current = session.execute(
                    select(Job.state).where(Job.id == job.job_id, Job.lease_owner == self.owner)
                ).scalar_one_or_none()
                if current is None:
                    return
                transition(
                    session,
                    job.job_id,
                    current=JobState(current),
                    target=JobState.FAILED,
                    now=now,
                    owner=self.owner,
                    reason=code,
                    values={"error_code": code, "error_text": str(exc)[:2000]},
                )
                events.append(
                    job.job_id,
                    event_type_for(JobState.FAILED),
                    now=now,
                    message=f"failed in {current}: {code}",
                    data={"reason": code, "previous_state": current, "error": str(exc)[:500]},
                )
        except SuiteError:  # pragma: no cover — the lease went while we were failing it
            logger.warning("worker.crash_fail_refused", extra={"job_id": job.job_id})

    def _run(self, job: ClaimedJob, entry: InFlightEntry) -> None:
        runtime = self.runtime
        started_perf = time.perf_counter()
        # A cancel that arrived between the claim and this instant is already in the row
        # (``leased -> cancelling``): honour it before spending a routing pass.
        if job.cancel_requested or self._cancel_requested(job.job_id):
            self._cancel_from(job, JobState.LEASED, records=())
            return
        routing = self._route(job)
        if routing is None:
            return
        schema = load_task_schema(
            cast("str | None", routing.task_profile.execution.get("json_schema_ref")),
            schemas_dir=runtime.schemas_dir,
        )
        execution = _Execution(job=job, routing=routing, schema=schema, started_perf=started_perf)
        self._admit(execution, entry)
        if self._cancel_requested(job.job_id):
            self._cancel_from(job, JobState.ADMITTED, records=())
            return
        self._attempts(execution, entry)

    # ------------------------------------------------------------------ routing and admission

    def _route(self, job: ClaimedJob) -> RoutingResult | None:
        """Route the job now, with the machine as it is. ``None`` when the job left ``leased``."""
        runtime = self.runtime
        submission = job.submission
        request = submission.to_generate_request()
        caller_text = "".join(message.content for message in request.transcript())
        now = runtime.clock()
        runtime.refresh_breakers(now)
        # A half-open breaker lets exactly one job through as its probe (queue §7): the exclusion
        # set is read *before* any probe is marked, so this job may route to the model; once it
        # has, the probe is marked in flight and later jobs exclude the model until the attempt
        # reports. P7 adds the dedicated low-priority probe job with its own prompt record.
        try:
            result = route(
                runtime.database,
                RouteRequest(
                    task=submission.task,
                    estimated_input_tokens=estimate_input_tokens(caller_text),
                    max_output_tokens=cast(
                        "int | None", submission.sampling.get("max_output_tokens")
                    ),
                    overrides=submission.overrides or RuntimeOverrides(),
                ),
                provider=runtime.provider_facts(),
                policy=runtime.policy,
                snapshot=runtime.admission_snapshot(excluding_job=job.job_id),
                resident_models=self._resident_canonical_ids(),
                open_circuit_breakers=runtime.breakers.excluded(),
                resident_devices=runtime.residency.resident_devices()
                if runtime.residency
                else None,
                circuit_breaker_details=runtime.breakers.details(),
                now=now,
            )
        except NoEligibleModel as exc:
            verdict = classify_rejections(
                cast("Sequence[Mapping[str, Any]]", exc.details.get("candidates", ()))
            )
            if verdict.defer:
                self._defer(job, verdict, exc)
            else:
                self._fail_admission(job, exc)
            return None
        ranking = result.explanation.ranking
        for candidate in (ranking.primary, *ranking.fallbacks):
            if candidate is not None:
                runtime.breakers.allow_probe(candidate.subject.facts.canonical_id)
        return result

    def _resident_canonical_ids(self) -> frozenset[str]:
        residency = self.runtime.residency
        return residency.resident_canonical_ids() if residency else frozenset()

    def _defer(self, job: ClaimedJob, verdict: AdmissionVerdict, exc: NoEligibleModel) -> None:
        """``leased -> waiting_resources`` with the numbers, releasing the lease (queue §5)."""
        runtime = self.runtime
        now = runtime.clock()
        decision_id = cast("str | None", exc.details.get("decision_id"))
        candidates = cast("Sequence[Mapping[str, Any]]", exc.details.get("candidates", ()))
        first_model_id = next(
            (
                cast("str | None", item.get("model_id"))
                for item in candidates
                if item.get("canonical_id") in verdict.candidates
            ),
            None,
        )
        summary = (
            f"insufficient VRAM: needs {verdict.required_bytes} B + {verdict.headroom_bytes} B "
            f"headroom; free by device {verdict.free_bytes_by_gpu}"
            if verdict.required_bytes is not None
            else f"VRAM estimate unknown ({', '.join(verdict.unknown_reasons) or 'no reason'}) "
            "and no candidate is resident"
        )
        with runtime.sink.write(runtime.database) as (session, events):
            transition(
                session,
                job.job_id,
                current=JobState.LEASED,
                target=JobState.WAITING_RESOURCES,
                now=now,
                owner=self.owner,
                reason="INSUFFICIENT_RESOURCES",
                values={"error_text": summary, "selected_model_id": first_model_id},
            )
            if decision_id is not None:
                link_decision(session, decision_id, job.job_id)
            events.append(
                job.job_id,
                event_type_for(JobState.WAITING_RESOURCES),
                now=now,
                message=summary,
                data={
                    "reason": "INSUFFICIENT_RESOURCES",
                    "decision_id": decision_id,
                    **verdict.as_json(),
                },
            )

    def _fail_admission(self, job: ClaimedJob, exc: NoEligibleModel) -> None:
        """No candidate at all: ``leased -> failed`` with the rejections (ADR-0036 §3)."""
        runtime = self.runtime
        now = runtime.clock()
        with runtime.sink.write(runtime.database) as (session, events):
            transition(
                session,
                job.job_id,
                current=JobState.LEASED,
                target=JobState.FAILED,
                now=now,
                owner=self.owner,
                reason=exc.code,
                values={"error_code": exc.code, "error_text": exc.message},
            )
            decision_id = cast("str | None", exc.details.get("decision_id"))
            if decision_id is not None:
                link_decision(session, decision_id, job.job_id)
            events.append(
                job.job_id,
                event_type_for(JobState.FAILED),
                now=now,
                message=f"no eligible model: {exc.message}",
                data={"reason": exc.code, "previous_state": "leased", **exc.details},
            )

    def _admit(self, execution: _Execution, entry: InFlightEntry) -> None:
        """``leased -> admitted``: the decision is linked and the selection recorded on the job."""
        runtime = self.runtime
        job = execution.job
        primary = execution.routing.explanation.ranking.primary
        assert primary is not None  # noqa: S101 — route() raised otherwise
        now = runtime.clock()
        with runtime.sink.write(runtime.database) as (session, events):
            transition(
                session,
                job.job_id,
                current=JobState.LEASED,
                target=JobState.ADMITTED,
                now=now,
                owner=self.owner,
                values={
                    "selected_model_id": primary.subject.facts.model_id,
                    "runtime_profile_hash": primary.subject.runtime_profile_hash,
                    "served_context": primary.subject.served_context.tokens,
                    "served_context_source": primary.subject.served_context.source,
                    "target_gpu_index": primary.target_gpu_index,
                },
            )
            link_decision(session, execution.routing.explanation.decision_id, job.job_id)
            events.append(
                job.job_id,
                event_type_for(JobState.ADMITTED),
                now=now,
                message=f"admitted on {primary.subject.facts.canonical_id}",
                data={
                    "decision_id": execution.routing.explanation.decision_id,
                    "canonical_id": primary.subject.facts.canonical_id,
                    "target_gpu_index": primary.target_gpu_index,
                    "estimated_vram_bytes": primary.estimated_vram_bytes,
                },
            )
        runtime.in_flight.update(
            job.job_id,
            self.owner,
            state=JobState.ADMITTED.value,
            canonical_id=primary.subject.facts.canonical_id,
            model_id=primary.subject.facts.model_id,
            target_gpu_index=primary.target_gpu_index,
            estimated_bytes=primary.estimated_vram_bytes,
        )

    def _cancel_requested(self, job_id: str) -> bool:
        from sqlalchemy import select

        from loadcoach.infrastructure.db.models import Job

        with self.runtime.database.read() as session:
            return bool(
                session.execute(select(Job.cancel_requested).where(Job.id == job_id)).scalar_one()
            )

    # ---------------------------------------------------------------------------- attempts

    def _attempts(self, execution: _Execution, entry: InFlightEntry) -> None:
        """Apply queue §7's failure table across the ranked candidates until one answers."""
        runtime = self.runtime
        job = execution.job
        ranking = execution.routing.explanation.ranking
        candidates: list[RankedCandidate] = [
            candidate
            for candidate in (ranking.primary, *ranking.fallbacks)
            if candidate is not None
        ]
        served_contexts = [candidate.subject.served_context.tokens for candidate in candidates]
        profile = execution.routing.task_profile
        per_candidate = int(cast("int", profile.execution.get("max_attempts", 1)))
        request = job.submission.to_generate_request()
        transcript = request.transcript()
        attempt_number = job.attempt
        state = JobState.ADMITTED
        records: list[AttemptRecord] = []
        index: int | None = 0
        first = True

        while index is not None:
            candidate = candidates[index]
            turns = transcript
            correction: Any = None
            attempts_here = 0
            if not first:
                state = self._readmit(execution, state, candidate, reason="fallback")
            first = False
            while True:
                if attempt_number >= job.max_attempts:
                    self._fail(execution, state, records, "ATTEMPTS_EXHAUSTED")
                    return
                attempt_number += 1
                attempts_here += 1
                residency = self._ensure_resident(job, candidate)
                if entry.cancel.is_cancelled or entry.lost or self._cancel_requested(job.job_id):
                    # Cancelled while the model was loading (state ``admitted``), or the lease
                    # was lost: stop before the provider is ever called. The row is read too,
                    # because a request from another process reaches the row before the token.
                    self._cancel_from(job, state, records=tuple(records))
                    return
                state = self._start_executing(execution, state, attempt_number, residency)
                on_chunk = self._on_chunk(job) if job.submission.stream else None
                outcome = run_attempt(
                    runtime.provider,
                    request=request,
                    candidate=candidate,
                    turns=turns,
                    attempt_number=attempt_number,
                    schema=execution.schema,
                    execution_policy=profile.execution,
                    validation_policy=profile.validation,
                    timeout_seconds=runtime.settings.execution.default_timeout_seconds,
                    cancel=entry.cancel,
                    on_chunk=on_chunk,
                    now=runtime.clock,
                    correction=correction,
                )
                records.append(outcome.record)
                self._record_use(candidate)
                if outcome.cancelled or entry.cancel.is_cancelled:
                    self._cancel_from(job, JobState.EXECUTING, records=tuple(records))
                    return
                if outcome.failure is not None:
                    kind = classify_failure(outcome.failure)
                    state = self._record_failed_attempt(execution, outcome.record)
                else:
                    assert outcome.validation is not None  # noqa: S101 — the provider answered
                    state = self._validate_attempt(execution, outcome.record)
                    if outcome.passed:
                        self._complete(execution, tuple(records), outcome, candidate)
                        return
                    kind = FailureKind.VALIDATION
                decision = next_action(
                    kind, attempts_on_candidate=attempts_here, per_candidate_limit=per_candidate
                )
                if decision.action is Action.STOP:
                    self._cancel_from(job, state, records=tuple(records))
                    return
                if decision.action is Action.RETRY_SAME:
                    if attempt_number >= job.max_attempts:
                        self._fail(execution, state, records, "ATTEMPTS_EXHAUSTED")
                        return
                    if kind is FailureKind.VALIDATION:
                        assert outcome.validation is not None  # noqa: S101
                        turns, correction = corrective_turns(
                            transcript,
                            previous_text=outcome.text,
                            outcome=outcome.validation,
                            schema=execution.schema,
                        )
                    state = self._readmit(
                        execution,
                        state,
                        candidate,
                        reason=decision.reason,
                        retry_number=attempts_here,
                    )
                    continue
                index = next_candidate_index(
                    served_contexts,
                    current=index,
                    larger_context_only=decision.larger_context_only,
                )
                break
        self._fail(execution, state, records, "ALL_CANDIDATES_FAILED")

    def _ensure_resident(
        self, job: ClaimedJob, candidate: RankedCandidate
    ) -> dict[str, Any] | None:
        """Load the candidate on its target device first, evicting idle residents as policy says."""
        runtime = self.runtime
        residency = runtime.residency
        if residency is None:
            return None
        facts = candidate.subject.facts
        snapshot = runtime.snapshot()
        free = (
            free_vram_by_gpu(snapshot).get(candidate.target_gpu_index)
            if snapshot is not None and candidate.target_gpu_index is not None
            else None
        )
        outcome = residency.ensure_loaded(
            model_id=facts.model_id,
            canonical_id=facts.canonical_id,
            identity=identity_of(facts),
            profile=candidate.subject.runtime_profile,
            gpu_index=candidate.target_gpu_index,
            in_use_model_ids=runtime.in_use_model_ids(excluding_job=job.job_id),
            required_bytes=candidate.estimated_vram_bytes,
            free_bytes=free,
            headroom_bytes=runtime.policy.vram_headroom_bytes,
            now=runtime.clock(),
        )
        if outcome.evicted:
            runtime.resources_changed.set()
        return outcome.as_json()

    def _record_use(self, candidate: RankedCandidate) -> None:
        residency = self.runtime.residency
        if residency is not None and candidate.target_gpu_index is not None:
            residency.record_use(
                candidate.subject.facts.model_id, candidate.target_gpu_index, self.runtime.clock()
            )

    def _start_executing(
        self,
        execution: _Execution,
        state: JobState,
        attempt_number: int,
        residency: dict[str, Any] | None = None,
    ) -> JobState:
        runtime = self.runtime
        job = execution.job
        now = runtime.clock()
        values: dict[str, Any] = {}
        if attempt_number == job.attempt + 1 and job.attempt == 0:
            values["started_at"] = now
            values["queue_wait_ms"] = int((now - job.queued_at).total_seconds() * 1000)
        with runtime.sink.write(runtime.database) as (session, events):
            transition(
                session,
                job.job_id,
                current=state,
                target=JobState.EXECUTING,
                now=now,
                owner=self.owner,
                values=values,
            )
            events.append(
                job.job_id,
                event_type_for(JobState.EXECUTING),
                now=now,
                message=f"attempt {attempt_number} started",
                data={"attempt": attempt_number, "residency": residency},
            )
        runtime.in_flight.update(job.job_id, self.owner, state=JobState.EXECUTING.value)
        return JobState.EXECUTING

    def _record_failed_attempt(self, execution: _Execution, record: AttemptRecord) -> JobState:
        """Provider failure: the attempt row and ``executing -> retrying`` in one transaction."""
        runtime = self.runtime
        job = execution.job
        now = runtime.clock()
        with runtime.sink.write(runtime.database) as (session, events):
            write_attempt(session, job.job_id, record, now=now, owner=self.owner)
            transition(
                session,
                job.job_id,
                current=JobState.EXECUTING,
                target=JobState.RETRYING,
                now=now,
                owner=self.owner,
                reason=record.error_code,
            )
            events.append(
                job.job_id,
                event_type_for(JobState.RETRYING),
                now=now,
                message=f"attempt {record.attempt} on {record.canonical_id}: {record.outcome}",
                data=record.as_json(),
            )
        runtime.in_flight.update(job.job_id, self.owner, state=JobState.RETRYING.value)
        return JobState.RETRYING

    def _validate_attempt(self, execution: _Execution, record: AttemptRecord) -> JobState:
        """The provider answered: the attempt row and ``executing -> validating`` together."""
        runtime = self.runtime
        job = execution.job
        now = runtime.clock()
        with runtime.sink.write(runtime.database) as (session, events):
            write_attempt(session, job.job_id, record, now=now, owner=self.owner)
            transition(
                session,
                job.job_id,
                current=JobState.EXECUTING,
                target=JobState.VALIDATING,
                now=now,
                owner=self.owner,
            )
            events.append(
                job.job_id,
                event_type_for(JobState.VALIDATING),
                now=now,
                message=f"attempt {record.attempt} on {record.canonical_id}: {record.outcome}",
                data=record.as_json(),
            )
        runtime.in_flight.update(job.job_id, self.owner, state=JobState.VALIDATING.value)
        return JobState.VALIDATING

    def _readmit(
        self,
        execution: _Execution,
        state: JobState,
        candidate: RankedCandidate,
        *,
        reason: str,
        retry_number: int = 0,
    ) -> JobState:
        """``validating|executing -> retrying -> admitted`` for the next attempt, with backoff.

        ``retry_number`` is 1 for the first retry on the same candidate, 2 for the second …, and
        0 for a fallback, which starts on a fresh model without waiting (queue §7: backoff is
        for retrying the same model).
        """
        runtime = self.runtime
        job = execution.job
        if state is not JobState.RETRYING:
            now = runtime.clock()
            with runtime.sink.write(runtime.database) as (session, events):
                transition(
                    session,
                    job.job_id,
                    current=state,
                    target=JobState.RETRYING,
                    now=now,
                    owner=self.owner,
                    reason=reason,
                )
                events.append(
                    job.job_id,
                    event_type_for(JobState.RETRYING),
                    now=now,
                    message=reason,
                    data={"reason": reason},
                )
            runtime.in_flight.update(job.job_id, self.owner, state=JobState.RETRYING.value)
        if retry_number > 0:
            backoff = backoff_seconds(
                runtime.settings.execution.attempt_backoff_seconds,
                retry_number,
                jitter=runtime.jitter(),
            )
            if backoff > 0:
                runtime.sleep(backoff)
        entry = runtime.in_flight.get(job.job_id, self.owner)
        if entry is not None and (entry.cancel.is_cancelled or entry.lost):
            # ADR-0036 §2: a cancel during backoff takes effect now, ``retrying -> cancelling``.
            self._cancel_from(job, JobState.RETRYING, records=())
            raise _Cancelled
        now = runtime.clock()
        with runtime.sink.write(runtime.database) as (session, events):
            transition(
                session,
                job.job_id,
                current=JobState.RETRYING,
                target=JobState.ADMITTED,
                now=now,
                owner=self.owner,
                reason=reason,
                values={
                    "selected_model_id": candidate.subject.facts.model_id,
                    "runtime_profile_hash": candidate.subject.runtime_profile_hash,
                    "served_context": candidate.subject.served_context.tokens,
                    "served_context_source": candidate.subject.served_context.source,
                    "target_gpu_index": candidate.target_gpu_index,
                },
            )
            events.append(
                job.job_id,
                event_type_for(JobState.ADMITTED),
                now=now,
                message=f"re-admitted on {candidate.subject.facts.canonical_id} ({reason})",
                data={
                    "reason": reason,
                    "canonical_id": candidate.subject.facts.canonical_id,
                    "target_gpu_index": candidate.target_gpu_index,
                },
            )
        runtime.in_flight.update(
            job.job_id,
            self.owner,
            state=JobState.ADMITTED.value,
            canonical_id=candidate.subject.facts.canonical_id,
            model_id=candidate.subject.facts.model_id,
            target_gpu_index=candidate.target_gpu_index,
            estimated_bytes=candidate.estimated_vram_bytes,
        )
        return JobState.ADMITTED

    def _on_chunk(self, job: ClaimedJob) -> Callable[[StreamChunk], None]:
        sink = self.runtime.sink

        def publish(chunk: StreamChunk) -> None:
            if chunk.kind == "token":
                sink.publish_token(job.job_id, chunk.payload)

        return publish

    # ----------------------------------------------------------------------------- endings

    def _complete(
        self,
        execution: _Execution,
        records: tuple[AttemptRecord, ...],
        outcome: Any,
        candidate: RankedCandidate,
    ) -> None:
        """``validating -> completed`` with the result written onto the job row."""
        runtime = self.runtime
        job = execution.job
        now = runtime.clock()
        result = outcome.result
        assert result is not None  # noqa: S101 — passed implies the provider answered
        total_ms = int((time.perf_counter() - execution.started_perf) * 1000)
        provider_ms = sum(record.provider_ms or 0 for record in records)
        validation: ValidationOutcome = outcome.validation
        degradations: list[str] = []
        if not runtime.provider_facts().supports_streaming:
            degradations.append("cancellation_deferred_to_completion")
        summary = ExecutionOutcome(
            job_id=job.job_id,
            status="completed",
            text=outcome.text,
            structured=validation.parsed if validation.performed else None,
            tool_calls=tuple(outcome.tool_calls),
            thinking=outcome.thinking or None,
            routing=execution.routing,
            selected=candidate,
            attempts=records,
            validation=validation,
            degradations=tuple(degradations),
            total_ms=total_ms,
            provider_ms=provider_ms,
            overhead_ms=max(total_ms - provider_ms, 0),
            ttft_ms=records[-1].ttft_ms if records else None,
            input_tokens=_count(result.usage.tokens.input_tokens),
            output_tokens=_count(result.usage.tokens.output_tokens),
            thinking_tokens=_count(result.usage.thinking_tokens),
            queue_wait_ms=self._queue_wait_ms(job),
        )
        with runtime.sink.write(runtime.database) as (session, events):
            transition(
                session,
                job.job_id,
                current=JobState.VALIDATING,
                target=JobState.COMPLETED,
                now=now,
                owner=self.owner,
                values={
                    "response_hash": _sha256(outcome.text),
                    "response_text": outcome.text,
                    "structured_output_json": summary.structured,
                    "tool_calls_json": list(summary.tool_calls),
                    "reasoning_available": summary.thinking is not None,
                    "reasoning_summary": summary.thinking,
                    "reasoning_source": "provider" if summary.thinking is not None else None,
                    "selected_model_id": candidate.subject.facts.model_id,
                    "runtime_profile_hash": candidate.subject.runtime_profile_hash,
                    "served_context": candidate.subject.served_context.tokens,
                    "served_context_source": candidate.subject.served_context.source,
                    "target_gpu_index": candidate.target_gpu_index,
                    "provider_ms": provider_ms,
                    "loadcoach_overhead_ms": summary.overhead_ms,
                    "total_ms": total_ms,
                    "ttft_ms": summary.ttft_ms,
                    "input_tokens": summary.input_tokens,
                    "output_tokens": summary.output_tokens,
                    "thinking_tokens": summary.thinking_tokens,
                    "validation_passed": validation.passed,
                    "degradations_json": list(degradations),
                    "error_code": None,
                    "error_text": None,
                },
            )
            events.append(
                job.job_id,
                event_type_for(JobState.COMPLETED),
                now=now,
                message=f"completed on {candidate.subject.facts.canonical_id}",
                data=summary.as_json(),
            )

    def _queue_wait_ms(self, job: ClaimedJob) -> int:
        from sqlalchemy import select

        from loadcoach.infrastructure.db.models import Job

        with self.runtime.database.read() as session:
            value = session.execute(
                select(Job.queue_wait_ms).where(Job.id == job.job_id)
            ).scalar_one_or_none()
        return int(value or 0)

    def _fail(
        self,
        execution: _Execution,
        state: JobState,
        records: Sequence[AttemptRecord],
        code: str,
    ) -> None:
        """A terminal failure from the current state, with every attempt in the event."""
        runtime = self.runtime
        job = execution.job
        now = runtime.clock()
        message = {
            "ATTEMPTS_EXHAUSTED": (
                f"attempts exhausted after {len(records)} (max {job.max_attempts})"
            ),
            "ALL_CANDIDATES_FAILED": "every candidate was tried and every attempt failed",
        }.get(code, code)
        with runtime.sink.write(runtime.database) as (session, events):
            transition(
                session,
                job.job_id,
                current=state,
                target=JobState.FAILED,
                now=now,
                owner=self.owner,
                reason=code,
                values={
                    "error_code": code,
                    "error_text": message,
                    "total_ms": int((time.perf_counter() - execution.started_perf) * 1000),
                    "provider_ms": sum(record.provider_ms or 0 for record in records),
                },
            )
            events.append(
                job.job_id,
                event_type_for(JobState.FAILED),
                now=now,
                message=message,
                data={
                    "reason": code,
                    "previous_state": state.value,
                    "attempts": [record.as_json() for record in records],
                },
            )

    def _cancel_from(
        self, job: ClaimedJob, state: JobState, *, records: tuple[AttemptRecord, ...]
    ) -> None:
        """``<state> -> cancelling -> cancelled``, preserving a partial attempt on its record.

        The row may already be ``cancelling``: a cancel request moves it there from outside
        (queue §8) while the worker is inside its provider call, and the worker completes the
        transition when it reaches its chunk boundary. Either way the lease fence applies.
        """
        runtime = self.runtime
        now = runtime.clock()
        with runtime.sink.write(runtime.database) as (session, events):
            from sqlalchemy import select

            from loadcoach.infrastructure.db.models import Job

            current = session.execute(
                select(Job.state).where(Job.id == job.job_id, Job.lease_owner == self.owner)
            ).scalar_one_or_none()
            if current is None:
                raise TransitionRefused(
                    f"Job {job.job_id} is no longer leased to {self.owner!r}; nothing to cancel.",
                    details={"job_id": job.job_id, "owner": self.owner},
                )
            state = JobState(current)
            if records and records[-1].outcome == "cancelled":
                write_attempt(session, job.job_id, records[-1], now=now, owner=self.owner)
            if state is not JobState.CANCELLING:
                transition(
                    session,
                    job.job_id,
                    current=state,
                    target=JobState.CANCELLING,
                    now=now,
                    owner=self.owner,
                    reason="cancel_requested",
                )
                events.append(
                    job.job_id,
                    event_type_for(JobState.CANCELLING),
                    now=now,
                    message=f"cancel requested in {state.value}",
                    data={"previous_state": state.value},
                )
            transition(
                session,
                job.job_id,
                current=JobState.CANCELLING,
                target=JobState.CANCELLED,
                now=now,
                owner=self.owner,
                reason="GENERATION_CANCELLED",
                values={"error_code": "GENERATION_CANCELLED", "cancel_requested": True},
            )
            events.append(
                job.job_id,
                event_type_for(JobState.CANCELLED),
                now=now,
                message="cancelled",
                data={
                    "reason": "GENERATION_CANCELLED",
                    "attempts": [record.as_json() for record in records],
                },
            )


def _count(value: object) -> int | None:
    from baseaicore import is_supported

    return int(value) if is_supported(value) and isinstance(value, (int, float)) else None


def _sha256(text: str) -> str:
    import hashlib

    return f"sha256:{hashlib.sha256(text.encode('utf-8')).hexdigest()}"


class Scheduler:
    """The scheduler thread's work, as a ``tick`` the simulator can call and a ``run`` loop.

    Each sub-step has its own cadence and its own enable flag. The flags exist for the simulator
    and for nothing else: a property test proves the keeper matters by switching it off and
    watching the lease expire, and proves the sweep matters by switching it off and watching a
    background job starve.
    """

    def __init__(self, runtime: QueueRuntime, *, tick_seconds: float) -> None:
        """Create the scheduler over ``runtime``."""
        self.runtime = runtime
        self.tick_seconds = tick_seconds
        self.keeper_enabled = True
        self.reaper_enabled = True
        self.sweep_enabled = True
        self.max_wait_enabled = True
        self._stop = threading.Event()
        self._last: dict[str, datetime | None] = {
            "keeper": None,
            "reaper": None,
            "sweep": None,
            "max_wait": None,
            "flags": None,
            "reevaluate": None,
            "evict": None,
            "sync": None,
            "breakers": None,
            "watchdog": None,
            "evidence": None,
        }
        self.renewals = 0
        self.sweeps = 0
        self.requeued = 0
        self.forced_cancellations = 0

    def stop(self) -> None:
        """Ask :meth:`run` to exit."""
        self._stop.set()

    def run(self) -> None:
        """Tick every ``tick_seconds`` on the wall clock until stopped (production only)."""
        while not self._stop.is_set():
            try:
                self.tick(self.runtime.clock())
            except Exception:
                logger.exception("scheduler.tick_failed")
            self._stop.wait(self.tick_seconds)

    def _due(self, name: str, now: datetime, interval_seconds: float) -> bool:
        last = self._last[name]
        if last is not None and (now - last).total_seconds() < interval_seconds:
            return False
        self._last[name] = now
        return True

    def tick(self, now: datetime) -> None:
        """Run every sub-step that is due at ``now``. Never blocks on a provider."""
        queue = self.runtime.settings.queue
        if self.keeper_enabled and self._due("keeper", now, queue.lease_renewal_interval_seconds):
            self.keep_leases(now)
        if self.reaper_enabled and self._due("reaper", now, _REAPER_INTERVAL_SECONDS):
            reap_expired_leases(self.runtime.database, now=now, sink=self.runtime.sink)
        if self.max_wait_enabled and self._due("max_wait", now, _MAX_WAIT_INTERVAL_SECONDS):
            expire_max_wait(
                self.runtime.database,
                now=now,
                default_max_wait_seconds=queue.max_wait_seconds,
                sink=self.runtime.sink,
            )
        if self.sweep_enabled and self._due("sweep", now, queue.ageing_interval_seconds):
            ageing_sweep(self.runtime.database, now=now, settings=queue)
            self.sweeps += 1
        if self.runtime.resources_changed.is_set() or self._due(
            "reevaluate", now, _REEVALUATE_INTERVAL_SECONDS
        ):
            self.runtime.resources_changed.clear()
            self._last["reevaluate"] = now
            self.reevaluate_waiting(now)
        if self._due("evict", now, _EVICT_INTERVAL_SECONDS):
            self.evict_idle(now)
        if self._due("sync", now, _SYNC_INTERVAL_SECONDS) and self.runtime.residency is not None:
            self.runtime.residency.sync(now)
        if self._due("breakers", now, _BREAKER_INTERVAL_SECONDS):
            self.runtime.refresh_breakers(now)
        if self._due("watchdog", now, _WATCHDOG_INTERVAL_SECONDS):
            self.watchdog(now)
        if self._due("flags", now, _FLAGS_INTERVAL_SECONDS):
            self.refresh_flags()
        if self.runtime.evidence_refresh is not None and self._due(
            "evidence", now, self.runtime.settings.evidence.import_interval_hours * 3600.0
        ):
            self.refresh_evidence(now)

    def refresh_evidence(self, now: datetime) -> None:
        """Pull from FreeWeight on the configured cadence, never failing the scheduler.

        A refresh that cannot reach its source is a degradation, not an error: the previous
        import is retained and badged, and the tick goes on. Raising here would take the queue's
        keeper and reaper down with it.
        """
        refresh = self.runtime.evidence_refresh
        if refresh is None:
            return
        try:
            refresh(now)
        except Exception:
            logger.exception("scheduler.evidence_refresh_failed")

    def watchdog(self, now: datetime) -> tuple[str, ...]:
        """Force ``cancelling -> cancelled`` after ``cancelling_watchdog_seconds`` (queue §8).

        A worker that never reaches a chunk boundary — a provider that ignores its token, a
        non-streaming call — would leave the job in ``cancelling`` for ever. The watchdog ends
        it and records that it did; the worker's late write is refused by the state fence.
        """
        limit = self.runtime.settings.queue.cancelling_watchdog_seconds
        forced: list[str] = []
        for job_id, entered_at in cancelling_since(self.runtime.database):
            if entered_at is not None and (now - entered_at).total_seconds() < limit:
                continue
            self.runtime.in_flight.request_cancel(job_id)
            try:
                move(
                    self.runtime.database,
                    self.runtime.sink,
                    job_id,
                    current=JobState.CANCELLING,
                    target=JobState.CANCELLED,
                    now=now,
                    reason="cancelling_watchdog",
                    message=(
                        f"cancelling for more than {limit} s: terminal transition forced by "
                        "the watchdog"
                    ),
                    data={"forced": True, "watchdog_seconds": limit},
                    values={"error_code": "GENERATION_CANCELLED"},
                )
            except TransitionRefused:
                continue
            forced.append(job_id)
            logger.warning("scheduler.cancelling_forced", extra={"job_id": job_id})
        self.forced_cancellations += len(forced)
        return tuple(forced)

    def reevaluate_waiting(self, now: datetime) -> tuple[str, ...]:
        """Re-queue every ``waiting_resources`` job that could be admitted now (queue §5).

        Applies exactly admission's rule to each job's recorded deferral — never more
        optimistic, so a job cannot bounce between claim and deferral while nothing changed.
        A job with no deferral record is re-queued and left to admission.
        """
        runtime = self.runtime
        waiting = waiting_deferrals(runtime.database)
        if not waiting:
            return ()
        snapshot = runtime.admission_snapshot()
        free = free_vram_by_gpu(snapshot) if snapshot is not None else {}
        resident_devices = runtime.residency.resident_devices() if runtime.residency else {}
        requeued: list[str] = []
        for job_id, record in waiting:
            if record is None or snapshot is None or not snapshot.gpus:
                proceed = (
                    True  # nothing recorded, or no device to be short of: let admission decide
                )
            else:
                candidates = cast("Sequence[str]", record.get("candidates", ()))
                resident_on: set[int] = set()
                for canonical_id in candidates:
                    resident_on |= resident_devices.get(canonical_id, frozenset())
                headroom = record.get("headroom_bytes")
                proceed = waiting_job_can_proceed(
                    required_bytes=cast("int | None", record.get("required_bytes")),
                    headroom_bytes=int(headroom)
                    if isinstance(headroom, int)
                    else runtime.policy.vram_headroom_bytes,
                    free_bytes_by_gpu=free,
                    resident_on=frozenset(resident_on),
                )
            if not proceed:
                continue
            try:
                move(
                    runtime.database,
                    runtime.sink,
                    job_id,
                    current=JobState.WAITING_RESOURCES,
                    target=JobState.QUEUED,
                    now=now,
                    reason="resources_freed",
                    message="resources may now be available: re-queued for admission",
                )
            except TransitionRefused:
                continue  # cancelled or expired meanwhile
            requeued.append(job_id)
        if requeued:
            self.requeued += len(requeued)
            runtime.wakeup.set()
        return tuple(requeued)

    def evict_idle(self, now: datetime) -> tuple[str, ...]:
        """Unload models idle for ``unload_idle_seconds`` (queue §6), then re-evaluate waiters."""
        residency = self.runtime.residency
        if residency is None:
            return ()
        unloaded = residency.evict_idle(now, in_use_model_ids=self.runtime.in_use_model_ids())
        if unloaded:
            self.runtime.resources_changed.set()
        return unloaded

    def keep_leases(self, now: datetime) -> None:
        """The lease keeper (ADR-0029 §4): renew every in-flight lease this process holds."""
        runtime = self.runtime
        for worker in runtime.workers:
            job_ids = runtime.in_flight.job_ids_for(worker.owner)
            if not job_ids:
                continue
            lost = renew_leases(
                runtime.database,
                owner=worker.owner,
                job_ids=job_ids,
                now=now,
                lease_seconds=runtime.settings.queue.lease_seconds,
            )
            self.renewals += len(job_ids) - len(lost)
            for job_id in lost:
                logger.warning(
                    "scheduler.lease_lost", extra={"job_id": job_id, "owner": worker.owner}
                )
                runtime.in_flight.mark_lost(job_id, worker.owner)
        # A cancel requested from another process (the CLI) reaches the row, not this
        # process's token; the keeper carries it across so the worker stops within a chunk.
        held = [
            job_id
            for worker in runtime.workers
            for job_id in runtime.in_flight.job_ids_for(worker.owner)
        ]
        if held:
            from sqlalchemy import select

            from loadcoach.infrastructure.db.models import Job

            with runtime.database.read() as session:
                flagged = (
                    session.execute(
                        select(Job.id).where(Job.id.in_(held), Job.cancel_requested.is_(True))
                    )
                    .scalars()
                    .all()
                )
            for job_id in flagged:
                runtime.in_flight.request_cancel(job_id)

    def refresh_flags(self) -> None:
        """Read the operator's pause/drain flags from the settings table."""
        self.runtime.flags.refresh(self.runtime.database)


def build_runtime(
    settings: Settings,
    *,
    database: Database,
    provider: Provider,
    sink: JobEventSink,
    snapshot: Callable[[], TelemetrySnapshot | None],
    clock: Callable[[], datetime] | None = None,
    wakeup: Wakeup | None = None,
    sleep: Callable[[float], None] | None = None,
    workers: int | None = None,
    owner_prefix: str | None = None,
    schemas_dir: Path = DEFAULT_SCHEMAS_DIR,
    jitter: Callable[[], float] | None = None,
) -> QueueRuntime:
    """Wire the runtime: registry, ``max_concurrent_jobs`` workers and the scheduler.

    Args:
        settings: The application settings.
        database: The database handle.
        provider: The provider handle.
        sink: The job event sink.
        snapshot: Takes one telemetry observation, or ``None``.
        clock: The clock; ``None`` is the wall clock.
        wakeup: The workers' wake-up; ``None`` is a ``threading.Event``.
        sleep: A worker's backoff sleep; ``None`` is a stoppable wall-clock wait.
        workers: How many workers; ``None`` is ``execution.max_concurrent_jobs``.
        owner_prefix: The lease owner prefix; ``None`` generates a per-process ULID.
        schemas_dir: Where task profile schemas resolve.
        jitter: The backoff jitter draw; ``None`` is ``random.random``.

    Returns:
        The runtime, not yet started.
    """
    event = threading.Event() if wakeup is None else wakeup
    stop_flag = threading.Event()

    def _wall_sleep(seconds: float) -> None:
        stop_flag.wait(seconds)

    runtime = QueueRuntime(
        settings=settings,
        database=database,
        provider=provider,
        sink=sink,
        in_flight=InFlightRegistry(),
        snapshot=snapshot,
        clock=clock if clock is not None else (lambda: datetime.now(UTC)),
        wakeup=event,
        sleep=sleep if sleep is not None else _wall_sleep,
        policy=RoutingPolicy.from_settings(
            routing=settings.routing,
            runtime=settings.runtime,
            telemetry=settings.telemetry,
            evidence=settings.evidence,
            machine_fingerprint=machine_fingerprint(),
        ),
        schemas_dir=schemas_dir,
        owner_prefix=owner_prefix if owner_prefix is not None else new_id(),
        jitter=jitter if jitter is not None else random.random,
    )
    if settings.evidence.freeweight_url.strip():

        def _refresh(now: datetime) -> None:
            from loadcoach.services.evidence import refresh_from_freeweight

            refresh_from_freeweight(database, settings.evidence, now=now)

        runtime.evidence_refresh = _refresh
    residency = ResidencyService(
        database, provider, settings=settings.residency, clock=runtime.clock
    )
    runtime.residency = residency
    runtime.resident_model_ids = residency.resident_model_ids
    count = workers if workers is not None else settings.execution.max_concurrent_jobs
    runtime.workers = [Worker(runtime, index) for index in range(count)]
    runtime.scheduler = Scheduler(runtime, tick_seconds=settings.queue.poll_interval_ms / 1000.0)
    original_stop = runtime.stop

    def _stop(*, timeout_seconds: float = 10.0) -> None:
        stop_flag.set()
        original_stop(timeout_seconds=timeout_seconds)

    runtime.stop = _stop  # type: ignore[method-assign]  # the backoff sleep must wake on stop
    return runtime
