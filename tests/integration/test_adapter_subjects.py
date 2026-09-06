"""Adapter subjects as routing candidates, and the three constraints that reject them by name.

ADR-0058 gives the execution subject an adapter axis; ADR-0064 gates routed selection on measured
evidence; ADR-0065 and ADR-0079 make a remote adapter candidate a named refusal rather than a
policy exclusion. The claims held here are that expansion happens **only** where a provider can
hot-swap, that each rejection is persisted and queryable, and that a deployment with no adapters
routes exactly as LoadCoach 1.0 did.

No provider process, no GPU, no network: the directory is real files, the registry is a real
database, and the provider is a value object stating what a provider would declare.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest
from baseaicore import DataClassification, IdentityConfidence
from modelrack.testing import FakeModel, FakeProvider, FakeScript
from setspec import GeneratorInfo, SchemaVersion, dump_envelope
from sqlalchemy import select

from loadcoach.config import Settings
from loadcoach.domain.routing.subject import ProviderFacts
from loadcoach.infrastructure.db.models import Adapter, RoutingCandidate
from loadcoach.infrastructure.providers.factory import ProviderRegistration
from loadcoach.services.adapters import adapter_facts_by_base_name, sync_adapters
from loadcoach.services.database import Database, ensure_ready
from loadcoach.services.models import discover_models
from loadcoach.services.routing import NoEligibleModel, RouteRequest, RoutingPolicy, route
from loadcoach.services.task_profiles import import_task_profiles, read_task_profiles_file

GIB = 1024**3
NOW = datetime(2026, 9, 5, 12, 0, 0, tzinfo=UTC)
BASE_NAME = "qwen2.5:1.5b"
BASE_DIGEST = "sha256:" + "b" * 64


def _model(name: str = BASE_NAME, digest: str = "b" * 64) -> FakeModel:
    return FakeModel(
        name=name,
        digest=digest,
        family=name.split(":")[0],
        parameter_count=1_500_000_000,
        quantization="Q8_0",
        size_bytes=GIB,
        max_context=32768,
        layers=28,
        kv_heads=2,
        head_dim=128,
        declared_capabilities=frozenset(),
    )


def _write_adapter(
    directory: Path,
    name: str,
    *,
    base_name: str = BASE_NAME,
    base_digest: str | None = BASE_DIGEST,
    capabilities: tuple[str, ...] = ("user.house_voice",),
    classification: DataClassification = DataClassification.CONFIDENTIAL,
) -> Path:
    """Write one artifact and its reviewed manifest, through SetSpec's own writer model."""
    from setspec.model.v1 import AdapterManifestOut

    from loadcoach.infrastructure.adapters.directory import sha256_of

    artifact = directory / f"{name}.gguf"
    artifact.write_bytes(f"lora-{name}".encode())
    payload = AdapterManifestOut.model_validate(
        {
            "name": name,
            "artifact_file": artifact.name,
            "artifact_sha256": sha256_of(artifact),
            "base": {
                "provider_model_name": base_name,
                "artifact_digest": base_digest,
                "identity_confidence": (
                    IdentityConfidence.DIGEST.value
                    if base_digest
                    else IdentityConfidence.NAME_ONLY.value
                ),
            },
            "declared_capabilities": list(capabilities),
            "data_classification": classification.value,
            "format": "gguf",
            "created_at": NOW.isoformat().replace("+00:00", "Z"),
        }
    )
    (directory / f"{name}.manifest.json").write_text(
        dump_envelope(
            payload,
            schema="model.adapter_manifest",
            version=SchemaVersion(1, 0),
            generator=GeneratorInfo(name="test", version="1.0.0"),
            generated_at=NOW,
        ),
        encoding="utf-8",
    )
    return artifact


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


@pytest.fixture
def database(tmp_path: Path) -> Any:
    handle = Database.from_url(f"sqlite:///{tmp_path / 'adapters.sqlite3'}")
    ensure_ready(handle, auto_migrate=True)
    import_task_profiles(handle, read_task_profiles_file(), now=NOW)
    discover_models(
        handle,
        (
            ProviderRegistration(
                name="local",
                kind="fake",
                is_remote=False,
                provider=FakeProvider(FakeScript(models=(_model(),))),
                adapters_registered=True,
            ),
        ),
        now=NOW,
    )
    yield handle
    handle.close()


