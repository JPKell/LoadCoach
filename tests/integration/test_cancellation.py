"""Cancellation at the service boundary (queue §8), against a real database."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from modelrack.testing import FakeProvider, FakeScript
from tests.integration.test_generate import _model

from loadcoach.config import ExecutionSettings, QueueSettings
from loadcoach.domain.queue_state import JobState
from loadcoach.services.database import Database, ensure_ready
from loadcoach.services.job_events import JobEventSink
from loadcoach.services.models import discover_models
from loadcoach.services.queue import (
    JobNotCancellable,
    JobNotFound,
    JobSubmission,
    cancel_job,
    cancelling_since,
    claim,
    enqueue,
    get_job,
)
from loadcoach.services.task_profiles import import_task_profiles, read_task_profiles_file

NOW = datetime(2026, 8, 29, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def database(tmp_path: Path) -> Iterator[Database]:
    handle = Database.from_url(f"sqlite:///{tmp_path / 'cancel.sqlite3'}")
    ensure_ready(handle, auto_migrate=True)
    import_task_profiles(handle, read_task_profiles_file(), now=NOW)
    discover_models(handle, FakeProvider(FakeScript(models=(_model(),))), now=NOW)
    try:
        yield handle
    finally:
        handle.close()


def _submit(database: Database, sink: JobEventSink) -> str:
    return enqueue(
        database,
        JobSubmission(task="general.chat", prompt="x"),
        now=NOW,
        queue_settings=QueueSettings(),
        execution_settings=ExecutionSettings(),
        sink=sink,
    ).job_id


def test_a_queued_job_is_cancelled_at_once_and_a_second_cancel_is_refused(
    database: Database,
) -> None:
    sink = JobEventSink()
    job_id = _submit(database, sink)
    outcome = cancel_job(database, sink, job_id, now=NOW)
    assert outcome.state is JobState.CANCELLED and outcome.already is False
    record = get_job(database, job_id)
    assert record.state is JobState.CANCELLED
    assert record.cancel_requested is True and record.error_code == "GENERATION_CANCELLED"
    assert record.completed_at == NOW
    with pytest.raises(JobNotCancellable) as excinfo:
        cancel_job(database, sink, job_id, now=NOW)
    assert excinfo.value.code == "JOB_NOT_CANCELLABLE"
    with pytest.raises(JobNotFound):
        cancel_job(database, sink, "01NOPE0000000000000000000", now=NOW)


def test_a_leased_job_moves_to_cancelling_with_the_flag_set_and_stays_cancellable_idempotently(
    database: Database,
) -> None:
    """Between claim and provider call: ``leased -> cancelling`` (ADR-0029 §3)."""
    sink = JobEventSink()
    job_id = _submit(database, sink)
    assert claim(database, owner="w", now=NOW, lease_seconds=60, sink=sink) is not None
    outcome = cancel_job(database, sink, job_id, now=NOW)
    assert outcome.state is JobState.CANCELLING and outcome.already is False
    record = get_job(database, job_id)
    assert record.state is JobState.CANCELLING and record.cancel_requested is True
    assert record.lease_owner == "w"  # the worker still owns it until it completes the transition
    again = cancel_job(database, sink, job_id, now=NOW)
    assert again.state is JobState.CANCELLING and again.already is True
    assert cancelling_since(database) == ((job_id, NOW),)
