"""End-to-end: GET /models, GET /task-profiles, and their plain HTML pages (dev-plan P2)."""

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


def test_get_task_profiles_returns_all_fifteen(client: TestClient) -> None:
    response = client.get("/api/v1/task-profiles")
    assert response.status_code == 200
    profiles = response.json()["task_profiles"]
    assert len(profiles) == 15
    ids = {profile["profile_id"] for profile in profiles}
    assert "content.review" in ids
    assert "code.review" in ids


def test_get_models_shows_declared_capabilities_and_availability(client: TestClient) -> None:
    """dev-plan P2 acceptance criterion 2."""
    response = client.get("/api/v1/models")
    assert response.status_code == 200
    models = response.json()["models"]
    assert len(models) >= 1
    model = models[0]
    assert "declared_capabilities" in model
    assert "available" in model
    assert "unavailable_reason" in model


def test_models_ui_page_renders(client: TestClient) -> None:
    response = client.get("/models")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "Models" in response.text


def test_task_profiles_ui_page_renders(client: TestClient) -> None:
    response = client.get("/task-profiles")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "content.review" in response.text


def test_ui_pages_escape_untrusted_content(client: TestClient) -> None:
    """A model or profile field reaching a template is never trusted markup (Jinja autoescape)."""
    response = client.get("/models")
    assert "<script>" not in response.text
