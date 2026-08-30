"""End-to-end: the Jobs and Queue pages render from the same services the API uses."""

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


def test_jobs_page_lists_a_job_and_the_detail_page_shows_its_history(client: TestClient) -> None:
    assert "No job has been submitted yet" in client.get("/jobs").text
    job_id = client.post(
        "/api/v1/jobs", json={"task": "general.chat", "prompt": "<script>alert(1)</script>"}
    ).json()["job_id"]
    deadline = time.monotonic() + 10
    while (
        time.monotonic() < deadline
        and client.get(f"/api/v1/jobs/{job_id}").json()["state"] != "completed"
    ):
        time.sleep(0.05)
    listing = client.get("/jobs")
    assert listing.status_code == 200
    assert job_id in listing.text
    detail = client.get(f"/jobs/{job_id}")
    assert detail.status_code == 200
    assert "completed" in detail.text
    assert "job.queued" in detail.text and "job.completed" in detail.text
    assert f"/api/v1/jobs/{job_id}/explanation" in detail.text
    # Untrusted output is escaped, while the shell's own script stays.
    assert "<script>alert(1)</script>" not in detail.text
    assert client.get("/jobs/01NOPE0000000000000000000").status_code == 404


def test_queue_page_renders_the_report(client: TestClient) -> None:
    page = client.get("/queue")
    assert page.status_code == 200
    assert "Active jobs" in page.text
    assert "No job is executing" in page.text
    assert "Circuit breakers" in page.text
