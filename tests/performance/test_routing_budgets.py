"""Spec §15's routing and streaming budgets, measured (dev-plan P9 unit 11). Marked ``performance``.

* Routing decision, 20 candidates, warm evidence cache: ≤ 20 ms target, 100 ms ceiling.
* Routing decision, cold cache: ≤ 150 ms target, 500 ms ceiling.
* Added latency per streamed chunk: ≤ 5 ms target, 20 ms ceiling.

Targets are asserted on the median; ceilings on the worst run. A budget met only because the
provider returns instantly is not a budget, so the streaming figure subtracts provider time.
"""

from __future__ import annotations

import json
import statistics
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from modelrack.testing import FakeGeneration, FakeModel, FakeProvider, FakeScript
from sweatmeter import GpuSample, TelemetrySnapshot
from tests.integration.test_evidence_routing_change import (
    MACHINE,
    _bundle,
    _facts,
    _identity,
    _record,
    _template,
)

from loadcoach.services.database import Database, ensure_ready
from loadcoach.services.evidence import import_bundle
from loadcoach.services.models import discover_models
from loadcoach.services.routing import RouteRequest, RoutingPolicy, route
from loadcoach.services.task_profiles import import_task_profiles, read_task_profiles_file

pytestmark = pytest.mark.performance

GIB = 1024**3
NOW = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)
CANDIDATES = 20
WARM_TARGET_MS, WARM_CEILING_MS = 20.0, 100.0
COLD_TARGET_MS, COLD_CEILING_MS = 150.0, 500.0
CHUNK_TARGET_MS, CHUNK_CEILING_MS = 5.0, 20.0
_MEASURED = 30


def _models() -> tuple[FakeModel, ...]:
    return tuple(
        FakeModel(
            name=f"model-{index:02d}:8b",
            digest=f"{index:064x}",
            family=f"family{index % 4}",
            parameter_count=8_000_000_000 + index * 100_000_000,
            quantization="Q8_0",
            size_bytes=8 * GIB,
            max_context=32768,
            layers=32,
            kv_heads=8,
            head_dim=128,
            declared_capabilities=frozenset(),
        )
        for index in range(CANDIDATES)
    )


def _snapshot() -> TelemetrySnapshot:
    return TelemetrySnapshot(
        timestamp=NOW,
        ram_available_bytes=256 * GIB,
        gpus=(GpuSample(index=0, vram_total_bytes=80 * GIB, vram_used_bytes=1 * GIB),),
    )


def _populated(tmp_path: Path, golden_bundle: dict[str, Any], wrap_bundle: Any) -> str:
    """Twenty models with bound evidence on three capabilities each: a realistic warm store."""
    url = f"sqlite:///{tmp_path / 'routing-budget.sqlite3'}"
    database = Database.from_url(url)
    ensure_ready(database, auto_migrate=True)
    import_task_profiles(database, read_task_profiles_file(), now=NOW)
    discover_models(database, FakeProvider(FakeScript(models=_models())), now=NOW)
    profile_hash = route(
        database,
        RouteRequest(task="general.chat", estimated_input_tokens=500),
        provider=_facts(),
        policy=RoutingPolicy(machine_fingerprint=MACHINE),
        snapshot=_snapshot(),
        now=NOW,
    ).explanation.payload["selected"]["runtime_profile_hash"]
    template = _template(golden_bundle)
    records = []
    for model in _models():
        identity = _identity(database, model.name)
        for capability, score in (
            ("reasoning", 0.7),
            ("instruction_following", 0.8),
            ("creative_writing", 0.6),
        ):
            records.append(
                _record(
                    template,
                    identity=identity,
                    capability=capability,
                    score=score,
                    confidence=0.7,
                    profile_hash=profile_hash,
                )
            )
    outcome = import_bundle(database, wrap_bundle(_bundle(*records)), now=NOW)
    assert outcome.bound == len(records)
    database.close()
    return url


def _decide(database: Database) -> float:
    started = time.perf_counter()
    result = route(
        database,
        RouteRequest(task="general.chat", estimated_input_tokens=500),
        provider=_facts(),
        policy=RoutingPolicy(machine_fingerprint=MACHINE),
        snapshot=_snapshot(),
        now=NOW,
    )
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    assert len(result.explanation.ranking.ordered) == CANDIDATES
    return elapsed_ms


