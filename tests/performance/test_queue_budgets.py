"""Queue budgets (performance-targets §3.3): enqueue ≤ 15 ms. Marked ``performance``."""

from __future__ import annotations

import statistics
import time
from datetime import UTC, datetime
from pathlib import Path

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
