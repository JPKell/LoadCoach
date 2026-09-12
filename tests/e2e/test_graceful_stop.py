"""A ``SIGTERM`` stop finishes while a stream is open, and in-flight work is recovered (row WPF4).

LoadCoach serves open-ended SSE streams — the Queue page, the telemetry bar, a job's log pane, and
the console's proxies of all three — which end only when their client goes away. Uvicorn's default
graceful shutdown waits for every open connection *for ever*, so at row WP6 a ``systemctl --user
restart loadcoach.service`` with the console's Queue page open sat in ``stop-sigterm`` until
systemd's ``TimeoutStopSec`` (90 s) and was ``SIGKILL``ed — which is not a shutdown: the lifespan's
teardown never ran (`WP6_HANDOFF.md` finding 6, first seen at `WP2_HANDOFF.md` §7 item 4).

This serves the real application on a real socket with a stream held open *and* a job in flight,
sends the real signal, and measures what systemd would measure: how long the process took and what
it exited with. The provider is a fake whose generation never finishes inside the test, so nothing
here needs a GPU, a model or a network.
"""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from loadcoach.config import QueueSettings
from loadcoach.domain.queue_state import JobState
from loadcoach.services.database import Database
from loadcoach.services.job_events import JobEventSink
from loadcoach.services.queue import get_job
from loadcoach.services.recovery import recover

_STOP_BUDGET_SECONDS = 30.0
"""What this test will accept. systemd's ``TimeoutStopSec`` for the unit the console writes is
90 s; the budget here is a third of it, which is what "well inside" has to mean for a stop that
waits ``system.SHUTDOWN_GRACE_SECONDS`` (5 s) for the stream and then up to 10 s for the queue
runtime's threads (``QueueRuntime.stop``)."""

