"""End-to-end: pause, resume and drain over HTTP against a server booted with zero configuration."""

from __future__ import annotations

import time
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


def _state(client: TestClient, job_id: str) -> str:
    return str(client.get(f"/api/v1/jobs/{job_id}").json()["state"])


def test_pause_holds_jobs_queued_and_resume_dispatches_them(client: TestClient) -> None:
    assert client.post("/api/v1/queue/pause").json()["paused"] is True
    job_id = client.post("/api/v1/jobs", json={"task": "general.chat", "prompt": "hello"}).json()[
        "job_id"
    ]
    time.sleep(1.5)  # longer than the idle poll: a running worker would have claimed it
    assert _state(client, job_id) == "queued"
    assert client.get("/api/v1/queue").json()["flags"]["paused"] is True
    assert client.post("/api/v1/queue/resume").json()["paused"] is False
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and _state(client, job_id) != "completed":
        time.sleep(0.05)
    assert _state(client, job_id) == "completed"


def test_drain_stops_claiming_and_reports_no_in_flight_work(client: TestClient) -> None:
    drained = client.post("/api/v1/queue/drain").json()
    assert drained == {"paused": False, "draining": True, "in_flight": 0}
    job_id = client.post("/api/v1/jobs", json={"task": "general.chat", "prompt": "hello"}).json()[
        "job_id"
    ]
    time.sleep(1.2)
    assert _state(client, job_id) == "queued"
    assert client.get("/api/v1/health").json()["status"] in {"ok", "degraded"}
    names = {c["name"] for c in client.get("/api/v1/health").json()["components"]}
    assert "queue" in names
    client.post("/api/v1/queue/resume")


def test_the_queue_page_has_working_controls_behind_csrf(client: TestClient) -> None:
    """P8 test 2: controls take effect; a forged form post is refused with CSRF_FAILED."""
    from mirrorwall import CSRF_COOKIE_NAME, CSRF_FIELD_NAME

    page = client.get("/queue")
    assert page.status_code == 200
    assert 'action="/queue/pause"' in page.text and 'id="queue-live"' in page.text
    assert 'data-stream-url="/api/v1/queue/stream"' in page.text
    assert 'aria-live="polite"' in page.text and "connecting…" in page.text  # loading state
    token = page.text.split(f'name="{CSRF_FIELD_NAME}" value="')[1].split('"')[0]
    cookie = page.headers["set-cookie"]
    assert cookie.startswith(f"{CSRF_COOKIE_NAME}={token}")
    assert "Secure" in cookie and "HttpOnly" in cookie and "Path=/" in cookie

    # The token in the field must equal the one in the cookie; the browser sends both.
    forged = client.post(
        "/queue/pause",
        data={CSRF_FIELD_NAME: "not-the-token"},
        headers={"Cookie": f"{CSRF_COOKIE_NAME}={token}"},
    )
    assert forged.status_code == 403
    assert forged.json()["error"]["code"] == "CSRF_FAILED"
    assert client.get("/api/v1/queue").json()["flags"]["paused"] is False

    missing = client.post("/queue/pause", data={CSRF_FIELD_NAME: token})  # no cookie
    assert missing.status_code == 403

    paused = client.post(
        "/queue/pause",
        data={CSRF_FIELD_NAME: token},
        headers={"Cookie": f"{CSRF_COOKIE_NAME}={token}"},
        follow_redirects=False,
    )
    assert paused.status_code == 303 and paused.headers["location"] == "/queue"
    assert client.get("/api/v1/queue").json()["flags"]["paused"] is True
    after = client.get("/queue", headers={"Cookie": f"{CSRF_COOKIE_NAME}={token}"})
    assert ">paused<" in after.text
    assert "set-cookie" not in after.headers  # the existing token is reused
    resumed = client.post(
        "/queue/resume",
        data={CSRF_FIELD_NAME: token},
        headers={"Cookie": f"{CSRF_COOKIE_NAME}={token}"},
        follow_redirects=False,
    )
    assert resumed.status_code == 303
    assert client.get("/api/v1/queue").json()["flags"]["paused"] is False
