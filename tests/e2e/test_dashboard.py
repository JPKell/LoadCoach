"""End-to-end: the Dashboard, the telemetry bar on every route, and the HTML error state (P8).

UI standards §6: every view has empty, populated and error states. The dashboard's empty state
is a fresh server; its populated state is one job later; its degraded state is a paused queue.
The error state is any page that fails, rendered as a page — code, request ID, what to do next —
not as a JSON envelope a person cannot read.
"""

from __future__ import annotations

import time
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from loadcoach.bootstrap import bootstrap
from loadcoach.web.rendering import NAV_ITEMS


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("LOADCOACH_PROVIDER__KIND", "fake")
    application = bootstrap()
    with TestClient(application.app, base_url="http://localhost") as test_client:
        yield test_client


def _run_job(client: TestClient) -> str:
    job_id = str(
        client.post("/api/v1/jobs", json={"task": "general.chat", "prompt": "hello"}).json()[
            "job_id"
        ]
    )
    deadline = time.monotonic() + 10
    while (
        time.monotonic() < deadline
        and client.get(f"/api/v1/jobs/{job_id}").json()["state"] != "completed"
    ):
        time.sleep(0.05)
    assert client.get(f"/api/v1/jobs/{job_id}").json()["state"] == "completed"
    return job_id


def test_dashboard_empty_state_then_populated(client: TestClient) -> None:
    empty = client.get("/")
    assert empty.status_code == 200 and "text/html" in empty.headers["content-type"]
    assert "No routing decision has been made yet" in empty.text
    assert "No job has been submitted yet" in empty.text
    assert "No job has been routed to a model" in empty.text
    assert "Nothing is degraded" in empty.text
    assert 'aria-current="page"' in empty.text  # the nav marks the dashboard

    job_id = _run_job(client)
    populated = client.get("/")
    assert f'href="/jobs/{job_id}"' in populated.text  # drillable to the record
    assert 'href="/routing/' in populated.text
    assert "fake/" in populated.text  # the model mix names the model
    assert "No job has been submitted yet" not in populated.text
    document = client.get(f"/api/v1/jobs/{job_id}").json()
    assert f'href="/routing/{document["routing"]["decision_id"]}"' in populated.text


def test_dashboard_lists_degradations_with_a_link_each(client: TestClient) -> None:
    assert client.post("/api/v1/queue/pause").status_code == 202
    try:
        page = client.get("/")
        assert "Nothing is degraded" not in page.text
        assert "dispatch is paused" in page.text
        assert 'href="/queue"' in page.text
        assert ">paused<" in page.text
    finally:
        client.post("/api/v1/queue/resume")


def test_the_telemetry_bar_is_on_every_route_and_its_stream_reports_a_sample(
    client: TestClient,
) -> None:
    """UI standards §3 and §13: telemetry on every page, em dashes before the first sample."""
    for item in NAV_ITEMS:
        page = client.get(item["href"])
        assert page.status_code == 200, item
        assert 'id="mw-telemetry-bar"' in page.text, item
        assert 'data-telemetry-url="/api/v1/system/telemetry/stream"' in page.text, item
        assert 'href="#content"' in page.text  # skip link, every page
    # The stream is open-ended, and a test client cannot interrupt a sync generator running in a
    # threadpool, so the first frame is pulled from the response body directly.
    from collections.abc import AsyncGenerator
    from typing import cast

    import anyio
    from starlette.requests import Request

    from loadcoach.domain.authorization import Principal
    from loadcoach.web.routes.system import telemetry_stream

    async def first_frame() -> tuple[str, str]:
        scope = {
            "type": "http",
            "method": "GET",
            "path": "/api/v1/system/telemetry/stream",
            "headers": [],
            "query_string": b"",
            "app": client.app,
        }
        loopback = Principal(name="loopback", scope="admin", source="loopback")
        response = await telemetry_stream(Request(scope), principal=loopback)
        iterator = cast("AsyncGenerator[bytes | str, None]", response.body_iterator)
        chunk: bytes | str = b""
        async for chunk in iterator:  # noqa: B007 — the first chunk is the whole first frame
            break
        await iterator.aclose()
        text = chunk.decode() if isinstance(chunk, bytes) else chunk
        return str(response.media_type), text

    media_type, frame = anyio.run(first_frame)
    assert media_type == "text/event-stream"
    assert "event: telemetry.sampled" in frame
    assert '"vram_total_bytes"' in frame and '"ram_available_bytes"' in frame
    assert '"unavailable_reasons"' in frame


def test_a_failing_page_renders_the_error_state_not_a_json_envelope(client: TestClient) -> None:
    """UI standards §6: what failed, why, the code, the request ID, what to do next."""
    page = client.get("/jobs/01NOPE0000000000000000000", headers={"Accept": "text/html"})
    assert page.status_code == 404
    assert "text/html" in page.headers["content-type"]
    assert "JOB_NOT_FOUND" in page.text
    assert page.headers["X-Request-ID"] in page.text
    assert "Check the identifier" in page.text
    assert 'role="alert"' in page.text
    # The API keeps its envelope, whatever the client accepts.
    api = client.get("/api/v1/jobs/01NOPE0000000000000000000", headers={"Accept": "text/html"})
    assert api.status_code == 404 and api.json()["error"]["code"] == "JOB_NOT_FOUND"
    # An unknown page is the same state.
    missing = client.get("/no-such-page", headers={"Accept": "text/html"})
    assert missing.status_code == 404 and "NOT_FOUND" in missing.text
