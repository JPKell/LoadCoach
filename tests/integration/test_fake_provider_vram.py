"""E6 — the fake provider must not be gated on real VRAM (`E4_HANDOFF.md` §5, `E6_HANDOFF.md`).

At E4, an unscripted ``FakeProvider()`` (ModelRack's ``DEFAULT_MODEL``, an 8.5 GB declared model)
tripped routing's ``insufficient_vram`` hard constraint whenever the host GPU was busy — even
``tools.agent``, unchanged. A provider that exists so the suite can be exercised without hardware
was gated on hardware. Three gates, deterministic and GPU-free (``estimate_vram``/``device_fits``
are pure functions over an injected telemetry snapshot):

* Gate A reproduces the original failure directly against ModelRack's own ``DEFAULT_MODEL``, term
  by term against routing.md §4's arithmetic — this is the baseline the fix is measured against,
  and it stays true forever regardless of what LoadCoach's own default becomes.
* Gate B proves ``build_provider``'s new small default is admitted on the exact busy-GPU snapshot
  that failed at E4, and that a routing journey completes.
* Gate C proves an operator can reproduce Gate A's rejection **through configuration** —
  ``[provider.fake]`` set to the original ``DEFAULT_MODEL`` geometry — on a machine with plenty of
  free VRAM, with the full ``estimate`` block and ``kv_source == "theoretical"``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from baseaicore import RuntimeProfile
from modelrack.testing import DEFAULT_MODEL, FakeProvider, FakeScript
from sweatmeter import GpuSample, TelemetrySnapshot

from loadcoach.config import FakeProviderSettings, ProviderSettings
from loadcoach.domain.routing.subject import ProviderFacts
from loadcoach.infrastructure.providers.factory import build_provider
from loadcoach.services.database import Database, ensure_ready
from loadcoach.services.models import discover_models
from loadcoach.services.routing import NoEligibleModel, RouteRequest, RoutingPolicy, route
from loadcoach.services.task_profiles import import_task_profiles, read_task_profiles_file

if TYPE_CHECKING:
    from modelrack.provider import Provider

GIB = 1024**3
NOW = datetime(2026, 9, 4, 12, 0, 0, tzinfo=UTC)


def _database(tmp_path: Path, provider: Provider) -> Database:
    database = Database.from_url(f"sqlite:///{tmp_path / 'fake_vram.sqlite3'}")
    ensure_ready(database, auto_migrate=True)
    import_task_profiles(database, read_task_profiles_file(), now=NOW)
    discover_models(database, provider, now=NOW)
    return database


def _snapshot(
    *, free_bytes: int, total_bytes: int = 24 * GIB, ram: int = 32 * GIB
) -> TelemetrySnapshot:
    used_bytes = total_bytes - free_bytes
    return TelemetrySnapshot(
        timestamp=NOW,
        ram_available_bytes=ram,
        gpus=(GpuSample(index=0, vram_total_bytes=total_bytes, vram_used_bytes=used_bytes),),
    )


def _facts(**overrides: Any) -> ProviderFacts:
    defaults: dict[str, Any] = {
        "healthy": True,
        "context_configurable": True,
        "supports_tool_use": True,
        "supports_structured_output": True,
        "supports_streaming": True,
    }
    defaults.update(overrides)
    return ProviderFacts(**defaults)


def test_gate_a_the_original_default_model_trips_insufficient_vram_on_a_busy_gpu(
    tmp_path: Path,
) -> None:
    """Reproduce E4's failure, deterministically, against ModelRack's unmodified DEFAULT_MODEL.

    Pins served_context to 16 384 to match E4_HANDOFF.md §5's observed 11.4 GB and E6's own §0.2
    derivation: weights 8_967_000_000 + kv 2_147_483_648 + activation 268_435_456 = 11_382_919_104
    (~10.6 GiB / 11.4 GB). ~1.2 GB free matches E4's own reported figure.
    """
    provider = FakeProvider(FakeScript(models=(DEFAULT_MODEL,)))
    database = _database(tmp_path, provider)
    try:
        with pytest.raises(NoEligibleModel) as caught:
            route(
                database,
                RouteRequest(task="general.chat"),
                provider=_facts(),
                policy=RoutingPolicy(runtime_defaults=RuntimeProfile(context_size=16384)),
                snapshot=_snapshot(free_bytes=int(1.2 * GIB)),
                now=NOW,
            )
        candidate = caught.value.details["candidates"][0]
        assert candidate["reason"] == "insufficient_vram"
        estimate = candidate["detail"]["estimate"]
        assert estimate["served_context"] == 16384
        assert estimate["weights_bytes"] == 8_967_000_000
        assert estimate["kv_bytes"] == 2_147_483_648
        assert estimate["activation_bytes"] == 268_435_456
        assert estimate["kv_source"] == "theoretical"
        assert candidate["detail"]["estimated_bytes"] == 11_382_919_104
    finally:
        database.close()


def test_gate_b_the_shipped_default_is_admitted_on_e4s_own_busy_gpu_snapshot(
    tmp_path: Path,
) -> None:
    """Requirement 1: build_provider's actual shipped fake, on the exact scenario that failed."""
    provider = build_provider(ProviderSettings(kind="fake"))
    database = _database(tmp_path, provider)
    try:
        result = route(
            database,
            RouteRequest(task="general.chat"),
            provider=_facts(),
            policy=RoutingPolicy(),
            snapshot=_snapshot(free_bytes=int(1.2 * GIB)),
            now=NOW,
        )
        payload = result.explanation.payload
        selected = payload["selected"]
        assert selected is not None
        assert selected["target_gpu_index"] == 0
        (candidate,) = payload["candidates"]
        assert candidate["estimated_vram_bytes"] < int(1.2 * GIB)
    finally:
        database.close()


