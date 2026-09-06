"""A pinned adapter reaches the provider, and every attempt records the subject that answered.

Gate E of LoadCoach 1.1 (ADR-0064 rule 4, ADR-0080, ADR-0065 rule 4). ModelRack's own fake
declares `adapter_hot_swap = False` on principle, so the double here is a thin wrapper that
declares the capability and records what it was asked to run under — the ModelRack half of the
same path is proved live against `llama-server`, not here.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from baseaicore import ModelDescriptor, ModelIdentity, RuntimeProfile
from modelrack import (
    GenerationRequest,
    GenerationResult,
    ProviderCapabilities,
    ProviderHealth,
    StreamEvent,
)
from modelrack.testing import FakeModel, FakeProvider, FakeScript
from sqlalchemy import select
from tests.integration.test_adapter_subjects import BASE_NAME, _settings, _write_adapter

from loadcoach.domain.routing.subject import RuntimeOverrides
from loadcoach.infrastructure.db.models import JobAttempt
from loadcoach.infrastructure.providers.factory import ProviderRegistration
from loadcoach.services.adapters import sync_adapters
from loadcoach.services.database import Database, ensure_ready
from loadcoach.services.execution import (
    ExecutionContext,
    GenerateRequest,
    execute,
    provider_facts_for,
)
from loadcoach.services.models import discover_models
from loadcoach.services.routing import RoutingPolicy
from loadcoach.services.task_profiles import (
    DEFAULT_SCHEMAS_DIR,
    import_task_profiles,
    read_task_profiles_file,
)

GIB = 1024**3
NOW = datetime(2026, 9, 5, 12, 0, 0, tzinfo=UTC)


class AdapterCapableProvider:
    """A ``FakeProvider`` that declares ``adapter_hot_swap`` and remembers each request's pin.

    The `adapter` field is stripped before the request reaches the real fake, which refuses one by
    contract; everything else — identity resolution, the transcript, the result — is the fake's
    own, so what is asserted here is LoadCoach's plumbing rather than a mock of it.
    """

    def __init__(self, fake: FakeProvider) -> None:
        self._fake = fake
        self.adapters_asked: list[str | None] = []

    @property
    def kind(self) -> Any:
        return self._fake.kind

    def health(self) -> ProviderHealth:
        return self._fake.health()

    def capabilities(self) -> ProviderCapabilities:
        return dataclasses.replace(self._fake.capabilities(), adapter_hot_swap=True)

    def list_models(self, *, refresh: bool = False) -> Sequence[ModelDescriptor]:
        return self._fake.list_models(refresh=refresh)

    def inspect_model(self, identity: ModelIdentity, *, refresh: bool = False) -> ModelDescriptor:
        return self._fake.inspect_model(identity, refresh=refresh)

    def resolve(self, reference: str, *, refresh: bool = False) -> ModelIdentity:
        return self._fake.resolve(reference, refresh=refresh)

    def _forward(self, request: GenerationRequest) -> GenerationRequest:
        self.adapters_asked.append(request.adapter)
        return dataclasses.replace(request, adapter=None)

    def generate(self, request: GenerationRequest) -> GenerationResult:
        return self._fake.generate(self._forward(request))

    def stream(self, request: GenerationRequest) -> Iterator[StreamEvent]:
        yield from self._fake.stream(self._forward(request))

    def load(self, identity: ModelIdentity, profile: Any) -> Any:
        return self._fake.load(identity, profile)

    def unload(self, identity: ModelIdentity) -> bool:
        return self._fake.unload(identity)

    def list_resident(self) -> Sequence[Any]:
        return self._fake.list_resident()

    def register_adapters(self, adapters: Sequence[Any]) -> Sequence[Any]:
        return ()


def _base() -> FakeModel:
    return FakeModel(
        name=BASE_NAME,
        digest="b" * 64,
        family="qwen2.5",
        parameter_count=1_500_000_000,
        quantization="Q8_0",
        size_bytes=GIB,
        max_context=32768,
        layers=28,
        kv_heads=2,
        head_dim=128,
    )


@pytest.fixture
def wired(tmp_path: Path) -> Any:
    """A database, an adapter directory with one reviewed adapter, and a hot-swapping provider."""
    directory = tmp_path / "adapters"
    directory.mkdir()
    _write_adapter(directory, "terse")

    handle = Database.from_url(f"sqlite:///{tmp_path / 'execution.sqlite3'}")
    ensure_ready(handle, auto_migrate=True)
    import_task_profiles(handle, read_task_profiles_file(), now=NOW)
    provider = AdapterCapableProvider(FakeProvider(FakeScript(models=(_base(),))))
    discover_models(
        handle,
        (
            ProviderRegistration(
                name="local",
                kind="fake",
                is_remote=False,
                provider=provider,  # type: ignore[arg-type]  # a Provider by protocol, not by base
                adapters_registered=True,
            ),
        ),
        now=NOW,
    )
    sync_adapters(handle, _settings(directory), now=NOW)
    yield handle, provider
    handle.close()


def _context(provider: AdapterCapableProvider) -> ExecutionContext:
    return ExecutionContext(
        provider=provider,  # type: ignore[arg-type]  # a Provider by protocol, not by base class
        provider_facts=provider_facts_for(provider, adapters_registered=True),  # type: ignore[arg-type]
        policy=RoutingPolicy(),
        schemas_dir=DEFAULT_SCHEMAS_DIR,
        now=lambda: NOW,
    )


def test_a_pinned_adapter_reaches_the_provider_and_is_recorded_on_the_attempt(
    wired: Any,
) -> None:
    database, provider = wired

    outcome = execute(
        database,
        GenerateRequest(
            task="general.chat",
            prompt="say something",
            overrides=RuntimeOverrides(adapter="terse"),
        ),
        _context(provider),
    )

    assert provider.adapters_asked == ["terse"]
    with database.read() as session:
        (attempt,) = session.execute(select(JobAttempt)).scalars().all()
    assert attempt.adapter_id is not None
    assert "+terse@sha256:" in (attempt.subject_canonical_id or "")
    assert attempt.adapter_data_classification == "confidential"
    assert attempt.effective_data_classification == "confidential"
    assert outcome.text


def test_a_bare_execution_names_no_adapter_anywhere(wired: Any) -> None:
    """The compatibility claim, at the execution edge: nothing about a bare run moved."""
    database, provider = wired

    execute(
        database,
        GenerateRequest(task="general.chat", prompt="say something"),
        _context(provider),
    )

    assert provider.adapters_asked == [None]
    with database.read() as session:
        (attempt,) = session.execute(select(JobAttempt)).scalars().all()
    assert attempt.adapter_id is None
    assert attempt.adapter_data_classification is None
    assert attempt.subject_canonical_id == "fake/qwen2.5:1.5b@sha256:" + "b" * 12


def test_alternating_adapters_on_one_base_load_it_once_and_write_one_residency_row(
    wired: Any, tmp_path: Path
) -> None:
    """Gate F, at the LoadCoach boundary: an adapter switch is not a load (ADR-0066, ADR-0038).

    Driven through the residency service directly, with a device to be resident on — the executor
    only reaches it when admission chose a GPU, and a machine with none has no residency to test.
    The live proof against a real `llama-server` is I16.
    """
    from loadcoach.config import ResidencySettings
    from loadcoach.infrastructure.db.models import Adapter, Model, Residency
    from loadcoach.services.residency import ResidencyService

    database, provider = wired
    directory = tmp_path / "adapters"
    _write_adapter(directory, "pirate")
    sync_adapters(database, _settings(directory), now=NOW)
    with database.read() as session:
        adapter_ids = {
            row.name: row.id for row in session.execute(select(Adapter)).scalars().all()
        }
        model = session.execute(select(Model)).scalars().one()
        model_id, canonical_id = model.id, model.canonical_id
        identity = ModelIdentity(
            provider_kind=model.provider_kind,
            provider_model_name=model.provider_model_name,
            artifact_digest=model.artifact_digest,
        )
    residency = ResidencyService(
        database, provider, settings=ResidencySettings(), clock=lambda: NOW
    )

    outcomes = [
        residency.ensure_loaded(
            model_id=model_id,
            canonical_id=canonical_id,
            identity=identity,
            profile=RuntimeProfile(),
            gpu_index=0,
            in_use_model_ids=frozenset(),
            required_bytes=None,
            free_bytes=None,
            headroom_bytes=0,
            now=NOW,
            adapter_id=adapter_ids[pinned],
            adapter_key=pinned,
        )
        for pinned in ("terse", "pirate", "terse")
    ]

    assert [outcome.loaded for outcome in outcomes] == [True, False, False]
    assert [outcome.already_resident for outcome in outcomes] == [False, True, True]
    assert not any(outcome.evicted for outcome in outcomes)
    with database.read() as session:
        rows = list(session.execute(select(Residency)).scalars().all())
    (row,) = rows
    assert row.resident
    # One episode, and it names the subject that last ran on it rather than one row per adapter.
    assert row.adapter_key == "terse"
    assert row.adapter_id == adapter_ids["terse"]
