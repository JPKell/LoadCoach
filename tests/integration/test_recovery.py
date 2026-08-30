"""Recovery after a real ``kill -9`` (queue §10), and the recovery pass's own contract.

The simulator proves recovery from every lifecycle point over a fake clock; this file kills a
real process with SIGKILL at two of them — inside a provider call (``executing``) and inside a
model load (``admitted``) — and recovers in this process, because "the worker thread simply
stops existing" is the one event no simulation can stand in for.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest
from modelrack.testing import FakeGeneration, FakeProvider, FakeScript
from sqlalchemy import select
from tests.integration.test_generate import _model

from loadcoach.config import ExecutionSettings, ProviderSettings, Settings, StorageSettings
from loadcoach.domain.queue_state import JobState
from loadcoach.infrastructure.db.models import Job, JobAttempt
from loadcoach.services.database import Database
from loadcoach.services.job_events import JobEventSink
from loadcoach.services.queue import get_job
from loadcoach.services.recovery import recover
from loadcoach.services.worker import build_runtime

_CHILD = r'''
import sys, time
from datetime import UTC, datetime
from modelrack import ProviderCapabilities
from sweatmeter import GpuSample, TelemetrySnapshot
from modelrack.testing import FakeGeneration, FakeModel, FakeProvider, FakeScript
from loadcoach.config import ExecutionSettings, ProviderSettings, Settings, StorageSettings
from loadcoach.services.database import Database, ensure_ready
from loadcoach.services.job_events import JobEventSink
from loadcoach.services.models import discover_models
from loadcoach.services.queue import JobSubmission, enqueue, get_job
from loadcoach.services.task_profiles import import_task_profiles, read_task_profiles_file
from loadcoach.services.worker import build_runtime

url, stop_at, idempotent = sys.argv[1], sys.argv[2], sys.argv[3] == "1"
caps = ProviderCapabilities(streaming=True, token_counts=True, context_configurable=True,
                            force_unload=True, residency_query=True, json_mode=True,
                            structured_output=True)
model = FakeModel(name="alpha:8b", digest="a" * 64, family="alpha",
                  parameter_count=8_000_000_000, quantization="Q8_0", size_bytes=2 * 1024**3,
                  max_context=32768, layers=32, kv_heads=8, head_dim=128,
                  vram_bytes=2 * 1024**3)
generation = FakeGeneration(text="slow answer", first_chunk_delay_ms=600_000)
script = FakeScript(models=(model,), capabilities=caps, generations=(generation,))


class SlowLoad:
    """A provider whose load never finishes: the process is stuck in ``admitted``."""

    def __init__(self, inner):
        self._inner = inner

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def load(self, identity, profile):
        time.sleep(600)
        return self._inner.load(identity, profile)


now = datetime.now(UTC)
settings = Settings(storage=StorageSettings(database_url=url),
                    provider=ProviderSettings(kind="fake"),
                    execution=ExecutionSettings(max_concurrent_jobs=1))
database = Database.from_url(url)
ensure_ready(database, auto_migrate=True)
import_task_profiles(database, read_task_profiles_file(), now=now)
provider = FakeProvider(script, sleep=time.sleep)
discover_models(database, provider, now=now)
if stop_at == "admitted":
    provider = SlowLoad(provider)
sink = JobEventSink()
submission = JobSubmission(task="general.chat", prompt="hello", idempotent=idempotent)
job_id = enqueue(database, submission, now=now, queue_settings=settings.queue,
                 execution_settings=settings.execution, sink=sink).job_id


def snapshot():
    # One 16 GiB device: admission has somewhere to place the model, so residency loads it
    # before execution and the process can be caught in ``admitted``.
    return TelemetrySnapshot(timestamp=datetime.now(UTC), gpus=(GpuSample(index=0,
                             vram_total_bytes=16 * 1024**3, vram_used_bytes=0),))


runtime = build_runtime(settings, database=database, provider=provider, sink=sink,
                        snapshot=snapshot, owner_prefix="child")
runtime.start()
deadline = time.monotonic() + 30
while time.monotonic() < deadline:
    if get_job(database, job_id).state.value == stop_at:
        print(f"READY {job_id}", flush=True)
        break
    time.sleep(0.02)
else:
    print("TIMEOUT", flush=True)
time.sleep(3600)
'''


def _spawn(url: str, stop_at: str, *, idempotent: bool) -> tuple[subprocess.Popen[str], str]:
    child = subprocess.Popen(  # noqa: S603 — our own interpreter, our own script
        [sys.executable, "-c", _CHILD, url, stop_at, "1" if idempotent else "0"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=Path(__file__).resolve().parents[2],
    )
    assert child.stdout is not None
    line = child.stdout.readline().strip()
    if not line.startswith("READY "):
        child.kill()
        _, err = child.communicate(timeout=10)
        pytest.fail(f"child never reached {stop_at}: {line!r}\n{err}")
    return child, line.split(" ", 1)[1]


def _kill_minus_nine(child: subprocess.Popen[str]) -> None:
    os.kill(child.pid, signal.SIGKILL)
    child.wait(timeout=10)


def _runtime(url: str) -> tuple[Database, object]:
    settings = Settings(
        storage=StorageSettings(database_url=url),
        provider=ProviderSettings(kind="fake"),
        execution=ExecutionSettings(max_concurrent_jobs=1),
    )
    database = Database.from_url(url)
    provider = FakeProvider(
        FakeScript(
            models=(_model(),),
            generations=(FakeGeneration(text="recovered answer"),),
            repeat_final_generation=True,
        )
    )
    return database, build_runtime(
        settings, database=database, provider=provider, sink=JobEventSink(), snapshot=lambda: None
    )


@pytest.mark.parametrize("stop_at", ["executing", "admitted"])
def test_kill_minus_nine_is_recovered_and_the_job_completes_exactly_once(
    tmp_path: Path, stop_at: str
) -> None:
    url = f"sqlite:///{tmp_path / 'kill.sqlite3'}"
    child, job_id = _spawn(url, stop_at, idempotent=True)
    _kill_minus_nine(child)

    database, runtime = _runtime(url)
    try:
        before = get_job(database, job_id)
        assert before.state is JobState(stop_at)
        assert before.lease_owner is not None and before.lease_owner.startswith("child/")

        summary = recover(
            database,
            runtime.sink,  # type: ignore[attr-defined]
            now=datetime.now(UTC),
            owner_prefix=runtime.owner_prefix,  # type: ignore[attr-defined]
            queue_settings=runtime.settings.queue,  # type: ignore[attr-defined]
        )
        assert summary.requeued == (job_id,) and summary.failed == ()
        after = get_job(database, job_id)
        assert after.state is JobState.QUEUED and after.lease_owner is None
        assert after.state_reason == "recovered"
        assert after.attempt == 0  # the dead attempt was never written

        runtime.start()  # type: ignore[attr-defined]  # recovers again (idempotent) and runs it
        assert runtime.last_recovery is not None and runtime.last_recovery.touched == 0  # type: ignore[attr-defined]
        deadline = time.monotonic() + 15
        while (
            time.monotonic() < deadline
            and get_job(database, job_id).state is not JobState.COMPLETED
        ):
            time.sleep(0.02)
        final = get_job(database, job_id)
        assert final.state is JobState.COMPLETED
        assert final.response_text == "recovered answer"
        with database.read() as session:
            attempts = (
                session.execute(
                    select(JobAttempt.attempt)
                    .where(JobAttempt.job_id == job_id)
                    .order_by(JobAttempt.attempt)
                )
                .scalars()
                .all()
            )
        assert attempts == [1]
    finally:
        runtime.stop()  # type: ignore[attr-defined]
        database.close()


def test_kill_minus_nine_on_non_idempotent_work_fails_with_worker_lost_and_never_reruns(
    tmp_path: Path,
) -> None:
    url = f"sqlite:///{tmp_path / 'kill-fragile.sqlite3'}"
    child, job_id = _spawn(url, "executing", idempotent=False)
    _kill_minus_nine(child)
    database, runtime = _runtime(url)
    try:
        runtime.start()  # type: ignore[attr-defined]
        summary = runtime.last_recovery  # type: ignore[attr-defined]
        assert summary is not None and summary.failed == (job_id,)
        time.sleep(0.5)
        record = get_job(database, job_id)
        assert record.state is JobState.FAILED and record.state_reason == "worker_lost"
        assert record.error_code == "WORKER_LOST"
        with database.read() as session:
            assert (
                session.execute(select(JobAttempt).where(JobAttempt.job_id == job_id)).all() == []
            )
            assert (
                session.execute(select(Job.lease_owner).where(Job.id == job_id)).scalar_one()
                is None
            )
    finally:
        runtime.stop()  # type: ignore[attr-defined]
        database.close()
