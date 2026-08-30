"""The queue service against a real database: enqueue, the atomic claim, leases and the sweep.

The stress test is the one that earns its keep: eight threads claiming from two hundred queued
jobs, asserting every job is claimed exactly once. The query-plan tests are the other half of
"asserted, not assumed" (data model §4): they run ``EXPLAIN QUERY PLAN`` on the *same compiled
statements* the service executes, not on a hand-written approximation of them.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from baseaicore import ValidationError
from modelrack.testing import FakeProvider, FakeScript
from sqlalchemy import bindparam, select, text, update
from tests.integration.test_generate import _model
from weightsdb import UtcDateTime

from loadcoach.config import ExecutionSettings, QueueSettings
from loadcoach.domain.priority import JobClass, effective_priority
from loadcoach.domain.queue_state import IllegalTransition, JobState
from loadcoach.infrastructure.db.models import Job, JobEvent
from loadcoach.services.database import Database, ensure_ready
from loadcoach.services.job_events import JobEventSink
from loadcoach.services.models import discover_models
from loadcoach.services.queue import (
    AffinityHint,
    JobSubmission,
    QueueFull,
    TransitionRefused,
    ageing_sweep,
    claim,
    enqueue,
    expire_max_wait,
    get_job,
    move,
    queue_snapshot,
    reap_expired_leases,
    renew_leases,
    resolve_model_id,
)
from loadcoach.services.routing import TaskProfileNotFound
from loadcoach.services.task_profiles import import_task_profiles, read_task_profiles_file

NOW = datetime(2026, 8, 29, 12, 0, 0, tzinfo=UTC)
QUEUE = QueueSettings()
EXECUTION = ExecutionSettings()


@pytest.fixture
def database(tmp_path: Path) -> Iterator[Database]:
    handle = Database.from_url(f"sqlite:///{tmp_path / 'queue.sqlite3'}")
    ensure_ready(handle, auto_migrate=True)
    import_task_profiles(handle, read_task_profiles_file(), now=NOW)
    discover_models(
        handle, FakeProvider(FakeScript(models=(_model(), _model("beta:8b", "b" * 64)))), now=NOW
    )
    try:
        yield handle
    finally:
        handle.close()


@pytest.fixture
def sink() -> JobEventSink:
    return JobEventSink()


def _submit(
    database: Database,
    sink: JobEventSink,
    *,
    now: datetime = NOW,
    queue: QueueSettings = QUEUE,
    **kwargs: Any,
) -> str:
    submission = JobSubmission(task="general.chat", prompt="hello", **kwargs)
    return enqueue(
        database, submission, now=now, queue_settings=queue, execution_settings=EXECUTION, sink=sink
    ).job_id


def _row(database: Database, job_id: str) -> Job:
    with database.read() as session:
        job = session.get_one(Job, job_id)
        session.expunge(job)
        return job


def _events(database: Database, job_id: str) -> list[tuple[int, str]]:
    with database.read() as session:
        return [
            (row.sequence, row.event_type)
            for row in session.execute(
                select(JobEvent).where(JobEvent.job_id == job_id).order_by(JobEvent.sequence)
            ).scalars()
        ]


# --- enqueue -------------------------------------------------------------------------------


def test_enqueue_writes_a_queued_job_with_its_event_and_idempotency_expiry(
    database: Database, sink: JobEventSink
) -> None:
    job_id = _submit(
        database, sink, job_class=JobClass.BACKGROUND, idempotency_key="k1", source="ideapress"
    )
    row = _row(database, job_id)
    assert row.state == "queued"
    assert row.job_class == "background"
    assert row.base_priority == row.effective_priority == 100
    assert row.queued_at == row.scheduled_for == row.created_at == NOW
    assert row.attempt == 0 and row.max_attempts == EXECUTION.max_attempts
    assert row.max_wait_seconds == QUEUE.max_wait_seconds
    assert row.idempotency_expires_at == NOW + timedelta(hours=24)
    assert row.lease_owner is None and row.lease_expires_at is None
    request = row.request_json
    assert isinstance(request, dict)
    assert request["messages"] == [{"role": "user", "content": "hello", "tool_call_id": None}]
    assert _events(database, job_id) == [(1, "job.queued")]
    record = get_job(database, job_id)
    assert record.state is JobState.QUEUED and record.source == "ideapress"


def test_a_repeated_idempotency_key_returns_the_same_job_per_caller(
    database: Database, sink: JobEventSink
) -> None:
    """api.md §4: keys are scoped per caller; the same key from another caller is a new job."""
    first = enqueue(
        database,
        JobSubmission(task="general.chat", prompt="a", idempotency_key="k", source="s1"),
        now=NOW,
        queue_settings=QUEUE,
        execution_settings=EXECUTION,
        sink=sink,
    )
    again = enqueue(
        database,
        JobSubmission(task="general.chat", prompt="a", idempotency_key="k", source="s1"),
        now=NOW + timedelta(minutes=5),
        queue_settings=QUEUE,
        execution_settings=EXECUTION,
        sink=sink,
    )
    other = enqueue(
        database,
        JobSubmission(task="general.chat", prompt="a", idempotency_key="k", source="s2"),
        now=NOW,
        queue_settings=QUEUE,
        execution_settings=EXECUTION,
        sink=sink,
    )
    assert first.created and not again.created and other.created
    assert again.job_id == first.job_id and other.job_id != first.job_id


def test_an_expired_idempotency_key_is_released_and_starts_new_work(
    database: Database, sink: JobEventSink
) -> None:
    first = _submit(database, sink, idempotency_key="k", now=NOW)
    later = NOW + timedelta(hours=24, seconds=1)
    second = _submit(database, sink, idempotency_key="k", now=later)
    assert second != first
    assert _row(database, first).idempotency_key is None  # released, the job itself kept
    assert _row(database, second).idempotency_key == "k"


def test_concurrent_submissions_with_one_key_create_exactly_one_job(
    database: Database, sink: JobEventSink
) -> None:
    ids: list[str] = []
    lock = threading.Lock()
    start = threading.Barrier(6)

    def submit() -> None:
        start.wait()
        job_id = _submit(database, sink, idempotency_key="race", source="s")
        with lock:
            ids.append(job_id)

    threads = [threading.Thread(target=submit) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(set(ids)) == 1
    with database.read() as session:
        assert session.execute(select(Job).where(Job.idempotency_key == "race")).scalars().all()


def test_queue_full_refuses_above_max_depth_and_names_the_numbers(
    database: Database, sink: JobEventSink
) -> None:
    small = QueueSettings(max_depth=2)
    _submit(database, sink, queue=small)
    _submit(database, sink, queue=small)
    with pytest.raises(QueueFull) as excinfo:
        _submit(database, sink, queue=small)
    assert excinfo.value.details == {"active": 2, "max_depth": 2}


def test_enqueue_refuses_a_priority_outside_the_band_and_an_unknown_task(
    database: Database, sink: JobEventSink
) -> None:
    with pytest.raises(ValidationError):
        _submit(database, sink, job_class=JobClass.BATCH, priority=500)
    with pytest.raises(TaskProfileNotFound):
        enqueue(
            database,
            JobSubmission(task="no.such", prompt="x"),
            now=NOW,
            queue_settings=QUEUE,
            execution_settings=EXECUTION,
            sink=sink,
        )
    with database.read() as session:
        assert session.execute(select(Job)).scalars().all() == []


# --- claim ---------------------------------------------------------------------------------


def test_claim_takes_highest_priority_then_oldest_and_leaves_attempt_alone(
    database: Database, sink: JobEventSink
) -> None:
    normal_old = _submit(database, sink, now=NOW)
    normal_new = _submit(database, sink, now=NOW + timedelta(seconds=1))
    interactive = _submit(
        database, sink, job_class=JobClass.INTERACTIVE, now=NOW + timedelta(seconds=2)
    )
    later = NOW + timedelta(seconds=10)
    order = []
    for _ in range(3):
        claimed = claim(database, owner="w1", now=later, lease_seconds=60, sink=sink)
        assert claimed is not None
        order.append(claimed.job_id)
        assert claimed.attempt == 0
        assert claimed.lease_expires_at == later + timedelta(seconds=60)
    assert order == [interactive, normal_old, normal_new]
    assert claim(database, owner="w1", now=later, lease_seconds=60, sink=sink) is None
    row = _row(database, interactive)
    assert row.state == "leased" and row.lease_owner == "w1" and row.attempt == 0
    assert _events(database, interactive) == [(1, "job.queued"), (2, "job.leased")]


def test_claim_respects_scheduled_for(database: Database, sink: JobEventSink) -> None:
    job_id = _submit(database, sink)
    with database.write() as session:
        session.execute(
            update(Job).where(Job.id == job_id).values(scheduled_for=NOW + timedelta(seconds=30))
        )
    assert (
        claim(database, owner="w", now=NOW + timedelta(seconds=29), lease_seconds=60, sink=sink)
        is None
    )
    claimed = claim(
        database, owner="w", now=NOW + timedelta(seconds=30), lease_seconds=60, sink=sink
    )
    assert claimed is not None and claimed.job_id == job_id


def test_atomic_claiming_under_concurrent_workers_never_double_claims(
    database: Database, sink: JobEventSink
) -> None:
    """Stress: eight workers, two hundred jobs, every job claimed exactly once."""
    total = 200
    for index in range(total):
        _submit(database, sink, now=NOW + timedelta(milliseconds=index))
    claimed_by: dict[str, list[str]] = {}
    lock = threading.Lock()
    start = threading.Barrier(8)

    def worker(name: str) -> None:
        start.wait()
        while True:
            job = claim(
                database, owner=name, now=NOW + timedelta(seconds=1), lease_seconds=60, sink=sink
            )
            if job is None:
                return
            with lock:
                claimed_by.setdefault(job.job_id, []).append(name)

    threads = [threading.Thread(target=worker, args=(f"w{i}",)) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(claimed_by) == total
    assert all(len(owners) == 1 for owners in claimed_by.values())
    with database.read() as session:
        leased = (
            session.execute(select(Job.lease_owner).where(Job.state == "leased")).scalars().all()
        )
    assert len(leased) == total
    assert len(set(leased)) > 1  # the work was actually shared, not taken by one thread


def test_affinity_prefers_a_resident_model_within_the_top_priority_tie_only(
    database: Database, sink: JobEventSink
) -> None:
    """Queue §6: affinity reorders inside a tie, never across priorities; the streak is bounded."""
    alpha = resolve_model_id(database, "fake/alpha:8b@sha256:" + "a" * 12)
    beta = resolve_model_id(database, "fake/beta:8b@sha256:" + "b" * 12)
    assert alpha and beta
    older_beta = _submit(database, sink, now=NOW)
    newer_alpha = _submit(database, sink, now=NOW + timedelta(seconds=1))
    higher_beta = _submit(
        database, sink, job_class=JobClass.INTERACTIVE, now=NOW + timedelta(seconds=2)
    )
    with database.write() as session:
        session.execute(update(Job).where(Job.id == older_beta).values(selected_model_id=beta))
        session.execute(update(Job).where(Job.id == newer_alpha).values(selected_model_id=alpha))
        session.execute(update(Job).where(Job.id == higher_beta).values(selected_model_id=beta))
    hint = AffinityHint(resident_model_ids=frozenset({alpha}), streak=0, max_streak=5)
    later = NOW + timedelta(seconds=5)
    # The interactive job outranks everything: affinity cannot reach across the priority gap.
    first = claim(database, owner="w", now=later, lease_seconds=60, sink=sink, affinity=hint)
    assert first is not None and first.job_id == higher_beta and not first.by_affinity
    # Within the normal tie, the resident model's job wins over the older one.
    second = claim(database, owner="w", now=later, lease_seconds=60, sink=sink, affinity=hint)
    assert second is not None and second.job_id == newer_alpha and second.by_affinity
    # With the streak exhausted, plain priority order applies again.
    third_id = _submit(database, sink, now=NOW)
    with database.write() as session:
        session.execute(update(Job).where(Job.id == third_id).values(selected_model_id=alpha))
    exhausted = AffinityHint(resident_model_ids=frozenset({alpha}), streak=5, max_streak=5)
    third = claim(database, owner="w", now=later, lease_seconds=60, sink=sink, affinity=exhausted)
    assert third is not None and third.job_id == older_beta and not third.by_affinity


# --- transitions -----------------------------------------------------------------------------


def test_transition_is_a_compare_and_set_on_state_and_lease_owner(
    database: Database, sink: JobEventSink
) -> None:
    job_id = _submit(database, sink)
    claimed = claim(database, owner="w1", now=NOW, lease_seconds=60, sink=sink)
    assert claimed is not None
    with pytest.raises(TransitionRefused):
        move(
            database,
            sink,
            job_id,
            current=JobState.LEASED,
            target=JobState.ADMITTED,
            now=NOW,
            owner="w2",
        )
    with pytest.raises(TransitionRefused):
        move(database, sink, job_id, current=JobState.QUEUED, target=JobState.LEASED, now=NOW)
    with pytest.raises(IllegalTransition):
        move(
            database,
            sink,
            job_id,
            current=JobState.LEASED,
            target=JobState.COMPLETED,
            now=NOW,
            owner="w1",
        )
    assert _row(database, job_id).state == "leased"  # nothing above touched the row
    move(
        database,
        sink,
        job_id,
        current=JobState.LEASED,
        target=JobState.ADMITTED,
        now=NOW,
        owner="w1",
    )
    assert _row(database, job_id).state == "admitted"
    assert _events(database, job_id)[-1] == (3, "job.admitted")


def test_moving_to_a_waiting_or_terminal_state_releases_the_lease(
    database: Database, sink: JobEventSink
) -> None:
    job_id = _submit(database, sink)
    claim(database, owner="w1", now=NOW, lease_seconds=60, sink=sink)
    move(
        database,
        sink,
        job_id,
        current=JobState.LEASED,
        target=JobState.WAITING_RESOURCES,
        now=NOW,
        owner="w1",
    )
    row = _row(database, job_id)
    assert row.state == "waiting_resources"
    assert row.lease_owner is None and row.lease_expires_at is None


# --- leases ----------------------------------------------------------------------------------


def test_renew_extends_owned_leases_and_reports_the_lost_ones(
    database: Database, sink: JobEventSink
) -> None:
    mine = _submit(database, sink)
    theirs = _submit(database, sink, now=NOW + timedelta(seconds=1))
    claimed_at = NOW + timedelta(seconds=2)
    assert claim(database, owner="w1", now=claimed_at, lease_seconds=60, sink=sink) is not None
    assert claim(database, owner="w2", now=claimed_at, lease_seconds=60, sink=sink) is not None
    lost = renew_leases(
        database,
        owner="w1",
        job_ids=[mine, theirs, "01NOPE0000000000000000000"],
        now=NOW + timedelta(seconds=20),
        lease_seconds=60,
    )
    assert lost == frozenset({theirs, "01NOPE0000000000000000000"})
    assert _row(database, mine).lease_expires_at == NOW + timedelta(seconds=80)
    assert _row(database, theirs).lease_expires_at == claimed_at + timedelta(seconds=60)


def test_lease_expiry_requeues_idempotent_work_and_fails_the_rest_with_worker_lost(
    database: Database, sink: JobEventSink
) -> None:
    idempotent = _submit(database, sink, idempotent=True)
    fragile = _submit(database, sink, idempotent=False, now=NOW + timedelta(seconds=1))
    fresh = _submit(database, sink, now=NOW + timedelta(seconds=2))
    for _ in range(2):
        assert (
            claim(
                database, owner="dead", now=NOW + timedelta(seconds=1), lease_seconds=60, sink=sink
            )
            is not None
        )
    assert (
        claim(database, owner="alive", now=NOW + timedelta(seconds=50), lease_seconds=60, sink=sink)
        is not None
    )
    with database.write() as session:  # the dead worker had advanced the counter mid-flight
        session.execute(update(Job).where(Job.id == idempotent).values(attempt=2))

    reaped_at = NOW + timedelta(seconds=62)  # both dead leases expired at NOW + 61 s
    summary = reap_expired_leases(database, now=reaped_at, sink=sink)
    assert summary.requeued == (idempotent,) and summary.failed == (fragile,)
    requeued = _row(database, idempotent)
    assert requeued.state == "queued" and requeued.state_reason == "lease_expired"
    assert requeued.lease_owner is None and requeued.lease_expires_at is None
    assert requeued.attempt == 2  # untouched: the next attempt continues the sequence
    assert requeued.scheduled_for == reaped_at
    failed = _row(database, fragile)
    assert failed.state == "failed" and failed.state_reason == "worker_lost"
    assert failed.error_code == "WORKER_LOST" and failed.completed_at == reaped_at
    assert _row(database, fresh).state == "leased"  # unexpired, untouched
    assert _events(database, idempotent)[-1] == (3, "job.queued")
    assert _events(database, fragile)[-1] == (3, "job.failed")
    # Idempotent: a second reap changes nothing.
    again = reap_expired_leases(database, now=NOW + timedelta(seconds=63), sink=sink)
    assert again.requeued == () and again.failed == ()


# --- the ageing sweep ------------------------------------------------------------------------


def test_ageing_sweep_matches_the_domain_formula_row_for_row_and_is_idempotent(
    database: Database, sink: JobEventSink
) -> None:
    settings = QueueSettings(ageing_priority_per_minute=2.0, overflow_allowance=50)
    ages_minutes = [0, 0.5, 1, 7.4, 30, 300, 10_000]
    classes = [JobClass.BATCH, JobClass.BACKGROUND, JobClass.NORMAL, JobClass.INTERACTIVE]
    expected: dict[str, int] = {}
    for index, minutes in enumerate(ages_minutes):
        for job_class in classes:
            queued_at = NOW - timedelta(minutes=minutes)
            job_id = _submit(database, sink, job_class=job_class, now=queued_at, queue=settings)
            if index % 2:  # time in waiting_resources counts as waiting (queued_at is the origin)
                with database.write() as session:
                    session.execute(
                        update(Job).where(Job.id == job_id).values(state="waiting_resources")
                    )
            expected[job_id] = effective_priority(
                base=_row(database, job_id).base_priority,
                job_class=job_class,
                waiting_seconds=minutes * 60,
                ageing_priority_per_minute=2.0,
                overflow_allowance=50,
            )
    executing = _submit(database, sink, now=NOW - timedelta(hours=5), queue=settings)
    with database.write() as session:
        session.execute(update(Job).where(Job.id == executing).values(state="executing"))

    changed = ageing_sweep(database, now=NOW, settings=settings)
    assert changed == sum(
        1 for job_id, value in expected.items() if _row(database, job_id).base_priority != value
    )
    for job_id, value in expected.items():
        assert _row(database, job_id).effective_priority == value, job_id
    assert _row(database, executing).effective_priority == 400  # not a waiting state: untouched
    assert ageing_sweep(database, now=NOW, settings=settings) == 0


def test_max_wait_expiry_fails_waiting_jobs_with_the_bound_named(
    database: Database, sink: JobEventSink
) -> None:
    short = _submit(database, sink, max_wait_seconds=10)
    default = _submit(database, sink)
    expired = expire_max_wait(
        database, now=NOW + timedelta(seconds=11), default_max_wait_seconds=3600, sink=sink
    )
    assert expired == (short,)
    row = _row(database, short)
    assert row.state == "failed" and row.state_reason == "MAX_WAIT_EXCEEDED"
    assert row.error_text == "waited longer than max_wait_seconds (10)"
    assert _row(database, default).state == "queued"
    assert _events(database, short)[-1] == (2, "job.failed")


def test_queue_snapshot_counts_depth_oldest_age_and_starvation(
    database: Database, sink: JobEventSink
) -> None:
    _submit(database, sink, now=NOW - timedelta(minutes=45))  # past half of a 60 min bound
    _submit(database, sink, now=NOW - timedelta(minutes=5), job_class=JobClass.BATCH)
    claimed_id = _submit(
        database, sink, now=NOW - timedelta(minutes=1), job_class=JobClass.INTERACTIVE
    )
    claim(database, owner="w", now=NOW, lease_seconds=60, sink=sink)
    snapshot = queue_snapshot(database, now=NOW, default_max_wait_seconds=3600)
    assert snapshot.depth_by_state == {"queued": 2, "leased": 1}
    assert snapshot.depth_by_class == {"normal": 1, "batch": 1, "interactive": 1}
    assert snapshot.oldest_queued_age_seconds == 45 * 60
    assert snapshot.starving == 1
    assert snapshot.active == 3
    assert _row(database, claimed_id).state == "leased"


# --- query plans (data model §4) -------------------------------------------------------------


def _plan(database: Database, statement: str) -> list[str]:
    with database.engine.connect() as connection:
        return [
            str(row[3]) for row in connection.execute(text("EXPLAIN QUERY PLAN " + statement)).all()
        ]


def _compiled(statement: Any, database: Database) -> str:
    return str(statement.compile(database.engine, compile_kwargs={"literal_binds": True}))


def test_query_plans_use_their_indexes_and_never_scan_jobs(database: Database) -> None:
    from loadcoach.services import queue as queue_module

    if database.engine.dialect.name != "sqlite":  # pragma: no cover — plan syntax is per dialect
        pytest.skip("EXPLAIN QUERY PLAN is SQLite's")
    claim_select = (
        select(Job.id)
        .where(Job.state == "queued", Job.scheduled_for <= NOW)
        .order_by(Job.effective_priority.desc(), Job.created_at.asc())
        .limit(1)
    )
    plan = _plan(database, _compiled(claim_select, database))
    assert any("ix_jobs_state_effective_priority_created_at" in step for step in plan), plan
    assert not any("TEMP B-TREE" in step for step in plan), plan
    assert not any(step.startswith("SCAN") for step in plan), plan

    with database.write() as session:
        now_param = bindparam("now", value=NOW, type_=UtcDateTime())
        seconds = queue_module._waiting_seconds(session, now_param)  # noqa: SLF001 — the real expression
        sweep = (
            update(Job)
            .where(Job.state.in_(["queued", "waiting_resources"]), Job.effective_priority != 5)
            .values(effective_priority=Job.base_priority + seconds)
        )
        sweep_sql = _compiled(sweep, database)
    plan = _plan(database, sweep_sql)
    assert any("ix_jobs_state" in step for step in plan), plan
    assert not any(step.startswith("SCAN") for step in plan), plan

    reap = select(Job.id, Job.state, Job.idempotent, Job.lease_owner).where(
        Job.lease_expires_at < NOW
    )
    plan = _plan(database, _compiled(reap, database))
    assert any("ix_jobs_lease_expires_at" in step for step in plan), plan
    assert not any(step.startswith("SCAN") for step in plan), plan
