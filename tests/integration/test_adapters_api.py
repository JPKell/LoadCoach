"""``GET /adapters`` (api.md §2): the directory's adapters, where each is resident, how it routed.

Asked for by WeightRoomGym row WP2, whose LoadCoach tab could not otherwise show an adapter's
residency or the decisions that named it. The directory and its manifests are built by
``test_adapter_registry``'s own helpers; the residency and routing rows are written directly,
because producing them for real needs a provider that hot-swaps adapters and a GPU.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from tests.integration.test_adapter_registry import NOW, _artifact, _manifest

from loadcoach.bootstrap import bootstrap
from loadcoach.infrastructure.db.models import (
    Adapter,
    Model,
    Residency,
    RoutingCandidate,
    RoutingDecision,
)


def _client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, directory: Path | None
) -> TestClient:
    config_path = tmp_path / "config.toml"
    text = '[provider]\nkind = "fake"\n'
    if directory is not None:
        text += f'[adapters]\ndirectory = "{directory}"\n'
    config_path.write_text(text, encoding="utf-8")
    monkeypatch.setenv("LOADCOACH_CONFIG", str(config_path))
    return TestClient(bootstrap().app, base_url="http://localhost")


@pytest.fixture
def directory(tmp_path: Path) -> Path:
    found = tmp_path / "adapters"
    found.mkdir()
    _manifest(found, _artifact(found, "fact-check"))
    return found


def test_with_no_directory_the_feature_is_off_and_names_the_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _client(tmp_path, monkeypatch, directory=None) as client:
        response = client.get("/api/v1/adapters")
    assert response.status_code == 200
    body = response.json()
    assert body["enabled"] is False
    assert "[adapters] directory" in body["note"]
    assert body["adapters"] == []


def _routed(client: TestClient) -> Iterator[str]:
    """One resident base serving the adapter, a decision that selected it, one that refused it."""
    database = client.app.state.database  # type: ignore[attr-defined]  # FastAPI state
    with database.write() as session:
        adapter = session.execute(select(Adapter)).scalar_one()
        model = Model(
            provider_kind="fake",
            provider_model_name="qwen3.5:9b",
            canonical_id="fake/qwen3.5:9b@sha256:bbbb",
            identity_confidence="digest",
        )
        session.add(model)
        session.flush()
        for requested_at, selected, reason in (
            (NOW, True, None),
            (NOW - timedelta(hours=1), False, "adapter_classification_conflict"),
        ):
            decision = RoutingDecision(
                task_profile_id="auditing.fact_check",
                task_profile_version="1.0.0",
                strategy_name="weighted",
                strategy_version="1",
                confidence_policy_version="1",
                requested_at=requested_at,
                duration_ms=3,
                explanation_json={},
                selected_model_id=model.id if selected else None,
                selected_adapter_id=adapter.id if selected else None,
            )
            session.add(decision)
            session.flush()
            session.add(
                RoutingCandidate(
                    decision_id=decision.id,
                    model_id=model.id,
                    adapter_id=adapter.id,
                    rank=1 if selected else None,
                    rejected=not selected,
                    rejection_reason=reason,
                )
            )
        session.add(
            Residency(
                model_id=model.id,
                adapter_id=adapter.id,
                adapter_key=adapter.artifact_sha256,
                gpu_index=0,
                loaded_at=NOW,
                last_used_at=NOW,
                resident=True,
            )
        )
        yield adapter.id


def test_every_adapter_is_listed_with_its_row_residency_and_routes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, directory: Path
) -> None:
    with _client(tmp_path, monkeypatch, directory=directory) as client:
        (adapter_id,) = list(_routed(client))
        body = client.get("/api/v1/adapters").json()
    assert body["enabled"] is True
    assert body["directory"] == str(directory)
    (entry,) = body["adapters"]
    assert (entry["name"], entry["in_directory"], entry["adapter_id"]) == (
        "fact-check",
        True,
        adapter_id,
    )
    assert entry["data_classification"] == "confidential"
    assert entry["registered_on"] == []  # a fake provider cannot hot-swap, so holds none
    assert [(one["gpu_index"], one["base_canonical_id"]) for one in entry["resident"]] == [
        (0, "fake/qwen3.5:9b@sha256:bbbb")
    ]
    assert [(one["selected"], one["rank"], one["rejection_reason"]) for one in entry["routes"]] == [
        (True, 1, None),
        (False, None, "adapter_classification_conflict"),
    ]


def test_a_row_the_directory_no_longer_describes_is_still_listed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, directory: Path
) -> None:
    with _client(tmp_path, monkeypatch, directory=directory) as client:
        for path in directory.iterdir():
            path.unlink()
        body = client.get("/api/v1/adapters").json()
    (entry,) = body["adapters"]
    assert (entry["name"], entry["in_directory"]) == ("fact-check", False)
    assert entry["adapter_id"] is not None
    assert entry["routes"] == []
