"""End-to-end: GET /models, GET /task-profiles, and their plain HTML pages (dev-plan P2)."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from loadcoach.bootstrap import bootstrap
from loadcoach.config import load_settings


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
    # api.md §2: the evidence summary, reliability and residency arrive with P8.
    assert model["model_ref"] and "/" not in model["model_ref"]
    assert model["evidence_summary"] == {"bound": 0, "capabilities": 0, "stale": 0, "unmatched": 0}
    assert model["reliability"]["pairs"] == 0 and model["reliability"]["lowest_factor"] is None
    assert model["residency"] == {"resident": False, "gpu_indexes": []}


def test_models_ui_page_renders(client: TestClient) -> None:
    response = client.get("/models")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "Models" in response.text
    assert "none imported" in response.text  # evidence coverage
    assert "no production evidence" in response.text  # reliability
    assert '<th scope="col">Resident</th>' in response.text


def test_task_profiles_ui_page_renders(client: TestClient) -> None:
    response = client.get("/task-profiles")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "content.review" in response.text


def test_ui_pages_escape_untrusted_content(client: TestClient) -> None:
    """A model field reaching a template is never trusted markup (Jinja autoescape).

    The name is injected into the registry first: asserting that a page with nothing hostile on it
    contains nothing hostile proves nothing, and passed for years before this became a real test.
    """
    from datetime import UTC, datetime

    from loadcoach.infrastructure.db.models import Model
    from loadcoach.services.database import Database

    hostile = '<script>alert("xss")</script>'
    url = load_settings().settings.storage.database_url
    assert url is not None
    with Database.from_url(url) as database, database.write() as session:
        session.add(
            Model(
                provider_kind="fake",
                provider_model_name=hostile,
                artifact_digest=None,
                canonical_id=f"fake/{hostile}@unknown",
                identity_confidence="name_only",
                first_seen_at=datetime.now(UTC),
                last_seen_at=datetime.now(UTC),
            )
        )

    response = client.get("/models")
    assert response.status_code == 200
    assert hostile not in response.text
    assert "&lt;script&gt;" in response.text
    # The shell's own theme bootstrap is a legitimate inline script and must still be there: it
    # runs before first paint, which is what stops a dark-mode reader seeing a white flash.
    assert "localStorage.getItem" in response.text
