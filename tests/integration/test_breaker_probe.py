"""The breaker's probe is marked when execution starts, and handed back if it never reports.

Found by a mutation check rather than by a failing test: with the probe never marked at all,
every P7 test still passed, because none watched the runtime while an attempt was in flight.
These do, on real threads. The P5 worker marked the probe at *routing* time for every ranked
candidate, so a fallback that never ran left the model excluded until nothing ever reported —
the phase's own named failure mode (a good model excluded for ever after a bad day).
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from modelrack.testing import FakeGeneration, FakeProvider, FakeScript
from sqlalchemy import select
from tests.integration.test_generate import _model
from tests.integration.test_worker import _wait_terminal

from loadcoach.config import ExecutionSettings, ProviderSettings, Settings, StorageSettings
from loadcoach.domain.circuit_breaker import AttemptSample, BreakerState, BreakerVerdict
from loadcoach.domain.queue_state import JobState
from loadcoach.infrastructure.db.models import JobAttempt
from loadcoach.services.database import Database, ensure_ready
from loadcoach.services.job_events import JobEventSink
from loadcoach.services.models import discover_models
from loadcoach.services.queue import JobSubmission, cancel_job, enqueue, get_job
from loadcoach.services.task_profiles import import_task_profiles, read_task_profiles_file
from loadcoach.services.worker import QueueRuntime, build_runtime

CANONICAL = f"fake/{_model().name}@sha256:{(_model().digest or '')[:12]}"


@pytest.fixture
def runtime(tmp_path: Path) -> Iterator[QueueRuntime]:
    """One worker, a generation that takes real time, and a breaker already half-open."""
    yield from _build_runtime(tmp_path, workers=1)


@pytest.fixture
def two_workers(tmp_path: Path) -> Iterator[QueueRuntime]:
    """Two real worker threads against the same half-open model — F2's arena."""
    yield from _build_runtime(tmp_path, workers=2)


def _build_runtime(tmp_path: Path, *, workers: int) -> Iterator[QueueRuntime]:
    settings = Settings(
        storage=StorageSettings(database_url=f"sqlite:///{tmp_path / 'probe.sqlite3'}"),
        provider=ProviderSettings(kind="fake"),
        execution=ExecutionSettings(max_concurrent_jobs=workers),
    )
    url = settings.storage.database_url
    assert url is not None
    database = Database.from_url(url)
    ensure_ready(database, auto_migrate=True)
    import_task_profiles(database, read_task_profiles_file(), now=datetime.now(UTC))
    provider = FakeProvider(
        FakeScript(
            models=(_model(),),
            generations=(FakeGeneration(text="answer", first_chunk_delay_ms=700),),
            repeat_final_generation=True,
        ),
        sleep=time.sleep,
    )
    discover_models(database, provider, now=datetime.now(UTC))
    built = build_runtime(
        settings,
        database=database,
        provider=provider,
        sink=JobEventSink(),
        snapshot=lambda: None,
        workers=workers,
    )
    # Five failures ending 360 s ago: opened then, cool-down (300 s) over 60 s ago → half-open.
    now = datetime.now(UTC)
    failures = [
        AttemptSample(at=now - timedelta(seconds=440 - 20 * i), succeeded=False) for i in range(5)
    ]
    built.breakers.update({CANONICAL: failures}, now=now)
    built.breakers.update({CANONICAL: failures}, now=now)
    assert _verdict(built).state is BreakerState.HALF_OPEN
    built.start()
    try:
        yield built
    finally:
        built.stop()
        database.close()


def _verdict(runtime: QueueRuntime) -> BreakerVerdict:
    return next(v for v in runtime.breakers.verdicts() if v.canonical_id == CANONICAL)


def _enqueue(runtime: QueueRuntime) -> str:
    return enqueue(
        runtime.database,
        JobSubmission(task="general.chat", prompt="probe me"),
        now=datetime.now(UTC),
        queue_settings=runtime.settings.queue,
        execution_settings=runtime.settings.execution,
        sink=runtime.sink,
        wakeup=runtime.wakeup,
    ).job_id


