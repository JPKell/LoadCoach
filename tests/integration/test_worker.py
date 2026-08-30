"""The production runtime on real threads: workers, the scheduler and the wake-up.

The simulator proves the properties; this file proves the same loop runs on real threads with a
real ``threading.Event`` and a wall clock — the two things the simulator replaces.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from modelrack.testing import FakeGeneration, FakeProvider, FakeScript
from tests.integration.test_generate import _model

from loadcoach.config import (
    ExecutionSettings,
    ProviderSettings,
    Settings,
    StorageSettings,
)
from loadcoach.domain.queue_state import JobState
from loadcoach.services.database import Database, ensure_ready
from loadcoach.services.job_events import JobEventSink
from loadcoach.services.models import discover_models
from loadcoach.services.queue import JobSubmission, enqueue, get_job
from loadcoach.services.task_profiles import import_task_profiles, read_task_profiles_file
from loadcoach.services.worker import QueueRuntime, build_runtime

NOW = datetime(2026, 8, 29, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def runtime(tmp_path: Path) -> Iterator[QueueRuntime]:
    settings = Settings(
        storage=StorageSettings(database_url=f"sqlite:///{tmp_path / 'worker.sqlite3'}"),
        provider=ProviderSettings(kind="fake"),
        execution=ExecutionSettings(max_concurrent_jobs=2),
    )
    url = settings.storage.database_url
    assert url is not None
    database = Database.from_url(url)
    ensure_ready(database, auto_migrate=True)
    import_task_profiles(database, read_task_profiles_file(), now=NOW)
    provider = FakeProvider(
        FakeScript(
            models=(_model(),),
            generations=(FakeGeneration(text="answer"),),
            repeat_final_generation=True,
        )
    )
    discover_models(database, provider, now=NOW)
    built = build_runtime(
        settings, database=database, provider=provider, sink=JobEventSink(), snapshot=lambda: None
    )
    built.start()
    try:
        yield built
    finally:
        built.stop()
        database.close()


def _wait_terminal(
    runtime: QueueRuntime, job_id: str, *, timeout_seconds: float = 10.0
) -> JobState:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        state = get_job(runtime.database, job_id).state
        if state in {JobState.COMPLETED, JobState.FAILED, JobState.CANCELLED}:
            return state
        time.sleep(0.02)
    pytest.fail(f"job {job_id} did not reach a terminal state within {timeout_seconds}s")


def test_real_threads_run_queued_jobs_to_completion(runtime: QueueRuntime) -> None:
    job_ids = [
        enqueue(
            runtime.database,
            JobSubmission(task="general.chat", prompt=f"job {index}"),
            now=datetime.now(UTC),
            queue_settings=runtime.settings.queue,
            execution_settings=runtime.settings.execution,
            sink=runtime.sink,
            wakeup=runtime.wakeup,
        ).job_id
        for index in range(5)
    ]
    for job_id in job_ids:
        assert _wait_terminal(runtime, job_id) is JobState.COMPLETED
    records = [get_job(runtime.database, job_id) for job_id in job_ids]
    assert all(record.response_text == "answer" for record in records)
    assert all(record.attempt == 1 for record in records)
    assert {record.lease_owner for record in records} == {None}
    assert len(runtime.in_flight) == 0


def test_stop_returns_promptly_with_an_idle_queue(runtime: QueueRuntime) -> None:
    started = time.monotonic()
    runtime.stop()
    assert time.monotonic() - started < 3.0
