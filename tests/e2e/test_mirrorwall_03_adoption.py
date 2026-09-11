"""Row WM2: the MirrorWall 0.3 surfaces this application opted into (design brief §6).

Each is a diff a person can see in the page: the tab strip only when ``[console] url`` is set,
the status dot on the System page, dense list tables, inline meters in the telemetry bar, and
the job page's log pane fed by ``/jobs/{id}/log`` — the same event source as the API stream,
rendered as ``log`` frames and closed with ``log.closed``.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from loadcoach.bootstrap import bootstrap

CONSOLE = "https://jordan-main.local:8769"


def _client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, console_url: str = ""
) -> TestClient:
    """A booted application over an isolated XDG tree — `bootstrap()` migrates the database the
    way `loadcoach serve` does; `create_app(settings)` alone would not."""
    for name in list(os.environ):
        if name.startswith("LOADCOACH_"):
            monkeypatch.delenv(name, raising=False)
    for name, directory in (("CONFIG", "config"), ("DATA", "data"), ("STATE", "state")):
        (tmp_path / directory).mkdir(exist_ok=True)
        monkeypatch.setenv(f"XDG_{name}_HOME", str(tmp_path / directory))
    monkeypatch.setenv("LOADCOACH_PROVIDER__KIND", "fake")
    if console_url:
        monkeypatch.setenv("LOADCOACH_CONSOLE__URL", console_url)
    return TestClient(bootstrap().app, base_url="http://127.0.0.1")


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    with _client(tmp_path, monkeypatch) as test_client:
        yield test_client


def test_no_tab_strip_without_a_console_url(client: TestClient) -> None:
    page = client.get("/system").text
    assert 'class="app-tabs"' not in page and 'class="app-tab"' not in page


def test_the_tab_strip_links_the_console_and_the_peers_through_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _client(tmp_path, monkeypatch, console_url=CONSOLE + "/") as client:
        page = client.get("/system").text
    assert f'<a href="{CONSOLE}" class="app-tab">' in page
    assert '<a href="/" class="app-tab" aria-current="page">' in page
    for peer in ("freeweight", "ideapress", "promptcadence"):
        assert f'<a href="{CONSOLE}/apps/{peer}" class="app-tab">' in page


def test_the_system_page_shows_a_status_dot_per_health_component(client: TestClient) -> None:
    page = client.get("/system").text
    components = client.get("/api/v1/health").json()["components"]
    assert page.count('class="status-dot"') == len(components)


def test_the_list_tables_are_dense_and_the_bar_carries_meters(client: TestClient) -> None:
    client.post("/api/v1/jobs", json={"task": "general.chat", "prompt": "hello"})
    for path in ("/jobs", "/models"):
        page = client.get(path).text
        assert 'data-density="dense"' in page or "<table" not in page, path
    for group in ("cpu", "ram", "gpu", "vram"):
        assert f'data-meter="{group}"' in client.get("/system").text


def test_the_job_page_has_a_log_pane_fed_by_the_jobs_own_events(client: TestClient) -> None:
    job_id = client.post("/api/v1/jobs", json={"task": "general.chat", "prompt": "hello"}).json()[
        "job_id"
    ]
    page = client.get(f"/jobs/{job_id}").text
    assert f'sse-connect="/jobs/{job_id}/log"' in page and 'sse-close="log.closed"' in page
    assert "vendor/htmx/htmx.min.js" in page
    assert "vendor/htmx" not in client.get("/jobs").text

    body = ""
    with client.stream("GET", f"/jobs/{job_id}/log") as response:
        assert response.headers["content-type"].startswith("text/event-stream")
        for chunk in response.iter_text():
            body += chunk
            if "event: log.closed" in body:
                break
    assert 'event: log\ndata: <div class="log-pane-line" data-level=' in body
    assert "event: token" not in body
    assert body.rstrip().endswith("event: log.closed\ndata: {}")
