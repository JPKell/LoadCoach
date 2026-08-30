"""Token-gap budget over a real TCP socket (spec §15; F12/M5C-12).

The in-process figure ("added latency per streamed chunk") cannot see the SSE loop's poll
quantisation, because a test client drives the body iterator directly. This test serves the real
application with uvicorn on a loopback socket, streams a generation whose fake provider produces
one token every ``_CADENCE_MS`` of *real* time, and measures the client-side inter-arrival gap of
token frames. The added latency per token is the arrival gap minus the production cadence; at the
old 10 ms poll its p95 was ~10 ms against the 5 ms budget (Fable's F12 measurement), at 2 ms it
is within budget. The hard assertion is the 20 ms ceiling — a shared machine may not honour the
5 ms target every run; the figures themselves are printed for the report.
"""

from __future__ import annotations

import statistics
import threading
import time
from typing import TYPE_CHECKING

import httpx
import pytest
import uvicorn
from modelrack.testing import DEFAULT_MODEL, FakeGeneration, FakeProvider, FakeScript

from loadcoach.bootstrap import bootstrap

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.performance

_CADENCE_MS = 5.0
_TOKENS = 200
_CEILING_MS = 20.0
_TARGET_MS = 5.0


def _paced_provider() -> FakeProvider:
    """One catalogue model; every generation produces ``_TOKENS`` tokens at the real cadence."""
    return FakeProvider(
        FakeScript(
            models=(DEFAULT_MODEL,),
            generations=(FakeGeneration(word_count=_TOKENS, chunk_delay_ms=_CADENCE_MS),),
            repeat_final_generation=True,
        ),
        sleep=time.sleep,
    )


def test_token_gap_over_a_real_socket_stays_under_the_ceiling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LOADCOACH_PROVIDER__KIND", "fake")
    monkeypatch.setenv("LOADCOACH_STORAGE__DATABASE_URL", f"sqlite:///{tmp_path / 'gap.sqlite3'}")
    # Both the bootstrap discovery pass and the lifespan's serving provider must be the paced
    # fake, and they must agree on the catalogue — DEFAULT_MODEL keeps the identity stable.
    monkeypatch.setattr("loadcoach.bootstrap.build_provider", lambda _s: _paced_provider())
    monkeypatch.setattr("loadcoach.web.app.build_provider", lambda _s: _paced_provider())
    application = bootstrap()

    server = uvicorn.Server(
        uvicorn.Config(application.app, host="127.0.0.1", port=0, log_level="warning")
    )
    thread = threading.Thread(target=server.run, name="loadcoach-gap-server", daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while not server.started:
        assert time.monotonic() < deadline, "uvicorn did not start"
        time.sleep(0.02)
    port = server.servers[0].sockets[0].getsockname()[1]

    arrivals: list[float] = []
    try:
        with (
            httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=30.0) as client,
            client.stream(
                "POST",
                "/api/v1/generate/stream",
                json={"task": "general.chat", "prompt": "measure me"},
            ) as response,
        ):
            assert response.status_code == 200, response.read()
            for line in response.iter_lines():
                if line.startswith("event:") and line.split(":", 1)[1].strip() == "token":
                    arrivals.append(time.perf_counter())
                if line.startswith("event:") and "result" in line:
                    break
    finally:
        server.should_exit = True
        thread.join(timeout=10)

    assert len(arrivals) >= _TOKENS * 0.9, f"only {len(arrivals)} token frames arrived"
    gaps_ms = [
        (later - earlier) * 1000.0 for earlier, later in zip(arrivals, arrivals[1:], strict=False)
    ]
    added_ms = sorted(max(gap - _CADENCE_MS, 0.0) for gap in gaps_ms)
    p95_added = added_ms[int(len(added_ms) * 0.95) - 1]
    print(  # noqa: T201 — the report
        f"\nstreaming over TCP: {len(arrivals)} tokens at {_CADENCE_MS} ms cadence — "
        f"raw gap median {statistics.median(gaps_ms):.2f} ms, "
        f"p95 {sorted(gaps_ms)[int(len(gaps_ms) * 0.95) - 1]:.2f} ms, "
        f"max {max(gaps_ms):.2f} ms; added latency p95 {p95_added:.2f} ms "
        f"(target {_TARGET_MS} ms, ceiling {_CEILING_MS} ms)"
    )
    assert p95_added <= _CEILING_MS, f"added token latency p95 {p95_added:.2f} ms over the ceiling"
