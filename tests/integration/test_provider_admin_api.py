"""The provider admin end to end: the file is edited, and the running server re-registers.

ADR-0117. No network and no GPU — the registrations are ``fake`` providers, and the point being
tested is what happens to the configuration file and to ``app.state``, not what a model says.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from loadcoach.bootstrap import bootstrap

_FILE = """\
# the deployment note nobody wants to lose
[provider]
kind = "fake"
"""


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    config_path = tmp_path / "config.toml"
    config_path.write_text(_FILE, encoding="utf-8")
    monkeypatch.setenv("LOADCOACH_CONFIG", str(config_path))
    application = bootstrap()
    with TestClient(application.app, base_url="http://localhost") as test_client:
        yield test_client


def _config_path(client: TestClient) -> Path:
    return Path(client.app.state.config_path)  # type: ignore[attr-defined]  # FastAPI


def test_the_configured_registration_is_listed(client: TestClient) -> None:
    document = client.get("/api/v1/providers").json()
    assert [registration["name"] for registration in document["registrations"]] == ["fake"]
    assert document["allow_remote"] is False


def test_a_written_registration_reaches_the_file_and_the_running_server(
    client: TestClient,
) -> None:
    response = client.put("/api/v1/providers/second", json={"kind": "fake"})
    assert response.status_code == 200
    names = [registration["name"] for registration in response.json()["registrations"]]
    assert names == ["fake", "second"]
    text = _config_path(client).read_text(encoding="utf-8")
    assert "# the deployment note nobody wants to lose" in text
    assert "[providers.second]" in text
    live = {
        registration.name
        for registration in client.app.state.provider_registrations  # type: ignore[attr-defined]
    }
    assert live == {"fake", "second"}


def test_the_egress_boundary_cannot_be_moved_from_here(client: TestClient) -> None:
    """``providers.allow_remote`` stays config-only (ADR-0117 decision 3)."""
    response = client.put("/api/v1/providers/second", json={"kind": "fake", "allow_remote": True})
    assert response.status_code == 400
    assert "allow_remote" in response.text
    assert "allow_remote" not in _config_path(client).read_text(encoding="utf-8")


def test_the_last_registration_cannot_be_deleted(client: TestClient) -> None:
    response = client.delete("/api/v1/providers/fake")
    assert response.status_code == 400
    assert client.get("/api/v1/providers").json()["registrations"]


def test_the_page_renders_every_registration(client: TestClient) -> None:
    page = client.get("/providers")
    assert page.status_code == 200
    assert "[providers.&lt;name&gt;]" in page.text
    assert "fake" in page.text


def test_a_disabled_model_is_refused_by_routing_and_named(client: TestClient) -> None:
    """ADR-0118: the exclusion is a rejection reason, not a model that quietly disappears."""
    models = client.get("/api/v1/models").json()["models"]
    assert models, "the fake provider serves at least one model"
    model_ref = models[0]["model_ref"]

    disabled = client.post(f"/api/v1/models/{model_ref}/enabled", json={"enabled": False})
    assert disabled.status_code == 200
    assert disabled.json()["enabled"] is False

    response = client.post("/api/v1/route", json={"task": "general.chat"})
    assert response.status_code == 422
    body = response.json()
    reasons = str(body)
    assert "model_disabled" in reasons

    # The row is still there, with its history, and re-enabling restores exactly what was there.
    again = client.post(f"/api/v1/models/{model_ref}/enabled", json={"enabled": True})
    assert again.json()["enabled"] is True
    assert client.post("/api/v1/route", json={"task": "general.chat"}).status_code == 200
