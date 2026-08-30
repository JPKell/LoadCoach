"""``GET``/``PUT /settings`` and the Settings page (api.md §9): runtime-changeable keys only."""

from __future__ import annotations

import time
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from mirrorwall import CSRF_COOKIE_NAME, CSRF_FIELD_NAME

from loadcoach.bootstrap import bootstrap


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("LOADCOACH_PROVIDER__KIND", "fake")
    application = bootstrap()
    with TestClient(application.app, base_url="http://localhost") as test_client:
        yield test_client


def test_get_settings_lists_the_runtime_changeable_set_with_definitions(
    client: TestClient,
) -> None:
    body = client.get("/api/v1/settings").json()
    assert set(body["settings"]) == {
        "queue.paused",
        "queue.draining",
        "routing.prefer_resident_bonus",
        "routing.min_present_weight",
        "routing.min_confidence",
        "routing.remote_cost_factor",
        "storage.content_retention_hours",
    }
    assert body["settings"]["storage.content_retention_hours"] == 24
    assert body["settings"]["routing.prefer_resident_bonus"] == 0.05
    definition = body["definitions"]["routing.prefer_resident_bonus"]
    assert definition["type"] == "float" and definition["maximum"] == 1.0
    assert definition["configured"] == 0.05
    assert "server.host" in body["config_only"] and "logging.include_content" in body["config_only"]
    assert "storage.retain_content" in body["config_only"]


def test_a_security_relevant_key_is_forbidden_by_name_and_an_unknown_one_is_invalid(
    client: TestClient,
) -> None:
    forbidden = client.put("/api/v1/settings", json={"server.host": "10.0.0.5"})
    assert forbidden.status_code == 403
    assert forbidden.json()["error"]["code"] == "FORBIDDEN"
    assert forbidden.json()["error"]["details"]["key"] == "server.host"
    assert "server.host" in forbidden.json()["error"]["message"]
    unknown = client.put("/api/v1/settings", json={"queue.max_depth": 5})
    assert unknown.status_code == 400
    error = unknown.json()["error"]
    assert error["code"] == "VALIDATION_ERROR"
    assert error["details"]["fields"][0]["path"] == "queue.max_depth"
    assert "routing.min_confidence" in error["details"]["runtime_changeable"]
    out_of_range = client.put("/api/v1/settings", json={"routing.prefer_resident_bonus": 2})
    assert out_of_range.status_code == 400
    wrong_type = client.put("/api/v1/settings", json={"queue.paused": "yes"})
    assert wrong_type.status_code == 400
    # Nothing was written by any refused call.
    assert (
        client.get("/api/v1/settings").json()["settings"]["routing.prefer_resident_bonus"] == 0.05
    )


def test_put_settings_takes_effect_in_the_running_process(client: TestClient) -> None:
    """The scheduler applies routing keys within a second; POST /route sees them at once."""
    changed = client.put(
        "/api/v1/settings",
        json={"routing.prefer_resident_bonus": 0.25, "storage.content_retention_hours": 1},
    )
    assert changed.status_code == 200, changed.text
    assert changed.json()["settings"]["routing.prefer_resident_bonus"] == 0.25
    assert changed.json()["settings"]["storage.content_retention_hours"] == 1
    runtime = client.app.state.queue_runtime  # type: ignore[attr-defined]  # FastAPI app
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and runtime.policy.prefer_resident_bonus != 0.25:
        time.sleep(0.05)
    assert runtime.policy.prefer_resident_bonus == 0.25
    assert runtime.content_retention_hours == 1
    # queue.paused through PUT /settings is the same flag the queue controls write.
    assert client.put("/api/v1/settings", json={"queue.paused": True}).status_code == 200
    assert client.get("/api/v1/queue").json()["flags"]["paused"] is True
    client.put("/api/v1/settings", json={"queue.paused": False})


def test_the_settings_page_renders_a_labelled_form_and_saves_behind_csrf(
    client: TestClient,
) -> None:
    page = client.get("/settings")
    assert page.status_code == 200
    assert 'for="setting-routing.prefer_resident_bonus"' in page.text
    assert 'for="setting-queue.paused"' in page.text
    assert "Config-only keys" in page.text and "server.allowed_hosts" in page.text
    token = page.text.split(f'name="{CSRF_FIELD_NAME}" value="')[1].split('"')[0]
    forged = client.post("/settings", data={CSRF_FIELD_NAME: "x", "routing.min_confidence": "0.2"})
    assert forged.status_code == 403
    saved = client.post(
        "/settings",
        data={
            CSRF_FIELD_NAME: token,
            "routing.min_confidence": "0.2",
            "routing.prefer_resident_bonus": "0.05",
            "routing.min_present_weight": "0.5",
            "routing.remote_cost_factor": "0.9",
            "storage.content_retention_hours": "48",
        },
        headers={"Cookie": f"{CSRF_COOKIE_NAME}={token}"},
        follow_redirects=False,
    )
    assert saved.status_code == 303 and saved.headers["location"] == "/settings?saved=1"
    body = client.get("/api/v1/settings").json()["settings"]
    assert body["routing.min_confidence"] == 0.2
    assert body["storage.content_retention_hours"] == 48
    assert body["queue.paused"] is False  # an unchecked box is false
    assert "Saved." in client.get("/settings?saved=1").text
