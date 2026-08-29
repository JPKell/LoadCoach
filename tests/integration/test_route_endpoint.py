"""POST /route end to end: the explanation, its persistence, and its reproducibility.

Acceptance criteria 1, 1a, 2 and 3 are all asserted here, against a real database and a real
FakeProvider — no FreeWeight anywhere in the picture, which is the whole premise of Phase 3.
"""

from __future__ import annotations

import copy
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from baseaicore import UNSUPPORTED, RuntimeProfile
from fastapi.testclient import TestClient
from modelrack.testing import FakeModel, FakeProvider, FakeScript
from sweatmeter import GpuSample, TelemetrySnapshot

from loadcoach.config import load_settings
from loadcoach.domain.routing.subject import ProviderFacts, RuntimeOverrides
from loadcoach.services.database import Database, ensure_ready
from loadcoach.services.models import discover_models
from loadcoach.services.routing import (
    ConstraintsNotTightening,
    NoEligibleModel,
    RouteRequest,
    RoutingPolicy,
    TaskProfileNotFound,
    read_decision,
    route,
)
from loadcoach.services.task_profiles import import_task_profiles, read_task_profiles_file
from loadcoach.web.app import create_app

GIB = 1024**3
NOW = datetime(2026, 8, 29, 12, 0, 0, tzinfo=UTC)


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


def _database(tmp_path: Path, provider: FakeProvider) -> Database:
    database = Database.from_url(f"sqlite:///{tmp_path / 'route.sqlite3'}")
    ensure_ready(database, auto_migrate=True)
    import_task_profiles(database, read_task_profiles_file(), now=NOW)
    discover_models(database, provider, now=NOW)
    return database


