"""Queue budgets (performance-targets §3.3): enqueue ≤ 15 ms. Marked ``performance``."""

from __future__ import annotations

import statistics
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from modelrack.testing import FakeProvider, FakeScript
from tests.integration.test_generate import _model

from loadcoach.config import ExecutionSettings, QueueSettings
from loadcoach.services.database import Database, ensure_ready
from loadcoach.services.job_events import JobEventSink
from loadcoach.services.models import discover_models
from loadcoach.services.queue import JobSubmission, enqueue
from loadcoach.services.task_profiles import import_task_profiles, read_task_profiles_file

pytestmark = pytest.mark.performance

ENQUEUE_BUDGET_MS = 15
_WARMUP = 5
_MEASURED = 50


def test_enqueue_commits_within_its_budget(tmp_path: Path) -> None:
    now = datetime(2026, 8, 29, tzinfo=UTC)
    database = Database.from_url(f"sqlite:///{tmp_path / 'perf.sqlite3'}")
    ensure_ready(database, auto_migrate=True)
    import_task_profiles(database, read_task_profiles_file(), now=now)
    discover_models(database, FakeProvider(FakeScript(models=(_model(),))), now=now)
    sink = JobEventSink()
    durations: list[float] = []
    for index in range(_WARMUP + _MEASURED):
        started = time.perf_counter()
        enqueue(
            database,
            JobSubmission(task="general.chat", prompt=f"job {index}", idempotency_key=f"k{index}"),
            now=now,
            queue_settings=QueueSettings(),
            execution_settings=ExecutionSettings(),
            sink=sink,
        )
        if index >= _WARMUP:
            durations.append((time.perf_counter() - started) * 1000)
    median = statistics.median(durations)
    p95 = sorted(durations)[int(len(durations) * 0.95)]
    print(f"enqueue median {median:.2f} ms, p95 {p95:.2f} ms")  # noqa: T201 — the report
    assert median <= ENQUEUE_BUDGET_MS, (
        f"median enqueue {median:.2f} ms exceeds {ENQUEUE_BUDGET_MS} ms"
    )
    database.close()


DISPATCH_BUDGET_MS = 100
IDLE_CPU_BUDGET_FRACTION = 0.005


def _runtime(tmp_path: Path) -> Any:
    from loadcoach.config import ProviderSettings, Settings, StorageSettings
    from loadcoach.services.worker import build_runtime

    settings = Settings(
        storage=StorageSettings(database_url=f"sqlite:///{tmp_path / 'perf-runtime.sqlite3'}"),
        provider=ProviderSettings(kind="fake"),
    )
    url = settings.storage.database_url
    assert url is not None
    database = Database.from_url(url)
    ensure_ready(database, auto_migrate=True)
    now = datetime(2026, 8, 29, tzinfo=UTC)
    import_task_profiles(database, read_task_profiles_file(), now=now)
    provider = FakeProvider(FakeScript(models=(_model(),)))
    discover_models(database, provider, now=now)
    return build_runtime(
        settings, database=database, provider=provider, sink=JobEventSink(), snapshot=lambda: None
    )


def test_dispatch_latency_with_an_idle_worker_stays_within_its_budget(tmp_path: Path) -> None:
    from loadcoach.services.queue import get_job

    runtime = _runtime(tmp_path)
    runtime.start()
    try:
        time.sleep(1.5)  # let the worker back off to its idle poll
        latencies: list[float] = []
        for index in range(10):
            submitted = time.perf_counter()
            job_id = enqueue(
                runtime.database,
                JobSubmission(task="general.chat", prompt=f"dispatch {index}"),
                now=datetime.now(UTC),
                queue_settings=runtime.settings.queue,
                execution_settings=runtime.settings.execution,
                sink=runtime.sink,
                wakeup=runtime.wakeup,
            ).job_id
            deadline = time.perf_counter() + 5
            while time.perf_counter() < deadline:
                record = get_job(runtime.database, job_id)
                if record.started_at is not None:
                    break
                time.sleep(0.002)
            latencies.append((time.perf_counter() - submitted) * 1000)
            time.sleep(1.2)  # back to idle before the next measurement
        median = statistics.median(latencies)
        print(f"dispatch median {median:.1f} ms, max {max(latencies):.1f} ms")  # noqa: T201 — the report
        assert median <= DISPATCH_BUDGET_MS
    finally:
        runtime.stop()
        runtime.database.close()


def test_idle_poll_cpu_stays_within_its_budget(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    runtime.start()
    try:
        time.sleep(1.5)  # settle into the idle poll
        cpu_before = time.process_time()
        wall_before = time.perf_counter()
        time.sleep(5.0)
        cpu = time.process_time() - cpu_before
        wall = time.perf_counter() - wall_before
    finally:
        runtime.stop()
        runtime.database.close()
    fraction = cpu / wall
    print(f"idle poll CPU {fraction * 100:.3f}% of one core over {wall:.1f} s")  # noqa: T201 — the report
    assert fraction <= IDLE_CPU_BUDGET_FRACTION


RECOVERY_BUDGET_SECONDS = 2.0


def test_recovery_of_a_thousand_in_flight_jobs_stays_within_its_budget(tmp_path: Path) -> None:
    from sqlalchemy import update

    from loadcoach.infrastructure.db.models import Job
    from loadcoach.services.recovery import recover

    runtime = _runtime(tmp_path)
    database = runtime.database
    sink = runtime.sink
    now = datetime.now(UTC)
    job_ids = [
        enqueue(
            database,
            JobSubmission(task="general.chat", prompt=f"job {index}", idempotent=index % 5 != 0),
            now=now,
            queue_settings=runtime.settings.queue,
            execution_settings=runtime.settings.execution,
            sink=sink,
        ).job_id
        for index in range(1000)
    ]
    # A dead process held every one of them, in a spread of in-flight states.
    states = ["leased", "admitted", "executing", "validating", "retrying", "cancelling"]
    with database.write() as session:
        for index, job_id in enumerate(job_ids):
            session.execute(
                update(Job)
                .where(Job.id == job_id)
                .values(
                    state=states[index % len(states)],
                    lease_owner="dead/worker-0",
                    lease_expires_at=now,
                )
            )
    started = time.perf_counter()
    summary = recover(
        database, sink, now=now, owner_prefix="alive", queue_settings=runtime.settings.queue
    )
    elapsed = time.perf_counter() - started
    print(f"recovery of 1000 in-flight jobs: {elapsed:.3f} s")  # noqa: T201 — the report
    assert summary.touched == 1000
    assert elapsed <= RECOVERY_BUDGET_SECONDS
    database.close()