def _wait_for_probe(runtime: QueueRuntime, *, timeout_seconds: float = 5.0) -> BreakerVerdict:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        verdict = _verdict(runtime)
        if verdict.probe_in_flight:
            return verdict
        time.sleep(0.01)
    pytest.fail("the worker never marked the probe")


def test_the_probe_is_marked_when_execution_starts_and_closes_the_breaker_on_success(
    runtime: QueueRuntime,
) -> None:
    job_id = _enqueue(runtime)
    in_flight = _wait_for_probe(runtime)
    assert in_flight.state is BreakerState.HALF_OPEN
    assert in_flight.probe_started_at is not None
    assert in_flight.reason == "cool-down elapsed; probe in flight"
    assert runtime.breakers.excluded() == frozenset({CANONICAL})  # later jobs wait for it

    assert _wait_terminal(runtime, job_id) is JobState.COMPLETED
    runtime.refresh_breakers(datetime.now(UTC))
    closed = _verdict(runtime)
    assert closed.state is BreakerState.CLOSED and not closed.probe_in_flight
    assert closed.closed_at is not None and closed.samples == 0
    assert runtime.breakers.excluded() == frozenset()


def test_a_half_open_model_admits_exactly_one_probe_across_two_workers(
    two_workers: QueueRuntime,
) -> None:
    """F2 (M5C-2): routing-time exclusion alone cannot hold queue §7's "a single low-priority job".

    Two workers can both route while the breaker is half-open and unmarked, and each arrive at
    execution believing the model is theirs. The loser of ``allow_probe`` must not run: it falls
    to the next candidate, or — with none, as here — is requeued and deferred until the probe
    reports. Watched failing before the fix: both jobs executing on the model at once, attempts
    overlapping in time, ``probe_in_flight`` true throughout.
    """
    runtime = two_workers
    first = _enqueue(runtime)
    second = _enqueue(runtime)
    job_ids = (first, second)
    peak_executing = 0
    saw_probe = False
    terminal = {JobState.COMPLETED, JobState.FAILED, JobState.CANCELLED}
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        states = [get_job(runtime.database, job_id).state for job_id in job_ids]
        peak_executing = max(
            peak_executing, sum(1 for state in states if state is JobState.EXECUTING)
        )
        saw_probe = saw_probe or _verdict(runtime).probe_in_flight
        if all(state in terminal for state in states):
            break
        time.sleep(0.005)

    assert saw_probe, "no probe was ever marked"
    states = [get_job(runtime.database, job_id).state for job_id in job_ids]
    assert states == [JobState.COMPLETED, JobState.COMPLETED], states
    assert peak_executing <= 1, f"both jobs executed on the half-open model at once ({states})"

    # The attempt rows are the deterministic record: never two attempts in flight together.
    with runtime.database.read() as session:
        rows = sorted(
            session.execute(
                select(JobAttempt.started_at, JobAttempt.completed_at, JobAttempt.job_id)
            ).all(),
            key=lambda row: row[0],
        )
    assert len(rows) >= 2  # one attempt per job, at least
    assert {row[2] for row in rows} == set(job_ids)
    for earlier, later in zip(rows, rows[1:], strict=False):
        assert earlier[1] is not None and earlier[1] <= later[0], (
            f"attempts overlapped: {earlier} and {later}"
        )


def test_a_probe_cancelled_before_it_reports_is_handed_back(runtime: QueueRuntime) -> None:
    job_id = _enqueue(runtime)
    _wait_for_probe(runtime)
    cancel_job(
        runtime.database,
        runtime.sink,
        job_id,
        now=datetime.now(UTC),
        on_request=runtime.in_flight.request_cancel,
    )
    assert _wait_terminal(runtime, job_id) is JobState.CANCELLED
    runtime.refresh_breakers(datetime.now(UTC))
    released = _verdict(runtime)
    assert released.state is BreakerState.HALF_OPEN  # a cancellation says nothing about the model
    assert not released.probe_in_flight and released.probe_started_at is None
    assert runtime.breakers.excluded() == frozenset()
    assert runtime.breakers.allow_probe(CANONICAL, now=datetime.now(UTC)) is True