def _remote_database(tmp_path: Path) -> Database:
    """A registry whose one model was discovered through a **remote** registration.

    `tools.agent.remote_cheap` is the profile used with it: I19's scenario is a deployment that
    allows remote providers and still refuses to send an adapter, so the policy exclusion must be
    out of the way before the classification refusal can be the thing that fires.
    """
    handle = Database.from_url(f"sqlite:///{tmp_path / 'remote.sqlite3'}")
    ensure_ready(handle, auto_migrate=True)
    import_task_profiles(handle, read_task_profiles_file(), now=NOW)
    model = _model()
    discover_models(
        handle,
        (
            ProviderRegistration(
                name="hosted",
                kind="fake",
                is_remote=True,
                provider=FakeProvider(
                    FakeScript(models=(dataclasses.replace(model, max_context=200000),))
                ),
                adapters_registered=True,
            ),
        ),
        now=NOW,
    )
    return handle


def _settings(directory: Path) -> Settings:
    return Settings.model_validate({"adapters": {"directory": str(directory)}})


def _candidates(handle: Database) -> list[RoutingCandidate]:
    with handle.read() as session:
        return list(session.execute(select(RoutingCandidate)).scalars().all())


# --------------------------------------------------------------------------------------------
# The table is the directory's projection
# --------------------------------------------------------------------------------------------


def test_sync_writes_one_row_per_reviewed_manifest_and_keeps_a_vanished_one(
    database: Any, tmp_path: Path
) -> None:
    """A row outlives its file: a stored decision names the adapter by foreign key (ADR-0080)."""
    directory = tmp_path / "adapters"
    directory.mkdir()
    _write_adapter(directory, "terse")
    _write_adapter(directory, "pirate")

    assert sync_adapters(database, _settings(directory), now=NOW) == 2
    assert set(adapter_facts_by_base_name(database)) == {BASE_NAME}

    (directory / "pirate.manifest.json").unlink()
    (directory / "pirate.gguf").unlink()
    assert sync_adapters(database, _settings(directory), now=NOW) == 1

    with database.read() as session:
        rows = {row.name: row for row in session.execute(select(Adapter)).scalars().all()}
    assert set(rows) == {"terse", "pirate"}
    assert rows["terse"].available
    assert not rows["pirate"].available
    assert "no reviewed manifest" in (rows["pirate"].unavailable_reason or "")


def test_adapters_are_off_when_no_directory_is_configured(database: Any) -> None:
    assert sync_adapters(database, Settings(), now=NOW) == 0
    assert adapter_facts_by_base_name(database) == {}


# --------------------------------------------------------------------------------------------
# Expansion
# --------------------------------------------------------------------------------------------


def test_an_adapter_subject_is_a_candidate_only_where_the_provider_can_hot_swap(
    database: Any, tmp_path: Path
) -> None:
    """ADR-0062 decision 5. A provider that cannot hot-swap contributes no adapter subjects."""
    directory = tmp_path / "adapters"
    directory.mkdir()
    _write_adapter(directory, "terse")
    sync_adapters(database, _settings(directory), now=NOW)

    without = route(
        database,
        RouteRequest(task="general.chat", estimated_input_tokens=100),
        provider=_facts(adapter_hot_swap=False),
        policy=RoutingPolicy(),
        now=NOW,
        persist=False,
    )
    subjects = {
        candidate.subject.subject_canonical_id for candidate in without.explanation.ranking.ordered
    } | {item.subject.subject_canonical_id for item in without.explanation.rejected}
    assert all("+terse@" not in subject for subject in subjects)

    with_swap = route(
        database,
        RouteRequest(task="general.chat", estimated_input_tokens=100),
        provider=_facts(adapter_hot_swap=True, adapters_registered=True),
        policy=RoutingPolicy(),
        now=NOW,
        persist=False,
    )
    rejected = {item.subject.subject_canonical_id: item for item in with_swap.explanation.rejected}
    assert any("+terse@sha256:" in subject for subject in rejected)


def test_a_bare_base_subject_string_is_byte_for_byte_the_canonical_id(
    database: Any, tmp_path: Path
) -> None:
    """ADR-0058 §3's additive claim, pinned: with no adapter, nothing about the string moves."""
    result = route(
        database,
        RouteRequest(task="general.chat", estimated_input_tokens=100),
        provider=_facts(),
        policy=RoutingPolicy(),
        now=NOW,
        persist=False,
    )
    primary = result.explanation.ranking.primary
    assert primary is not None
    assert primary.subject.subject_canonical_id == primary.subject.facts.canonical_id
    assert primary.subject.runtime_profile.adapters_registered is None


# --------------------------------------------------------------------------------------------
# The three constraints
# --------------------------------------------------------------------------------------------