def _snapshot(*gpus: tuple[int, int, int], ram: int = 32 * GIB) -> TelemetrySnapshot:
    return TelemetrySnapshot(
        timestamp=NOW,
        ram_available_bytes=ram,
        gpus=tuple(
            GpuSample(index=index, vram_total_bytes=total, vram_used_bytes=used)
            for index, total, used in gpus
        ),
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


def test_routes_with_no_evidence_and_says_evidence_none(tmp_path: Path) -> None:
    """Acceptance criterion 1, and 1a: the decision names its subject in full."""
    provider = FakeProvider(FakeScript(models=(_model("alpha:8b", "a" * 64),)))
    database = _database(tmp_path, provider)
    try:
        result = route(
            database,
            RouteRequest(task="general.chat", estimated_input_tokens=1000),
            provider=_facts(),
            policy=RoutingPolicy(),
            snapshot=_snapshot((0, 24 * GIB, 1 * GIB)),
            now=NOW,
        )
        payload = result.explanation.payload
        selected = payload["selected"]
        assert selected is not None
        assert payload["evidence_summary"]["source"] == "none"
        assert "low_evidence" in payload["flags"]

        # AC 1a: every decision names its resolved profile, its served context and that source.
        assert selected["runtime_profile_hash"]
        assert selected["served_context"] > 0
        assert selected["served_context_source"] in {"configured", "reported", "assumed"}
        assert selected["target_gpu_index"] == 0
        for candidate in payload["candidates"]:
            assert candidate["runtime_profile_hash"]
            assert candidate["served_context_source"] in {"configured", "reported", "assumed"}
    finally:
        database.close()


def test_the_decision_is_persisted_and_retrievable(tmp_path: Path) -> None:
    """Acceptance criterion 2."""
    provider = FakeProvider(FakeScript(models=(_model("alpha:8b", "a" * 64),)))
    database = _database(tmp_path, provider)
    try:
        result = route(
            database,
            RouteRequest(task="general.chat"),
            provider=_facts(),
            policy=RoutingPolicy(),
            snapshot=_snapshot((0, 24 * GIB, 1 * GIB)),
            now=NOW,
        )
        stored = read_decision(database, result.explanation.decision_id)
        assert stored == result.explanation.payload

        from loadcoach.infrastructure.db.models import RoutingCandidate, RoutingDecision

        with database.read() as session:
            decision = session.get(RoutingDecision, result.explanation.decision_id)
            assert decision is not None
            assert decision.selected_runtime_profile_id is not None
            assert (
                decision.selected_served_context
                == result.explanation.payload["selected"]["served_context"]
            )
            assert decision.selected_served_context_source is not None
            candidates = (
                session.query(RoutingCandidate)
                .filter_by(decision_id=result.explanation.decision_id)
                .all()
            )
            assert candidates
            assert all(row.runtime_profile_id is not None for row in candidates)
    finally:
        database.close()


def test_identical_inputs_produce_an_identical_decision(tmp_path: Path) -> None:
    """Determinism (routing §12), and acceptance criterion 3: reproducible from stored inputs."""
    provider = FakeProvider(
        FakeScript(
            models=(
                _model("alpha:8b", "a" * 64),
                _model("beta:13b", "b" * 64, parameter_count=13_000_000_000, size_bytes=13 * GIB),
                _model("gamma:3b", "c" * 64, parameter_count=3_000_000_000, size_bytes=3 * GIB),
            )
        )
    )
    database = _database(tmp_path, provider)
    try:
        request = RouteRequest(task="general.reasoning", estimated_input_tokens=2000)
        snapshot = _snapshot((0, 24 * GIB, 1 * GIB))
        first = route(
            database, request, provider=_facts(), policy=RoutingPolicy(), snapshot=snapshot, now=NOW
        )
        second = route(
            database, request, provider=_facts(), policy=RoutingPolicy(), snapshot=snapshot, now=NOW
        )

        golden = copy.deepcopy(first.explanation.payload)
        replay = copy.deepcopy(second.explanation.payload)
        for payload in (golden, replay):
            # The decision ID and the elapsed time are the only fields two runs may differ in.
            payload.pop("decision_id")
            payload.pop("duration_ms")
        assert golden == replay

        # And a third run from the *stored* decision's own inputs reproduces it too.
        stored = read_decision(database, first.explanation.decision_id)
        assert stored is not None
        third = route(
            database,
            request,
            provider=_facts(),
            policy=RoutingPolicy(),
            snapshot=snapshot,
            now=NOW,
            persist=False,
        )
        assert stored["telemetry_snapshot"] == third.explanation.payload["telemetry_snapshot"]
        assert (
            stored["selected"]["canonical_id"]
            == (third.explanation.payload["selected"]["canonical_id"])
        )
        assert (
            stored["selected"]["final_score"]
            == (third.explanation.payload["selected"]["final_score"])
        )
    finally:
        database.close()


def test_no_eligible_model_lists_every_candidate_with_its_reason(tmp_path: Path) -> None:
    provider = FakeProvider(
        FakeScript(
            models=(
                _model("alpha:8b", "a" * 64, max_context=4096),
                _model("beta:13b", "b" * 64, max_context=2048),
            )
        )
    )
    database = _database(tmp_path, provider)
    try:
        with pytest.raises(NoEligibleModel) as caught:
            route(
                database,
                RouteRequest(task="code.review"),
                # Not configurable, so the resolved context stays at the advertised maximum.
                provider=_facts(context_configurable=False),
                policy=RoutingPolicy(),
                snapshot=_snapshot((0, 24 * GIB, 1 * GIB)),
                now=NOW,
            )
        candidates = caught.value.details["candidates"]
        assert len(candidates) == 2
        assert {c["canonical_id"] for c in candidates} == {
            "fake/alpha:8b@sha256:" + "a" * 12,
            "fake/beta:13b@sha256:" + "b" * 12,
        }
        for candidate in candidates:
            assert candidate["reason"] == "context_too_small"
            assert candidate["detail"]["min_context_tokens"] == 16384
            assert candidate["detail"]["served_context"] in {4096, 2048}
        # Rejected candidates are persisted too, so the decision is retrievable afterwards.
        assert read_decision(database, caught.value.details["decision_id"]) is not None
    finally:
        database.close()


def test_a_model_advertising_131072_but_served_4096_is_rejected_not_truncated(
    tmp_path: Path,
) -> None:
    """The named failure mode: admitted on advertised context and silently truncated."""
    provider = FakeProvider(FakeScript(models=(_model("alpha:8b", "a" * 64, max_context=131072),)))
    database = _database(tmp_path, provider)
    try:
        with pytest.raises(NoEligibleModel) as caught:
            route(
                database,
                RouteRequest(task="code.review"),
                provider=_facts(context_configurable=True),
                # An operator pinned 4 096; the profile's 16 384 requirement is not silently
                # overridden, and the candidate is rejected rather than truncated.
                policy=RoutingPolicy(runtime_defaults=RuntimeProfile(context_size=4096)),
                snapshot=_snapshot((0, 24 * GIB, 1 * GIB)),
                now=NOW,
            )
        candidate = caught.value.details["candidates"][0]
        assert candidate["reason"] == "context_too_small"
        assert candidate["detail"]["served_context"] == 4096
        assert candidate["detail"]["advertised_max_context"] == 131072
        assert candidate["detail"]["min_context_tokens"] == 16384
    finally:
        database.close()


def test_assumed_context_is_flagged_when_it_can_only_be_assumed(tmp_path: Path) -> None:
    provider = FakeProvider(FakeScript(models=(_model("alpha:8b", "a" * 64),)))
    database = _database(tmp_path, provider)
    try:
        result = route(
            database,
            RouteRequest(task="general.chat"),
            provider=_facts(context_configurable=False),
            policy=RoutingPolicy(),
            snapshot=_snapshot((0, 24 * GIB, 1 * GIB)),
            now=NOW,
        )
        payload = result.explanation.payload
        assert payload["selected"]["served_context_source"] == "assumed"
        assert "assumed_context" in payload["flags"]
    finally:
        database.close()


def test_two_gpus_are_not_summed_end_to_end(tmp_path: Path) -> None:
    provider = FakeProvider(FakeScript(models=(_model("big:34b", "d" * 64, size_bytes=13 * GIB),)))
    database = _database(tmp_path, provider)
    try:
        with pytest.raises(NoEligibleModel) as caught:
            route(
                database,
                RouteRequest(task="general.chat"),
                provider=_facts(),
                policy=RoutingPolicy(vram_headroom_bytes=0),
                snapshot=_snapshot(
                    (0, 24 * GIB, 24 * GIB - 9_800_000_000),
                    (1, 24 * GIB, 24 * GIB - 7_100_000_000),
                ),
                now=NOW,
            )
        candidate = caught.value.details["candidates"][0]
        assert candidate["reason"] == "insufficient_vram"
        assert candidate["detail"]["free_bytes_by_gpu"] == {
            "0": 9_800_000_000,
            "1": 7_100_000_000,
        }
        assert candidate["detail"]["estimated_bytes"] > 9_800_000_000
        assert candidate["detail"]["estimated_bytes"] < 9_800_000_000 + 7_100_000_000
    finally:
        database.close()


def test_context_budgeting_rejects_with_numbers_end_to_end(tmp_path: Path) -> None:
    provider = FakeProvider()
    database = _database(tmp_path, provider)
    try:
        with pytest.raises(NoEligibleModel) as caught:
            route(
                database,
                RouteRequest(task="general.chat", estimated_input_tokens=1_000_000),
                provider=_facts(),
                policy=RoutingPolicy(),
                snapshot=_snapshot((0, 24 * GIB, 1 * GIB)),
                now=NOW,
            )
        candidate = caught.value.details["candidates"][0]
        assert candidate["reason"] == "context_limit_exceeded"
        assert candidate["detail"]["required_context"] > candidate["detail"]["served_context"]
        assert candidate["detail"]["context_budget"]["shortfall_tokens"] > 0
        assert candidate["detail"]["context_budget"]["estimated_input_tokens"] == 1_000_000
    finally:
        database.close()


def test_an_unknown_task_profile_is_refused_by_name(tmp_path: Path) -> None:
    provider = FakeProvider()
    database = _database(tmp_path, provider)
    try:
        with pytest.raises(TaskProfileNotFound) as caught:
            route(
                database,
                RouteRequest(task="nope.nope"),
                provider=_facts(),
                policy=RoutingPolicy(),
                now=NOW,
            )
        assert caught.value.details["task_profile_id"] == "nope.nope"
    finally:
        database.close()


def test_request_constraints_may_tighten_but_never_loosen(tmp_path: Path) -> None:
    from loadcoach.domain.task_profile import TaskProfileConstraints

    provider = FakeProvider()
    database = _database(tmp_path, provider)
    try:
        with pytest.raises(ConstraintsNotTightening) as caught:
            route(
                database,
                RouteRequest(
                    task="code.review",
                    constraints=TaskProfileConstraints(allow_remote_providers=True),
                ),
                provider=_facts(),
                policy=RoutingPolicy(),
                now=NOW,
            )
        assert "constraints.allow_remote_providers" in caught.value.details["fields"]
    finally:
        database.close()


def test_require_evidence_refuses_to_route_on_priors(tmp_path: Path) -> None:
    provider = FakeProvider(FakeScript(models=(_model("alpha:8b", "a" * 64),)))
    database = _database(tmp_path, provider)
    try:
        result = route(
            database,
            RouteRequest(task="general.chat", overrides=RuntimeOverrides(require_evidence=True)),
            provider=_facts(),
            policy=RoutingPolicy(),
            snapshot=_snapshot((0, 24 * GIB, 1 * GIB)),
            now=NOW,
        )
        candidate = result.explanation.payload["candidates"][0]
        assert all(score["source"] == "absent" for score in candidate["capabilities"])
        assert candidate["task_fit"] == 0.0
        assert result.explanation.payload["overrides"]["require_evidence"] is True
    finally:
        database.close()


def test_a_model_with_no_context_at_all_is_rejected_not_defaulted(tmp_path: Path) -> None:
    provider = FakeProvider(
        FakeScript(models=(_model("headless:8b", "e" * 64, max_context=UNSUPPORTED),))
    )
    database = _database(tmp_path, provider)
    try:
        with pytest.raises(NoEligibleModel) as caught:
            route(
                database,
                RouteRequest(task="general.chat"),
                provider=_facts(context_configurable=False),
                policy=RoutingPolicy(),
                now=NOW,
            )
        candidate = caught.value.details["candidates"][0]
        assert candidate["reason"] == "context_too_small"
        assert candidate["detail"]["served_context"] is None
    finally:
        database.close()


# --- the HTTP surface --------------------------------------------------------------------


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    url = f"sqlite:///{tmp_path / 'api.sqlite3'}"
    monkeypatch.setenv("LOADCOACH_STORAGE__DATABASE_URL", url)
    monkeypatch.setenv("LOADCOACH_PROVIDER__KIND", "fake")
    settings = load_settings().settings
    database = Database.from_url(url)
    ensure_ready(database, auto_migrate=True)
    import_task_profiles(database, read_task_profiles_file(), now=NOW)
    discover_models(database, FakeProvider(), now=NOW)
    database.close()
    with TestClient(create_app(settings), base_url="http://localhost") as test_client:
        yield test_client


def test_post_route_returns_the_explanation(client: TestClient) -> None:
    response = client.post(
        "/api/v1/route", json={"task": "general.chat", "estimated_input_tokens": 500}
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["selected"]["runtime_profile_hash"]
    assert payload["selected"]["served_context"] > 0
    assert payload["selected"]["served_context_source"] in {"configured", "reported", "assumed"}
    assert payload["evidence_summary"]["source"] == "none"


def test_post_route_for_an_unknown_task_is_404_in_the_standard_envelope(
    client: TestClient,
) -> None:
    response = client.post("/api/v1/route", json={"task": "no.such.task"})
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "TASK_PROFILE_NOT_FOUND"


def test_a_stored_decision_is_retrievable_over_http_and_rendered(client: TestClient) -> None:
    created = client.post("/api/v1/route", json={"task": "general.chat"})
    assert created.status_code == 200, created.text
    decision_id = created.json()["decision_id"]

    fetched = client.get(f"/api/v1/routing-decisions/{decision_id}")
    assert fetched.status_code == 200
    assert fetched.json() == created.json()

    listing = client.get("/api/v1/routing-decisions")
    assert decision_id in {row["decision_id"] for row in listing.json()["decisions"]}

    page = client.get(f"/routing/{decision_id}")
    assert page.status_code == 200
    assert "Routing explanation" in page.text
    assert created.json()["selected"]["runtime_profile_hash"] in page.text

    index = client.get("/routing")
    assert index.status_code == 200
    assert decision_id in index.text


def test_an_unknown_decision_is_404(client: TestClient) -> None:
    response = client.get("/api/v1/routing-decisions/01ABCDEFGHJKMNPQRSTVWXYZ00")
    assert response.status_code == 404
