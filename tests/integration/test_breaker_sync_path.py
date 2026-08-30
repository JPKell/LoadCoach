"""F3 (M5C-3): the synchronous path obeys the circuit breaker, and probes like the worker.

Before the fix, ``execute()`` called ``route()`` without ``open_circuit_breakers``,
``circuit_breaker_details`` or ``resident_models``: ``POST /generate``, ``POST /route`` and both
CLI commands selected models the queue had opened the breaker on, ``/route``'s explanation never
showed ``recently_failing``, and a synchronous request on a half-open model was a second,
unmarked probe — F2 through the other door. Spec §13 does not exempt the synchronous path.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from tests.integration.test_generate import NOW, _context, _model, _setup

from loadcoach.bootstrap import bootstrap
from loadcoach.domain.circuit_breaker import AttemptSample, BreakerState, CircuitBreakers
from loadcoach.services.execution import GenerateRequest, execute
from loadcoach.services.routing import NoEligibleModel

CANONICAL = f"fake/{_model().name}@sha256:{(_model().digest or '')[:12]}"


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("LOADCOACH_PROVIDER__KIND", "fake")
    application = bootstrap()
    with TestClient(application.app, base_url="http://localhost") as test_client:
        yield test_client


def _open_breaker(client: TestClient) -> str:
    """Open the runtime's breaker on the only discovered model; return its canonical ID."""
    canonical = str(client.get("/api/v1/models").json()["models"][0]["canonical_id"])
    runtime = client.app.state.queue_runtime  # type: ignore[attr-defined]  # FastAPI
    now = datetime.now(UTC)
    failures = [
        AttemptSample(at=now - timedelta(seconds=30 + i), succeeded=False) for i in range(5)
    ]
    verdicts = runtime.breakers.update({canonical: failures}, now=now)
    assert verdicts[canonical].state is BreakerState.OPEN
    return canonical


def test_post_route_rejects_a_breaker_open_model_as_recently_failing(
    client: TestClient,
) -> None:
    """The explanation ``POST /route`` returns is the decision a job would actually get."""
    canonical = _open_breaker(client)
    response = client.post("/api/v1/route", json={"task": "general.chat"})
    assert response.status_code == 422, response.text
    error = response.json()["error"]
    assert error["code"] == "NO_ELIGIBLE_MODEL"
    reasons = {item["canonical_id"]: item for item in error["details"]["candidates"]}
    assert reasons[canonical]["reason"] == "recently_failing"
    assert reasons[canonical]["detail"]["state"] == "open"


def test_post_generate_rejects_a_breaker_open_model_with_the_reason(
    client: TestClient,
) -> None:
    canonical = _open_breaker(client)
    response = client.post("/api/v1/generate", json={"task": "general.chat", "prompt": "hello"})
    assert response.status_code == 422, response.text
    error = response.json()["error"]
    assert error["code"] == "NO_ELIGIBLE_MODEL"
    reasons = {item["canonical_id"]: item["reason"] for item in error["details"]["candidates"]}
    assert reasons[canonical] == "recently_failing"


def _half_open(now: datetime) -> CircuitBreakers:
    """A registry whose breaker on ``CANONICAL`` is half-open with no probe out."""
    breakers = CircuitBreakers()
    failures = [
        AttemptSample(at=now - timedelta(seconds=440 - 20 * i), succeeded=False) for i in range(5)
    ]
    breakers.update({CANONICAL: failures}, now=now)
    breakers.update({CANONICAL: failures}, now=now)
    verdict = next(v for v in breakers.verdicts() if v.canonical_id == CANONICAL)
    assert verdict.state is BreakerState.HALF_OPEN and not verdict.probe_in_flight
    return breakers


def test_execute_marks_the_half_open_probe_while_the_attempt_is_in_flight(
    tmp_path: Path,
) -> None:
    """The synchronous path is the probe when it runs on a half-open model — marked, and seen."""
    database, provider = _setup(tmp_path)
    breakers = _half_open(NOW)
    context = replace(_context(provider), breakers=breakers)
    observed: list[bool] = []

    def on_chunk(chunk: object) -> None:
        if getattr(chunk, "kind", None) == "token":
            verdict = next(v for v in breakers.verdicts() if v.canonical_id == CANONICAL)
            observed.append(verdict.probe_in_flight)

    try:
        outcome = execute(
            database,
            GenerateRequest(task="general.chat", prompt="probe me"),
            context,
            on_chunk=on_chunk,
        )
    finally:
        database.close()
    assert outcome.status == "completed"
    assert observed and all(observed), "the probe was not marked while the attempt ran"


def test_execute_refuses_a_half_open_model_whose_probe_is_already_out(tmp_path: Path) -> None:
    """A synchronous request must not become a second, unmarked probe (queue §7)."""
    database, provider = _setup(tmp_path)
    breakers = _half_open(NOW)
    assert breakers.allow_probe(CANONICAL, now=NOW) is True  # another job's probe goes out
    context = replace(_context(provider), breakers=breakers)
    try:
        with pytest.raises(NoEligibleModel) as excinfo:
            execute(
                database,
                GenerateRequest(task="general.chat", prompt="probe me"),
                context,
            )
    finally:
        database.close()
    candidates = excinfo.value.details["candidates"]
    reasons = {item["canonical_id"]: item for item in candidates}
    assert reasons[CANONICAL]["reason"] == "recently_failing"
    assert reasons[CANONICAL]["detail"]["probe_in_flight"] is True
    assert len(provider.requests) == 0, "the provider was called despite the probe being out"