_CHILD = r"""
import dataclasses, sys, time
from datetime import UTC, datetime

import modelrack.testing as fakes
import uvicorn

url, port = sys.argv[1], int(sys.argv[2])

# The factory builds `FakeProvider(FakeScript(models=(...,)))` with no generation scripted, which
# answers instantly. Wrapping the class it reaches for keeps the factory's own small model — sized
# so admission never trips `insufficient_vram` on a busy host — and only makes the generation slow,
# so the job is still `executing` when the signal arrives.
_real = fakes.FakeProvider


def _slow(script=None, **kwargs):
    slow = dataclasses.replace(
        script,
        generations=(fakes.FakeGeneration(text="never finishes", first_chunk_delay_ms=600_000),),
        repeat_final_generation=True,
    )
    return _real(slow, sleep=time.sleep, **kwargs)


fakes.FakeProvider = _slow

from loadcoach.bootstrap import bootstrap
from loadcoach.cli.commands.system import SHUTDOWN_GRACE_SECONDS
from loadcoach.services.database import Database
from loadcoach.services.job_events import JobEventSink
from loadcoach.services.queue import JobSubmission, enqueue

application = bootstrap()
settings = application.loaded_settings.settings
with Database.from_url(url) as database:
    submission = JobSubmission(task="general.chat", prompt="hold a lease", idempotent=True)
    reserved = enqueue(
        database,
        submission,
        now=datetime.now(UTC),
        queue_settings=settings.queue,
        execution_settings=settings.execution,
        sink=JobEventSink(),
    )
print(f"READY {reserved.job_id}", flush=True)
uvicorn.run(
    application.app,
    host="127.0.0.1",
    port=port,
    log_config=None,
    timeout_graceful_shutdown=SHUTDOWN_GRACE_SECONDS,
)
"""


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _spawn(url: str, port: int, home: Path) -> tuple[subprocess.Popen[str], str]:
    """Serve the application on ``port`` with one job queued; return the child and that job.

    ``home`` becomes every XDG root, so the child cannot reach the operator's own configuration,
    database or task-profile file — it gets the zero-configuration defaults and ``url``.
    """
    environment = {
        **os.environ,
        "LOADCOACH_PROVIDER__KIND": "fake",
        "LOADCOACH_STORAGE__DATABASE_URL": url,
        "LOADCOACH_SERVER__PORT": str(port),
        "XDG_CONFIG_HOME": str(home / "config"),
        "XDG_DATA_HOME": str(home / "data"),
        "XDG_STATE_HOME": str(home / "state"),
        "XDG_CACHE_HOME": str(home / "cache"),
    }
    environment.pop("LOADCOACH_CONFIG", None)
    child = subprocess.Popen(  # noqa: S603 — our own interpreter, our own script
        [sys.executable, "-c", _CHILD, url, str(port)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=environment,
        cwd=Path(__file__).resolve().parents[2],
    )
    assert child.stdout is not None
    line = child.stdout.readline().strip()
    if not line.startswith("READY "):
        child.kill()
        _, err = child.communicate(timeout=10)
        pytest.fail(f"the child never enqueued its job: {line!r}\n{err}")
    return child, line.split(" ", 1)[1]


def _await_state(client: httpx.Client, job_id: str, state: str, *, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    seen = "unknown"
    while time.monotonic() < deadline:
        response = client.get(f"/api/v1/jobs/{job_id}")
        if response.status_code == 200:
            seen = response.json()["state"]
            if seen == state:
                return
        time.sleep(0.1)
    pytest.fail(f"job {job_id} reached {seen!r}, not {state!r} (admission deferred it?)")


def test_sigterm_stops_the_server_while_a_stream_is_open_and_leaves_the_job_recoverable(
    tmp_path: Path,
) -> None:
    """The row's two properties at once: the stop is bounded, and the lease is not lost.

    A ``SIGKILL`` would prove neither — it is the case this row exists to stop happening.
    """
    url = f"sqlite:///{tmp_path / 'graceful.sqlite3'}"
    port = _free_port()
    child, job_id = _spawn(url, port, tmp_path)
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=10.0) as client:
            deadline = time.monotonic() + 30
            while True:
                try:
                    if client.get("/api/v1/health").status_code == 200:
                        break
                except httpx.HTTPError:  # noqa: PERF203 — retry until the socket answers
                    pass
                if time.monotonic() >= deadline:
                    child.kill()
                    _, err = child.communicate(timeout=10)
                    pytest.fail(f"the child never served /api/v1/health\n{err[-3000:]}")
                time.sleep(0.1)
            _await_state(client, job_id, "executing", timeout=30)

            # The connection WP6 had open: the live queue stream, held across the whole stop.
            with client.stream("GET", "/api/v1/queue/stream") as stream:
                frames = stream.iter_lines()
                assert next(frames).startswith("id: ")
                started = time.monotonic()
                os.kill(child.pid, signal.SIGTERM)
                returncode = child.wait(timeout=_STOP_BUDGET_SECONDS)
                elapsed = time.monotonic() - started
        _, log = child.communicate(timeout=10)
    except subprocess.TimeoutExpired:
        child.kill()
        _, log = child.communicate(timeout=10)
        pytest.fail(f"the server did not stop within {_STOP_BUDGET_SECONDS:.0f}s\n{log}")
    finally:
        if child.poll() is None:  # pragma: no cover — only on an assertion above
            child.kill()

    print(f"stop took {elapsed:.1f}s, exit {returncode}")  # noqa: T201 — the row's measurement
    assert elapsed < _STOP_BUDGET_SECONDS
    # Uvicorn restores the default disposition and re-raises the signal it caught, so a clean stop
    # shows as terminated-by-SIGTERM — which is the signal systemd sent, and which it counts as
    # success. What must never appear is ``-9``: that is systemd's stop timeout giving up.
    assert returncode in {0, -signal.SIGTERM}, f"SIGTERM ended the process with {returncode}"
    # The property a SIGKILL destroys: the lifespan's teardown ran.
    assert "Application shutdown complete" in log

    # The lease is handled the way queue §10 says, and not by the job being quietly failed as the
    # teardown closed the provider under the worker still holding it.
    with Database.from_url(url) as database:
        before = get_job(database, job_id)
        assert before.state is JobState.EXECUTING
        assert before.lease_owner is not None
        summary = recover(
            database,
            JobEventSink(),
            now=datetime.now(UTC),
            owner_prefix="wpf4-recovery",
            queue_settings=QueueSettings(),
        )
        assert summary.requeued == (job_id,)
        after = get_job(database, job_id)
        assert after.state is JobState.QUEUED
        assert after.lease_owner is None
        assert after.state_reason == "recovered"
