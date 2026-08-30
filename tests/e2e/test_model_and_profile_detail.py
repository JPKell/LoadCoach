"""The model and task-profile detail routes, and discovery on demand (api.md §2)."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from loadcoach.bootstrap import bootstrap


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("LOADCOACH_PROVIDER__KIND", "fake")
    application = bootstrap()
    with TestClient(application.app, base_url="http://localhost") as test_client:
        yield test_client


def test_a_model_is_retrievable_by_its_ulid_or_an_unambiguous_prefix(client: TestClient) -> None:
    listed = client.get("/api/v1/models").json()["models"]
    assert listed
    model_ref = listed[0]["model_ref"]
    full = client.get(f"/api/v1/models/{model_ref}")
    assert full.status_code == 200, full.text
    body = full.json()
    assert body["canonical_id"] == listed[0]["canonical_id"]
    assert body["evidence"] == [] and body["reliability_by_task_profile"] == []
    assert body["circuit_breaker"] == {"state": "closed"}
    assert "descriptor" in body and "declared_capabilities" in body
    prefix = client.get(f"/api/v1/models/{model_ref[:8]}")
    assert prefix.status_code == 200 and prefix.json()["model_ref"] == model_ref
    missing = client.get("/api/v1/models/01NOPE0000000000000000000")
    assert missing.status_code == 404 and missing.json()["error"]["code"] == "MODEL_NOT_FOUND"
    # The canonical ID is not a reference: it does not survive a path segment (ADR-0024).
    assert client.get(f"/api/v1/models/{listed[0]['canonical_id']}").status_code == 404


def test_discovery_can_be_triggered_and_reports_counts(client: TestClient) -> None:
    response = client.post("/api/v1/models/discover")
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {"added", "updated", "unavailable", "total", "checked_at"}
    assert body["total"] >= 1 and body["added"] == 0  # already discovered at boot


def test_a_task_profile_is_retrievable_by_id(client: TestClient) -> None:
    profile = client.get("/api/v1/task-profiles/general.chat")
    assert profile.status_code == 200, profile.text
    body = profile.json()
    assert body["profile_id"] == "general.chat" and body["weights"]
    missing = client.get("/api/v1/task-profiles/no.such")
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "TASK_PROFILE_NOT_FOUND"
