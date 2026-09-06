"""A subject's own evidence scores it, and nothing else's does (ADR-0081, ADR-0085).

The consumer half of LA3, one layer above the importer: evidence imported for ``(base, terse)``
reaches the ``terse`` candidate, does not reach the base's, and does not reach ``pirate``'s. The
contrast is the whole point — a measured adapter passes ``require_adapter_evidence`` and an
unmeasured sibling on the same base is still rejected by name in the same decision.

No provider process, no GPU, no network: real files, a real database, a value object for the
provider, and a bundle built by substituting into a SetSpec golden.
"""

from __future__ import annotations

import copy
from typing import TYPE_CHECKING, Any, cast

import pytest
from modelrack.testing import FakeProvider, FakeScript
from sqlalchemy import select
from tests.integration.test_adapter_subjects import (
    BASE_NAME,
    NOW,
    _facts,
    _model,
    _settings,
    _write_adapter,
)

from loadcoach.infrastructure.db.models import Adapter, Model, RoutingCandidate
from loadcoach.infrastructure.providers.factory import ProviderRegistration
from loadcoach.services.adapters import sync_adapters
from loadcoach.services.database import Database, ensure_ready
from loadcoach.services.evidence import import_bundle
from loadcoach.services.models import discover_models
from loadcoach.services.routing import RouteRequest, RoutingPolicy, route
from loadcoach.services.task_profiles import import_task_profiles, read_task_profiles_file

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

WEIGHTED = ("reasoning", "instruction_following", "creative_writing", "reliability")
MACHINE = "9d1c4a5f2b7e83c04e6a1f9b2d5c7e83"


@pytest.fixture
def database(tmp_path: Path) -> Any:
    handle = Database.from_url(f"sqlite:///{tmp_path / 'evidence-routing.sqlite3'}")
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


def _local_identity(handle: Database) -> dict[str, Any]:
    with handle.read() as session:
        row = session.execute(select(Model)).scalars().one()
        return {
            "provider_kind": row.provider_kind,
            "provider_model_name": row.provider_model_name,
            "artifact_digest": row.artifact_digest,
            "canonical_id": row.canonical_id,
            "identity_confidence": row.identity_confidence,
            "observed_at": NOW.isoformat().replace("+00:00", "Z"),
        }


def _adapter_digest(handle: Database, name: str) -> str:
    with handle.read() as session:
        return (
            session.execute(select(Adapter.artifact_sha256).where(Adapter.name == name))
            .scalars()
            .one()
        )


def _route(database: Database, *, persist: bool = True, **kwargs: Any) -> Any:
    """One decision, with this machine pinned so a measurement is never foreign to it."""
    return route(
        database,
        RouteRequest(task="general.chat", estimated_input_tokens=100),
        provider=_facts(adapter_hot_swap=True, adapters_registered=True),
        policy=RoutingPolicy(machine_fingerprint=MACHINE, **kwargs),
        now=NOW,
        persist=persist,
    )


def _profile_hash(database: Database) -> str:
    """The hash the routed candidates actually resolve to, read from a decision rather than guessed.

    Evidence measured under a different profile is excluded by name (ADR-0023), so a test that
    invented a hash would prove the exclusion rather than the binding.
    """
    primary = _route(database, persist=False).explanation.ranking.primary
    assert primary is not None
    return str(primary.subject.runtime_profile_hash)


def _bundle(
    golden: dict[str, Any],
    *,
    identity: dict[str, Any],
    adapter: dict[str, Any] | None,
    profile_hash: str,
) -> dict[str, Any]:
    """One record per weighted capability, on one subject, at a score nothing else can reach."""
    bundle = copy.deepcopy(golden)
    template = copy.deepcopy(bundle["evidence"][0])
    template["model"] = copy.deepcopy(identity)
    records = []
    for capability in WEIGHTED:
        record = copy.deepcopy(template)
        record["capability_id"] = capability
        record["score"] = 0.97
        record["confidence"] = 0.9
        record["runtime_profile_hash"] = profile_hash
        record["machine_fingerprint"] = MACHINE
        record.pop("goal_hash", None)
        record.pop("calibration", None)
        record.pop("judge_set", None)
        record.pop("score_method_mix", None)
        record.pop("judge_validity_factor", None)
        if adapter is None:
            record.pop("adapter", None)
        else:
            record["adapter"] = copy.deepcopy(adapter)
        records.append(record)
    bundle["evidence"] = records
    return bundle


def _adapter_block(handle: Database, name: str) -> dict[str, Any]:
    digest = _adapter_digest(handle, name)
    return {
        "name": name,
        "artifact_digest": digest,
        "source_digest": None,
        "canonical_suffix": f"+{name}@{digest[:19]}",
    }


def _candidates(handle: Database) -> list[RoutingCandidate]:
    with handle.read() as session:
        return list(session.execute(select(RoutingCandidate)).scalars().all())


def _sync_two_adapters(handle: Database, tmp_path: Path) -> None:
    directory = tmp_path / "adapters"
    directory.mkdir(exist_ok=True)
    _write_adapter(directory, "terse", base_name=BASE_NAME)
    _write_adapter(directory, "pirate", base_name=BASE_NAME)
    sync_adapters(handle, _settings(directory), now=NOW)