def test_gate_c_provider_fake_config_reproduces_gate_as_rejection_with_plenty_free(
    tmp_path: Path,
) -> None:
    """Requirement 2: an operator reaches insufficient_vram through configuration alone, on a
    machine with plenty of VRAM free by an ordinary fake provider's standard (10 GiB — thirty
    times what the shipped default needs) — the diagnostic stays reachable on purpose, and the
    original 8.5 GB DEFAULT_MODEL geometry is still enough to provoke it at that served context."""
    settings = ProviderSettings(
        kind="fake",
        fake=FakeProviderSettings(size_bytes=8_540_000_000, layers=32, kv_heads=8, head_dim=128),
    )
    provider = build_provider(settings)
    database = _database(tmp_path, provider)
    try:
        with pytest.raises(NoEligibleModel) as caught:
            route(
                database,
                RouteRequest(task="general.chat"),
                provider=_facts(),
                policy=RoutingPolicy(runtime_defaults=RuntimeProfile(context_size=16384)),
                snapshot=_snapshot(free_bytes=10 * GIB),
                now=NOW,
            )
        candidate = caught.value.details["candidates"][0]
        assert candidate["reason"] == "insufficient_vram"
        estimate = candidate["detail"]["estimate"]
        assert estimate["served_context"] == 16384
        assert estimate["weights_bytes"] == 8_967_000_000
        assert estimate["kv_bytes"] == 2_147_483_648
        assert estimate["activation_bytes"] == 268_435_456
        assert estimate["kv_source"] == "theoretical"
        assert candidate["detail"]["estimated_bytes"] == 11_382_919_104
        assert candidate["detail"]["free_bytes_by_gpu"] == {"0": 10 * GIB}
        assert candidate["detail"]["headroom_bytes"] > 0
    finally:
        database.close()


def test_the_fake_provider_kind_is_not_exempted_from_insufficient_vram(tmp_path: Path) -> None:
    """The row's stop rule, proven: `kind == "fake"` gets no bypass — Gate C above is the same
    constraint, evaluated the same way, that a real provider would be evaluated against."""
    settings = ProviderSettings(
        kind="fake",
        fake=FakeProviderSettings(size_bytes=8_540_000_000, layers=32, kv_heads=8, head_dim=128),
    )
    provider = build_provider(settings)
    database = _database(tmp_path, provider)
    try:
        with pytest.raises(NoEligibleModel) as caught:
            route(
                database,
                RouteRequest(task="general.chat"),
                provider=_facts(),
                policy=RoutingPolicy(runtime_defaults=RuntimeProfile(context_size=16384)),
                snapshot=_snapshot(free_bytes=int(1.2 * GIB)),
                now=NOW,
            )
        assert caught.value.details["candidates"][0]["reason"] == "insufficient_vram"
    finally:
        database.close()
