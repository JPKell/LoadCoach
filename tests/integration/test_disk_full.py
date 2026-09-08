"""Degradation: disk full mid-job (graceful-degradation.md, row "Disk full").

Documented behaviour: "Job fails; queue preserved." The write that would hit a full disk is the
one ``_complete``/``_fail`` make in ``services/worker.py`` — one transaction that moves a job's
``state`` and appends its terminal event (``JobEventSink.write``). ``transition()`` (``services/
queue.py``) does that move as a Core-level compare-and-set ``UPDATE`` rather than through an ORM
object, so the seam is the engine's own cursor-execute hook rather than a session-level one.
Rather than filling a real disk or patching ``os.write`` globally, this attaches a SQLAlchemy
engine event to the runtime's own engine (``Database.engine``, the seam the application already
owns) that raises ``sqlite3.OperationalError("database or disk is full")`` — SQLite's own wording
for ``ENOSPC`` — the moment a job row is about to be written to ``completed``. Every other write
(claiming, admitting, transitioning to ``executing``, the second job's own completion) is
untouched.
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from modelrack.testing import FakeGeneration, FakeProvider, FakeScript
from sqlalchemy import event
from tests.integration.test_generate import _model

from loadcoach.config import ExecutionSettings, ProviderSettings, Settings, StorageSettings
from loadcoach.domain.queue_state import JobState
from loadcoach.services.database import Database, ensure_ready
from loadcoach.services.job_events import JobEventSink
from loadcoach.services.models import discover_models
from loadcoach.services.queue import JobSubmission, enqueue, get_job
from loadcoach.services.task_profiles import import_task_profiles, read_task_profiles_file
from loadcoach.services.worker import QueueRuntime, build_runtime

NOW = datetime(2026, 8, 29, 12, 0, 0, tzinfo=UTC)


def _fail_once_on_completion(fired: dict[str, bool]) -> Any:
    """A disk fills once and is noticed once — the fault must not haunt every later write."""

    def hook(
        conn: Any, cursor: Any, statement: str, parameters: Any, context: Any, executemany: bool
    ) -> None:
        if fired["value"] or "UPDATE jobs" not in statement:
            return
        values = parameters.values() if isinstance(parameters, dict) else parameters
        if JobState.COMPLETED.value in values:
            fired["value"] = True
            raise sqlite3.OperationalError("database or disk is full")

    return hook


@pytest.fixture
def runtime(tmp_path: Path) -> Iterator[QueueRuntime]:
    settings = Settings(
        storage=StorageSettings(database_url=f"sqlite:///{tmp_path / 'worker.sqlite3'}"),
        provider=ProviderSettings(kind="fake"),
        execution=ExecutionSettings(max_concurrent_jobs=1),
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
    # Attached before any job runs: only the *first* UPDATE that would move a job to `completed`
    # sees the fault, so the earlier claim/admit/attempt writes — and every write after the one
    # disk-full moment — are all real.
    event.listen(
        database.engine, "before_cursor_execute", _fail_once_on_completion({"value": False})
    )
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


def test_a_disk_full_completion_write_fails_the_job_and_preserves_the_queue(
    runtime: QueueRuntime,
) -> None:
    failing_job_id = enqueue(
        runtime.database,
        JobSubmission(task="general.chat", prompt="the one that hits the full disk"),
        now=datetime.now(UTC),
        queue_settings=runtime.settings.queue,
        execution_settings=runtime.settings.execution,
        sink=runtime.sink,
        wakeup=runtime.wakeup,
    ).job_id

    assert _wait_terminal(runtime, failing_job_id) is JobState.FAILED
    failed = get_job(runtime.database, failing_job_id)
    assert failed.error_code is not None
    assert failed.lease_owner is None  # the crash handler released it rather than leaving it held

    # The queue itself is undamaged: a second job, enqueued after the first crashed, runs and
    # completes normally through the very same runtime and session factory.
    healthy_job_id = enqueue(
        runtime.database,
        JobSubmission(task="general.chat", prompt="an ordinary job right after"),
        now=datetime.now(UTC),
        queue_settings=runtime.settings.queue,
        execution_settings=runtime.settings.execution,
        sink=runtime.sink,
        wakeup=runtime.wakeup,
    ).job_id
    assert _wait_terminal(runtime, healthy_job_id) is JobState.COMPLETED
    assert get_job(runtime.database, healthy_job_id).response_text == "answer"