def test_a_measured_adapter_subject_is_selected_and_its_unmeasured_sibling_is_not(
    database: Any,
    tmp_path: Path,
    golden_bundle_11: dict[str, Any],
    wrap_bundle: Callable[..., str],
) -> None:
    """I18's claim, in one decision: measured adapter selected, unmeasured sibling refused.

    Both adapters sit on the same base and declare the same manifest. The only difference between
    them is that one has imported evidence, which is exactly the difference
    ``require_adapter_evidence`` exists to notice (ADR-0064 rule 3).
    """
    _sync_two_adapters(database, tmp_path)
    identity = _local_identity(database)
    bundle = _bundle(
        golden_bundle_11,
        identity=identity,
        adapter=_adapter_block(database, "terse"),
        profile_hash=_profile_hash(database),
    )

    outcome = import_bundle(database, wrap_bundle(bundle, minor=1), now=NOW)
    assert outcome.rejected == ()
    assert outcome.bound == len(WEIGHTED)

    result = _route(database)

    primary = result.explanation.ranking.primary
    assert primary is not None
    assert "+terse@sha256:" in primary.subject.subject_canonical_id
    assert any(
        score.source == "benchmark"
        for score in primary.fit.capabilities
        if score.capability_id in WEIGHTED
    ), "the explanation names the evidence that moved the decision"

    rows = {row.subject_canonical_id: row for row in _candidates(database)}
    pirate = next(row for row in rows.values() if "+pirate@" in row.subject_canonical_id)
    assert pirate.rejected
    assert pirate.rejection_reason == "adapter_unmeasured"
    detail = cast("dict[str, Any]", pirate.rejection_detail_json or {})
    assert detail["adapter"] == "pirate"


def test_without_that_evidence_the_base_is_selected_and_the_adapter_is_refused(
    database: Any, tmp_path: Path
) -> None:
    """The same decision, one input removed: the adapter's evidence is what changed it."""
    _sync_two_adapters(database, tmp_path)

    result = _route(database)

    primary = result.explanation.ranking.primary
    assert primary is not None
    assert primary.subject.adapter is None
    assert "+" not in primary.subject.subject_canonical_id.split("@")[-1]

    reasons = {row.rejection_reason for row in _candidates(database) if row.rejected}
    assert reasons == {"adapter_unmeasured"}


def test_a_bases_evidence_never_reaches_an_adapter_subject(
    database: Any,
    tmp_path: Path,
    golden_bundle_11: dict[str, Any],
    wrap_bundle: Callable[..., str],
) -> None:
    """ADR-0081, asserted as an absence: the failure here looks exactly like the feature working.

    The **base** is measured superbly and both adapters are measured nowhere. If evidence flowed
    down the base to its subjects, both would pass the gate and one would very likely be selected.
    """
    _sync_two_adapters(database, tmp_path)
    identity = _local_identity(database)
    bundle = _bundle(
        golden_bundle_11,
        identity=identity,
        adapter=None,
        profile_hash=_profile_hash(database),
    )

    import_bundle(database, wrap_bundle(bundle, minor=1), now=NOW)

    _route(database)

    adapter_rows = [row for row in _candidates(database) if row.adapter_id is not None]
    assert len(adapter_rows) == 2
    assert all(row.rejection_reason == "adapter_unmeasured" for row in adapter_rows)


def test_adapters_sync_registers_the_directory_and_binds_what_was_waiting(
    tmp_path: Path,
    golden_bundle_11: dict[str, Any],
    wrap_bundle: Callable[..., str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`loadcoach adapters sync` is the command for the sequence an operator actually performs.

    Import a bundle, review a manifest, expect the evidence to attach. Before this command the
    attaching happened as a side effect of the next routed decision, which is not something a
    person can be told to run.
    """
    import json as json_module

    from typer.testing import CliRunner

    from loadcoach.cli.main import app as cli

    database_path = tmp_path / "sync.sqlite3"
    database = Database.from_url(f"sqlite:///{database_path}")
    ensure_ready(database, auto_migrate=True)
    import_task_profiles(database, read_task_profiles_file(), now=NOW)
    discover_models(
        database,
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
    identity = _local_identity(database)
    directory = tmp_path / "adapters"
    directory.mkdir()
    _write_adapter(directory, "terse", base_name=BASE_NAME)
    from loadcoach.infrastructure.adapters.directory import sha256_of

    digest = sha256_of(directory / "terse.gguf")
    bundle = _bundle(
        golden_bundle_11,
        identity=identity,
        adapter={
            "name": "terse",
            "artifact_digest": digest,
            "source_digest": None,
            "canonical_suffix": f"+terse@{digest[:19]}",
        },
        profile_hash="whatever-this-test-does-not-route",
    )
    outcome = import_bundle(database, wrap_bundle(bundle, minor=1), now=NOW)
    assert outcome.unmatched == len(WEIGHTED), "the adapter is not registered yet"
    database.close()

    config = tmp_path / "config.toml"
    config.write_text(
        f'[storage]\ndatabase_url = "sqlite:///{database_path}"\n\n'
        f'[adapters]\ndirectory = "{directory}"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("LOADCOACH_CONFIG", str(config))

    result = CliRunner().invoke(cli, ["adapters", "sync", "--json"])

    assert result.exit_code == 0, result.output
    payload = json_module.loads(result.stdout)
    assert payload["adapters"] == 1
    assert payload["evidence"]["bound"] == len(WEIGHTED)
    assert payload["evidence"]["unmatched"] == 0