def test_an_unmeasured_adapter_is_rejected_by_name_and_the_rejection_is_persisted(
    database: Any, tmp_path: Path
) -> None:
    """ADR-0064 rule 3: until FreeWeight measures adapters, every adapter subject is unmeasured."""
    directory = tmp_path / "adapters"
    directory.mkdir()
    _write_adapter(directory, "terse")
    sync_adapters(database, _settings(directory), now=NOW)

    route(
        database,
        RouteRequest(task="general.chat", estimated_input_tokens=100),
        provider=_facts(adapter_hot_swap=True, adapters_registered=True),
        policy=RoutingPolicy(),
        now=NOW,
    )

    rows = {row.subject_canonical_id: row for row in _candidates(database)}
    adapter_rows = [row for row in rows.values() if row.adapter_id is not None]
    assert len(adapter_rows) == 1
    row = adapter_rows[0]
    assert row.rejected
    assert row.rejection_reason == "adapter_unmeasured"
    detail = cast("dict[str, Any]", row.rejection_detail_json or {})
    assert detail["adapter"] == "terse"
    assert detail["capability"]
    assert "+terse@sha256:" in row.subject_canonical_id


def test_the_evidence_gate_can_be_turned_off_and_the_subject_then_scores(
    database: Any, tmp_path: Path
) -> None:
    directory = tmp_path / "adapters"
    directory.mkdir()
    _write_adapter(directory, "terse")
    sync_adapters(database, _settings(directory), now=NOW)

    result = route(
        database,
        RouteRequest(task="general.chat", estimated_input_tokens=100),
        provider=_facts(adapter_hot_swap=True, adapters_registered=True),
        policy=RoutingPolicy(require_adapter_evidence=False),
        now=NOW,
        persist=False,
    )

    subjects = {
        candidate.subject.subject_canonical_id for candidate in result.explanation.ranking.ordered
    }
    assert any("+terse@sha256:" in subject for subject in subjects)


def test_a_base_digest_mismatch_refuses_by_name_with_both_digests(
    database: Any, tmp_path: Path
) -> None:
    """ADR-0058 §5: refused rather than tried, and the detail names what disagreed."""
    directory = tmp_path / "adapters"
    directory.mkdir()
    _write_adapter(directory, "terse", base_digest="sha256:" + "c" * 64)
    sync_adapters(database, _settings(directory), now=NOW)

    result = route(
        database,
        RouteRequest(task="general.chat", estimated_input_tokens=100),
        provider=_facts(adapter_hot_swap=True, adapters_registered=True),
        policy=RoutingPolicy(require_adapter_evidence=False),
        now=NOW,
        persist=False,
    )

    (rejection,) = [
        item for item in result.explanation.rejected if item.subject.adapter is not None
    ]
    assert rejection.rejection.reason == "adapter_incompatible"
    detail = rejection.rejection.detail
    assert detail["declared_base_digest"] == "sha256:" + "c" * 64
    assert detail["served_base_digest"] == "sha256:" + "b" * 64


def test_a_remote_candidate_carrying_an_adapter_is_a_classification_refusal(
    tmp_path: Path,
) -> None:
    """I19's recorded denial: a LoadCoach explanation row, never a governance ledger entry.

    Remote is *allowed* here, so `excluded_by_policy` never fires and the refusal that does is the
    one no flag can fix (ADR-0079).
    """
    directory = tmp_path / "adapters"
    directory.mkdir()
    _write_adapter(directory, "house_voice")
    handle = _remote_database(tmp_path)
    try:
        sync_adapters(handle, _settings(directory), now=NOW)
        route(
            handle,
            RouteRequest(task="tools.agent.remote_cheap", estimated_input_tokens=100),
            provider=_facts(adapter_hot_swap=True, adapters_registered=True, is_remote=True),
            policy=RoutingPolicy(require_adapter_evidence=False),
            now=NOW,
        )

        rows = [row for row in _candidates(handle) if row.adapter_id is not None]
        (row,) = rows
        assert row.rejection_reason == "adapter_classification_conflict"
        detail = cast("dict[str, Any]", row.rejection_detail_json or {})
        assert detail["adapter_classification"] == "confidential"
        assert detail["effective_classification"] == "confidential"
        assert detail["provider_remote"] is True
        assert detail["adapter"] == "house_voice"
    finally:
        handle.close()


def test_a_remote_registration_without_adapters_still_rejects_as_policy(tmp_path: Path) -> None:
    """The two rejections stay distinct: one is fixed by a flag, the other by nothing."""
    handle = _remote_database(tmp_path)
    try:
        with pytest.raises(NoEligibleModel):
            route(
                handle,
                RouteRequest(task="general.chat", estimated_input_tokens=100),
                provider=_facts(is_remote=True),
                policy=RoutingPolicy(),
                now=NOW,
            )

        reasons = {row.rejection_reason for row in _candidates(handle)}
        assert reasons == {"excluded_by_policy"}
    finally:
        handle.close()