def test_a_warm_routing_decision_over_twenty_candidates_stays_within_its_budget(
    tmp_path: Path, golden_bundle: dict[str, Any], wrap_bundle: Any
) -> None:
    url = _populated(tmp_path, golden_bundle, wrap_bundle)
    database = Database.from_url(url)
    try:
        _decide(database)  # warm the pool and the compiled-statement cache
        samples = [_decide(database) for _ in range(_MEASURED)]
    finally:
        database.close()
    median, worst = statistics.median(samples), max(samples)
    print(  # noqa: T201 — the report
        f"\nwarm routing over {CANDIDATES} candidates: median {median:.1f} ms, max {worst:.1f} ms"
    )
    assert median <= WARM_TARGET_MS, f"median {median:.1f} ms exceeds {WARM_TARGET_MS} ms"
    assert worst <= WARM_CEILING_MS, (
        f"worst {worst:.1f} ms exceeds the {WARM_CEILING_MS} ms ceiling"
    )


def test_a_cold_routing_decision_stays_within_its_budget(
    tmp_path: Path, golden_bundle: dict[str, Any], wrap_bundle: Any
) -> None:
    """Cold: a fresh handle, no pooled connection, no compiled statement — the first decision."""
    url = _populated(tmp_path, golden_bundle, wrap_bundle)
    samples = []
    for _ in range(5):
        database = Database.from_url(url)
        try:
            samples.append(_decide(database))
        finally:
            database.close()
    median, worst = statistics.median(samples), max(samples)
    print(  # noqa: T201 — the report
        f"\ncold routing over {CANDIDATES} candidates: median {median:.1f} ms, max {worst:.1f} ms"
    )
    assert median <= COLD_TARGET_MS, f"median {median:.1f} ms exceeds {COLD_TARGET_MS} ms"
    assert worst <= COLD_CEILING_MS, (
        f"worst {worst:.1f} ms exceeds the {COLD_CEILING_MS} ms ceiling"
    )


def test_added_latency_per_streamed_chunk_stays_within_its_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``POST /generate/stream`` with a few hundred token-level chunks from an instant provider:
    the wall time per chunk, minus the provider's own (nil) time, is LoadCoach's added latency."""
    from fastapi.testclient import TestClient
    from tests.integration.test_generate import _model

    from loadcoach.config import load_settings
    from loadcoach.web.app import create_app

    chunks = 200
    script = FakeScript(
        models=(_model(),),
        generations=(FakeGeneration(word_count=chunks),),
        repeat_final_generation=True,
    )
    url = f"sqlite:///{tmp_path / 'stream-budget.sqlite3'}"
    monkeypatch.setenv("LOADCOACH_STORAGE__DATABASE_URL", url)
    monkeypatch.setenv("LOADCOACH_PROVIDER__KIND", "fake")
    settings = load_settings().settings
    database = Database.from_url(url)
    ensure_ready(database, auto_migrate=True)
    import_task_profiles(database, read_task_profiles_file(), now=NOW)
    discover_models(database, FakeProvider(script), now=NOW)
    database.close()
    app = create_app(settings)
    per_chunk: list[float] = []
    with TestClient(app, base_url="http://localhost") as client:
        provider = FakeProvider(script)
        app.state.provider = provider
        app.state.queue_runtime.replace_provider(provider)
        for _ in range(5):
            started = time.perf_counter()
            tokens = 0
            provider_ms = 0
            with client.stream(
                "POST", "/api/v1/generate/stream", json={"task": "general.chat", "prompt": "go"}
            ) as response:
                assert response.status_code == 200
                for line in response.iter_lines():
                    if line.startswith("event: token"):
                        tokens += 1
                    if '"provider_ms"' in line:
                        payload = json.loads(line[len("data: ") :])
                        provider_ms = int(payload["payload"]["timing"]["provider_ms"])
            wall_ms = (time.perf_counter() - started) * 1000.0
            assert tokens >= 100, tokens
            per_chunk.append(max(wall_ms - provider_ms, 0.0) / tokens)
    median, worst = statistics.median(per_chunk), max(per_chunk)
    print(  # noqa: T201 — the report
        f"\nadded latency per streamed chunk: median {median:.2f} ms, max {worst:.2f} ms"
    )
    assert median <= CHUNK_TARGET_MS, f"median {median:.2f} ms/chunk exceeds {CHUNK_TARGET_MS} ms"
    assert worst <= CHUNK_CEILING_MS, f"worst {worst:.2f} ms/chunk exceeds the ceiling"
