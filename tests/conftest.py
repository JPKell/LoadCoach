"""Shared pytest fixtures: isolated XDG roots and a deterministic clock.

No test may read or write the developer's real config, data or state directories (testing
standards §9), so every test runs against a throwaway tree by default.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator, Mapping
from datetime import UTC, datetime
from pathlib import Path

import pytest
from setspec import (
    GeneratorInfo,
    SchemaVersion,
    dump_envelope,
    golden_payloads,
)


@pytest.fixture(autouse=True)
def _isolated_xdg_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Point every XDG directory at a throwaway tree and clear stray LOADCOACH_* variables."""
    config_home = tmp_path / "config"
    data_home = tmp_path / "data"
    state_home = tmp_path / "state"
    for path in (config_home, data_home, state_home):
        path.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    monkeypatch.setenv("XDG_DATA_HOME", str(data_home))
    monkeypatch.setenv("XDG_STATE_HOME", str(state_home))
    monkeypatch.chdir(tmp_path)
    # Every LOADCOACH_* variable is the application's own configuration and must not leak in from
    # the developer's shell. Harness configuration (a PostgreSQL URL for the integration suite)
    # deliberately uses the LCTEST_ prefix instead, precisely so it survives this.
    for key in list(os.environ):
        if key.startswith("LOADCOACH_"):
            monkeypatch.delenv(key, raising=False)
    yield


@pytest.fixture
def frozen_instant() -> datetime:
    """A fixed, timezone-aware UTC instant for deterministic timestamp assertions."""
    return datetime(2026, 8, 26, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def wrap_bundle() -> Callable[..., str]:
    """Return a helper that wraps an evidence payload in a SetSpec envelope.

    The payload is whatever the caller supplies — a golden, a mutated golden, or a hand-built
    minimal bundle — and the envelope is built by ``setspec.dump_envelope`` rather than by string
    formatting, so a test can never accidentally assert against an envelope shape SetSpec would
    not produce.
    """

    def wrap(
        payload: Mapping[str, object],
        *,
        major: int = 1,
        minor: int = 0,
        generator: str = "freeweight",
        generator_version: str = "1.0.0",
        generated_at: datetime | None = None,
    ) -> str:
        return dump_envelope(
            payload,
            schema="benchmark.evidence_bundle",
            version=SchemaVersion(major, minor),
            generator=GeneratorInfo(name=generator, version=generator_version),
            generated_at=generated_at or datetime(2026, 8, 21, 12, 0, tzinfo=UTC),
        )

    return wrap


@pytest.fixture
def golden_bundle() -> dict[str, object]:
    """The ``full`` ``benchmark.evidence_bundle`` golden, straight from the installed SetSpec.

    Three records: a digest-identified ``coding.python``, a ``user.noir_tech_voice`` with
    calibration, and a ``name_only`` ``reasoning`` measured under a different runtime profile.
    Between them they exercise every branch P6 has to get right, which is why the goldens are
    imported rather than hand-authored (ADR-0009 rule 7).
    """
    for payload in golden_payloads("benchmark.evidence_bundle", SchemaVersion(1, 0)):
        if payload.get("evidence"):
            return payload
    message = "the installed setspec ships no evidence_bundle golden carrying records"
    raise AssertionError(message)


@pytest.fixture(autouse=True)
def _deterministic_telemetry(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make every test see the same machine, whatever this machine is doing.

    Coding standards §5 requires telemetry readers to be injected precisely so that a decision is
    reproducible, and most of the suite honours that by passing a ``TelemetrySnapshot`` straight
    into :func:`~loadcoach.services.routing.route`. The paths that go through the *application* —
    ``loadcoach route explain``, ``POST /route``, the queue's e2e controls — build a real
    :class:`~sweatmeter.TelemetryCollector` instead, and therefore read whatever the developer's
    or the runner's GPU happens to be doing at that moment.

    That is a genuine flake, not a hypothetical one: with roughly 3.7 GB free on a card another
    process had filled, four tests that had passed all session began failing with
    ``insufficient_vram`` — the correct answer to the question they accidentally asked, and the
    wrong question. This fixture pins the answer: 64 GiB of RAM and one 48 GiB device with 1 GiB
    in use. A test that needs different resources passes its own snapshot, which is unaffected.
    """
    from sweatmeter import GpuSample, TelemetryCollector, TelemetrySnapshot

    def snapshot(_self: TelemetryCollector) -> TelemetrySnapshot:
        return TelemetrySnapshot(
            timestamp=datetime(2026, 8, 26, 12, 0, 0, tzinfo=UTC),
            ram_available_bytes=64 * 1024**3,
            gpus=(
                GpuSample(
                    index=0,
                    vram_total_bytes=48 * 1024**3,
                    vram_used_bytes=1 * 1024**3,
                ),
            ),
        )

    monkeypatch.setattr(TelemetryCollector, "snapshot", snapshot)
