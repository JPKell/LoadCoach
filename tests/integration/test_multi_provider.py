"""LC-E1 end to end: two registrations, one tagged pool, and the compatibility golden.

ADR-0055 generalized LoadCoach's single ``[provider]`` block into named registrations. The claim
that matters is that nothing downstream of the registry changed meaning: filtering, scoring,
ranking and residency are unchanged code over a larger pool, and a 1.0 configuration produces the
registry it always did.

No FreeWeight, no network, no GPU — two ``FakeProvider``s serving different models.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from modelrack.testing import FakeModel, FakeProvider, FakeScript

from loadcoach.config import Settings
from loadcoach.domain.routing.subject import ProviderFacts
from loadcoach.infrastructure.providers.factory import (
    ProviderRegistration,
    build_provider,
    build_registrations,
)
from loadcoach.services.database import Database, ensure_ready
from loadcoach.services.models import discover_models, list_registry
from loadcoach.services.routing import RouteRequest, RoutingPolicy, route
from loadcoach.services.task_profiles import import_task_profiles, read_task_profiles_file

GIB = 1024**3
NOW = datetime(2026, 9, 5, 12, 0, 0, tzinfo=UTC)


def _model(name: str, digest: str, **overrides: Any) -> FakeModel:
    defaults: dict[str, Any] = {
        "family": name.split(":")[0],
        "parameter_count": 8_000_000_000,
        "quantization": "Q8_0",
        "size_bytes": 8 * GIB,
        "max_context": 32768,
        "layers": 32,
        "kv_heads": 8,
        "head_dim": 128,
        "declared_capabilities": frozenset(),
    }
    defaults.update(overrides)
    return FakeModel(name=name, digest=digest, **defaults)


def _registration(name: str, model: FakeModel, *, remote: bool = False) -> ProviderRegistration:
    return ProviderRegistration(
        name=name,
        kind="fake",
        is_remote=remote,
        provider=FakeProvider(FakeScript(models=(model,))),
    )


def _database(tmp_path: Path) -> Database:
    database = Database.from_url(f"sqlite:///{tmp_path / 'multi.sqlite3'}")
    ensure_ready(database, auto_migrate=True)
    import_task_profiles(database, read_task_profiles_file(), now=NOW)
    return database


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
    handle = _database(tmp_path)
    yield handle
    handle.close()


def test_discovery_tags_every_model_with_the_registration_that_served_it(database: Any) -> None:
    registrations = (
        _registration("local", _model("alpha:8b", "a" * 64)),
        _registration("hosted", _model("beta:8b", "b" * 64), remote=True),
    )

    outcome = discover_models(database, registrations, now=NOW)

    assert outcome.total == 2
    assert outcome.added == 2
    assert outcome.unreachable == ()
    entries = {entry.canonical_id: entry for entry in list_registry(database)}
    assert len(entries) == 2


def test_two_registrations_route_across_both(database: Any) -> None:
    """ADR-0055 rule 3: one pool, tagged — a mixed candidate list, ranked together."""
    registrations = (
        _registration("local", _model("alpha:8b", "a" * 64)),
        _registration("hosted", _model("beta:8b", "b" * 64), remote=True),
    )
    discover_models(database, registrations, now=NOW)

    result = route(
        database,
        RouteRequest(task="general.chat", estimated_input_tokens=1000),
        provider=_facts(),
        provider_facts_by_name={
            "local": _facts(),
            "hosted": _facts(is_remote=True),
        },
        policy=RoutingPolicy(),
        now=NOW,
    )

    payload = result.explanation.payload
    considered = {candidate["canonical_id"] for candidate in payload["candidates"]}
    rejected = {entry["canonical_id"] for entry in payload["rejected"]}
    # `general.chat` allows no remote provider, so the remote registration's model is in the
    # decision as a *named rejection* rather than missing from it.
    assert any("alpha:8b" in name for name in considered)
    assert any("beta:8b" in name for name in rejected)


def test_a_remote_registration_is_excluded_by_the_existing_reason(database: Any) -> None:
    """The rejection is `excluded_by_policy` — the string a 1.0 caller already switches on."""
    registrations = (
        _registration("local", _model("alpha:8b", "a" * 64)),
        _registration("hosted", _model("beta:8b", "b" * 64), remote=True),
    )
    discover_models(database, registrations, now=NOW)

    result = route(
        database,
        RouteRequest(task="general.chat"),
        provider=_facts(),
        provider_facts_by_name={"local": _facts(), "hosted": _facts(is_remote=True)},
        policy=RoutingPolicy(),
        now=NOW,
    )

    rejections = {
        entry["canonical_id"]: entry["reason"] for entry in result.explanation.payload["rejected"]
    }
    remote = next(name for name in rejections if "beta:8b" in name)
    assert rejections[remote] == "excluded_by_policy"


def test_a_remote_registration_is_routable_when_the_profile_allows_it(database: Any) -> None:
    """The other half: `remote` is a fact about the registration, not a ban."""
    registrations = (_registration("hosted", _model("beta:8b", "b" * 64), remote=True),)
    discover_models(database, registrations, now=NOW)

    result = route(
        database,
        RouteRequest(task="tools.agent.remote_cheap"),
        provider=_facts(),
        provider_facts_by_name={"hosted": _facts(is_remote=True)},
        policy=RoutingPolicy(),
        now=NOW,
    )

    selected = result.explanation.payload["selected"]
    assert selected is not None
    assert "beta:8b" in selected["canonical_id"]


def test_one_unreachable_registration_never_empties_a_working_registry(database: Any) -> None:
    """An unreachable provider is availability, not a statement that its models are gone."""
    from modelrack.errors import ProviderUnavailable

    class _Dead(FakeProvider):
        def list_models(self, *, refresh: bool = False) -> Any:
            raise ProviderUnavailable("nothing is listening")

    working = _registration("local", _model("alpha:8b", "a" * 64))
    discover_models(database, (working,), now=NOW)
    assert len(list_registry(database)) == 1

    dead = ProviderRegistration(
        name="hosted", kind="fake", is_remote=True, provider=_Dead(FakeScript(models=()))
    )
    outcome = discover_models(database, (working, dead), now=NOW)

    assert outcome.unreachable == ("hosted",)
    assert outcome.unavailable == 0
    assert [entry.available for entry in list_registry(database)] == [True]


def test_every_registration_failing_still_raises(database: Any) -> None:
    """With one registration this is the 1.0 contract, unchanged."""
    from modelrack.errors import ProviderError, ProviderUnavailable

    class _Dead(FakeProvider):
        def list_models(self, *, refresh: bool = False) -> Any:
            raise ProviderUnavailable("nothing is listening")

    dead = ProviderRegistration(
        name="local", kind="fake", is_remote=False, provider=_Dead(FakeScript(models=()))
    )

    with pytest.raises(ProviderError):
        discover_models(database, (dead,), now=NOW)


def test_a_singular_configuration_produces_the_registry_it_always_did(tmp_path: Path) -> None:
    """The compatibility golden: 1.0's configuration, 1.1's code, byte-identical rows.

    Asserted rather than argued (Phase 10 acceptance criterion 5). Discovery through the bare
    provider handle a 1.0 caller passes and through the singular block's registration must write
    the same identity columns; the registration adds a name and an egress class and changes
    nothing else.
    """
    bare_db = Database.from_url(f"sqlite:///{tmp_path / 'bare.sqlite3'}")
    ensure_ready(bare_db, auto_migrate=True)
    named_db = Database.from_url(f"sqlite:///{tmp_path / 'named.sqlite3'}")
    ensure_ready(named_db, auto_migrate=True)
    try:
        settings = Settings.model_validate({"provider": {"kind": "fake"}})
        # 1.0's composition root, and 1.1's, over the same singular block.
        discover_models(bare_db, build_provider(settings.provider), now=NOW)
        registrations = build_registrations(settings)
        discover_models(named_db, registrations, now=NOW)

        def rows(handle: Database) -> list[tuple[Any, ...]]:
            from loadcoach.infrastructure.db.models import Model

            with handle.read() as session:
                return [
                    (
                        row.canonical_id,
                        row.provider_kind,
                        row.provider_model_name,
                        row.artifact_digest,
                        row.identity_confidence,
                        row.max_context,
                        row.size_bytes,
                        row.available,
                        row.is_remote,
                    )
                    for row in session.query(Model).order_by(Model.canonical_id).all()
                ]

        assert rows(bare_db) == rows(named_db)
        with named_db.read() as session:
            from loadcoach.infrastructure.db.models import Model

            assert {row.provider_name for row in session.query(Model).all()} == {"fake"}
    finally:
        bare_db.close()
        named_db.close()


def test_the_models_listing_renders_the_registration_and_its_egress_class(database: Any) -> None:
    """ADR-0099 rule 1: `GET /models` carries `provider_name` and `is_remote` per entry.

    LoadCoach 1.1.0 recorded both columns and rendered neither in the listing, so ADR-0098 rule 1
    read `False` from a real deployment whatever was registered (I2 handoff §5). The
    pre-registration row — `""` and `False`, migration 0008's honest defaults — renders as
    recorded, never guessed at from the provider kind.
    """
    from loadcoach.infrastructure.db.models import Model
    from loadcoach.services.models import registry_overview
    from loadcoach.web.routes.models import _model_to_json

    discover_models(
        database,
        (
            _registration("local", _model("alpha:8b", "a" * 64)),
            _registration("hosted", _model("beta:8b", "b" * 64), remote=True),
        ),
        now=NOW,
    )
    # A row as migration 0008 left one discovered before registrations had names.
    with database.write() as session:
        row = session.query(Model).filter(Model.provider_model_name == "alpha:8b").one()
        row.provider_name = ""
        row.is_remote = False

    rendered = {
        str(entry["provider_model_name"]): entry
        for entry in (_model_to_json(overview) for overview in registry_overview(database))
    }
    assert (rendered["beta:8b"]["provider_name"], rendered["beta:8b"]["is_remote"]) == (
        "hosted",
        True,
    )
    assert (rendered["alpha:8b"]["provider_name"], rendered["alpha:8b"]["is_remote"]) == ("", False)
