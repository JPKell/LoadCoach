"""The residency service against a fake provider: the paths the simulator does not reach."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from baseaicore import RuntimeProfile
from modelrack import ProviderCapabilities
from modelrack.testing import FakeProvider, FakeScript
from sqlalchemy import select
from tests.integration.test_generate import _model

from loadcoach.config import ResidencySettings
from loadcoach.infrastructure.db.models import Model, Residency
from loadcoach.services.database import Database, ensure_ready
from loadcoach.services.models import discover_models
from loadcoach.services.residency import REASON_UNMANAGED, ResidencyService

NOW = datetime(2026, 8, 29, 12, 0, 0, tzinfo=UTC)
GIB = 1024**3
MANAGED = ProviderCapabilities(
    streaming=True, token_counts=True, force_unload=True, residency_query=True
)
UNMANAGED = ProviderCapabilities(streaming=True, token_counts=True)


@pytest.fixture
def database(tmp_path: Path) -> Iterator[Database]:
    handle = Database.from_url(f"sqlite:///{tmp_path / 'residency.sqlite3'}")
    ensure_ready(handle, auto_migrate=True)
    try:
        yield handle
    finally:
        handle.close()


def _service(
    database: Database, capabilities: ProviderCapabilities, **settings: int
) -> tuple[ResidencyService, FakeProvider, dict[str, str]]:
    provider = FakeProvider(
        FakeScript(
            models=(
                _model("alpha:8b", vram_bytes=4 * GIB),
                _model("beta:8b", "b" * 64, vram_bytes=4 * GIB),
            ),
            capabilities=capabilities,
        )
    )
    discover_models(database, provider, now=NOW)
    with database.read() as session:
        ids = {
            model.provider_model_name: model.id
            for model in session.execute(select(Model)).scalars().all()
        }
    service = ResidencyService(
        database, provider, settings=ResidencySettings(**settings), clock=lambda: NOW
    )
    return service, provider, ids


def _load(
    service: ResidencyService,
    provider: FakeProvider,
    ids: dict[str, str],
    name: str,
    *,
    in_use: frozenset[str] = frozenset(),
    now: datetime = NOW,
    free_bytes: int | None = 16 * GIB,
) -> object:
    return service.ensure_loaded(
        model_id=ids[name],
        canonical_id=f"fake/{name}",
        identity=provider.resolve(name),
        profile=RuntimeProfile(),
        gpu_index=0,
        in_use_model_ids=in_use,
        required_bytes=5 * GIB,
        free_bytes=free_bytes,
        headroom_bytes=GIB // 2,
        now=now,
    )


def test_an_unmanaged_provider_degrades_to_load_on_demand_with_the_reason(
    database: Database,
) -> None:
    service, provider, ids = _service(database, UNMANAGED)
    assert service.manageable is False
    outcome = _load(service, provider, ids, "alpha:8b")
    assert outcome.reason == REASON_UNMANAGED  # type: ignore[attr-defined]  # LoadOutcome
    assert outcome.loaded is False  # type: ignore[attr-defined]
    assert service.resident() == ()
    assert service.evict_idle(NOW + timedelta(hours=1), in_use_model_ids=frozenset()) == ()
    assert service.evictable_bytes_by_device(frozenset()) == {}


def test_a_load_records_an_episode_with_the_reported_vram_and_a_second_call_is_a_touch(
    database: Database,
) -> None:
    service, provider, ids = _service(database, MANAGED, max_resident_models=2)
    first = _load(service, provider, ids, "alpha:8b")
    assert first.loaded is True and first.already_resident is False  # type: ignore[attr-defined]
    later = NOW + timedelta(minutes=5)
    second = _load(service, provider, ids, "alpha:8b", now=later)
    assert second.already_resident is True and second.loaded is False  # type: ignore[attr-defined]
    entries = service.resident()
    assert len(entries) == 1
    assert entries[0].vram_bytes == 4 * GIB
    assert entries[0].last_used_at == later
    assert service.resident_devices() == {entries[0].canonical_id: frozenset({0})}
    assert service.evictable_bytes_by_device(frozenset()) == {0: 4 * GIB}
    assert service.evictable_bytes_by_device(frozenset({ids["alpha:8b"]})) == {}


def test_a_model_in_use_is_never_evicted_even_when_the_limit_says_so(database: Database) -> None:
    service, provider, ids = _service(database, MANAGED, max_resident_models=1)
    _load(service, provider, ids, "alpha:8b")
    outcome = _load(service, provider, ids, "beta:8b", in_use=frozenset({ids["alpha:8b"]}))
    assert outcome.loaded is True and outcome.evicted == ()  # type: ignore[attr-defined]
    assert {entry.provider_model_name for entry in service.resident()} == {"alpha:8b", "beta:8b"}
    # Once alpha is idle, the room check evicts it for the next load that needs the space.
    outcome = _load(service, provider, ids, "alpha:8b", free_bytes=2 * GIB)
    assert outcome.already_resident is True  # type: ignore[attr-defined]


def test_sync_closes_an_episode_the_provider_no_longer_reports(database: Database) -> None:
    service, provider, ids = _service(database, MANAGED, max_resident_models=2)
    _load(service, provider, ids, "alpha:8b")
    provider.unload(provider.resolve("alpha:8b"))  # evicted behind LoadCoach's back
    service.sync(NOW + timedelta(minutes=1))
    assert service.resident() == ()
    with database.read() as session:
        row = session.execute(select(Residency)).scalar_one()
    assert row.resident is False and row.unload_reason == "provider_reported_unloaded"
    assert row.unloaded_at == NOW + timedelta(minutes=1)
