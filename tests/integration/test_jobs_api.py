"""The ``/jobs`` and ``/queue`` API over HTTP, with the real runtime the lifespan starts.

Not in the Phase 5 file list verbatim (its integration files are queue, recovery and
cancellation); it exists because api.md §5 and §8 are a surface with a shape, and a shape is
tested over HTTP or not at all.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from modelrack.testing import FakeGeneration, FakeProvider, FakeScript
from tests.integration.test_generate import NOW, _model
from tests.integration.test_streaming import _frames

from loadcoach.config import load_settings
from loadcoach.services.database import Database, ensure_ready
from loadcoach.services.models import discover_models
from loadcoach.services.task_profiles import import_task_profiles, read_task_profiles_file
from loadcoach.web.app import create_app

TERMINAL = {"completed", "failed", "cancelled"}


@contextmanager
def _client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, env: dict[str, str] | None = None
) -> Iterator[TestClient]:
    """An entered TestClient whose workers run against a scripted fake provider."""
    url = f"sqlite:///{tmp_path / 'jobs.sqlite3'}"
    monkeypatch.setenv("LOADCOACH_STORAGE__DATABASE_URL", url)
    monkeypatch.setenv("LOADCOACH_PROVIDER__KIND", "fake")
    for key, value in (env or {}).items():
        monkeypatch.setenv(key, value)
    settings = load_settings().settings
    script = FakeScript(
        models=(_model(),),
        generations=(FakeGeneration(text="the answer"),),
        repeat_final_generation=True,
    )
    database = Database.from_url(url)
    ensure_ready(database, auto_migrate=True)
    import_task_profiles(database, read_task_profiles_file(), now=NOW)
    discover_models(database, FakeProvider(script), now=NOW)
    database.close()

    app = create_app(settings)
    with TestClient(app, base_url="http://localhost") as client:
        provider = FakeProvider(script)
        app.state.provider = provider
        app.state.queue_runtime.replace_provider(provider)
        yield client


def _wait(client: TestClient, job_id: str, *, timeout: float = 10.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    document: dict[str, Any] = {}
    while time.monotonic() < deadline:
        document = dict(client.get(f"/api/v1/jobs/{job_id}").json())
        if document["state"] in TERMINAL:
            return document
        time.sleep(0.02)
    pytest.fail(f"job {job_id} did not finish: {document.get('state')}")


def test_submit_returns_202_and_the_job_runs_to_completion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _client(tmp_path, monkeypatch) as client:
        response = client.post(
            "/api/v1/jobs",
            json={"task": "general.chat", "prompt": "hello", "class": "background"},
            headers={"X-Client-Name": "ideapress"},
        )
        assert response.status_code == 202, response.text
        assert response.headers["X-Idempotent-Replay"] == "false"
        submitted = response.json()
        assert submitted["state"] == "queued" and submitted["class"] == "background"
        assert submitted["source"] == "ideapress"
        assert submitted["priority"] == {"base": 100, "effective": 100}
        document = _wait(client, submitted["job_id"])
        assert document["state"] == "completed"
        assert document["output"]["text"] == "the answer"
        assert document["model"]["canonical_id"].startswith("fake/")
        assert [a["outcome"] for a in document["attempts"]] == ["completed"]
        assert document["routing"]["decision_id"]
        assert document["timing"]["queue_wait_ms"] is not None
        assert document["routing"]["explanation_url"].endswith("/explanation")

        with client.stream("GET", f"/api/v1/jobs/{submitted['job_id']}/stream") as stream:
            assert stream.status_code == 200
            frames = _frames("".join(stream.iter_text()))
        events = [event for _, event, _ in frames]
        assert events[0] == "job.queued" and events[-1] == "job.completed"
        assert "job.leased" in events and "job.executing" in events
        sequences = [int(i) for i, _, _ in frames if i]
        assert sequences == sorted(sequences) and len(set(sequences)) == len(sequences)


def test_a_repeated_key_returns_the_original_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _client(tmp_path, monkeypatch) as client:
        body = {"task": "general.chat", "prompt": "hello", "idempotency_key": "K1"}
        first = client.post("/api/v1/jobs", json=body)
        again = client.post("/api/v1/jobs", json=body)
        other = client.post("/api/v1/jobs", json=body, headers={"X-Client-Name": "other"})
        assert first.status_code == again.status_code == other.status_code == 202
        assert first.json()["job_id"] == again.json()["job_id"]
        assert again.headers["X-Idempotent-Replay"] == "true"
        assert other.json()["job_id"] != first.json()["job_id"]


def test_list_filters_and_paginates_with_an_opaque_cursor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _client(tmp_path, monkeypatch) as client:
        assert client.post("/api/v1/queue/pause").status_code == 202
        ids = [
            client.post(
                "/api/v1/jobs",
                json={
                    "task": "general.chat",
                    "prompt": f"j{i}",
                    "class": "batch" if i else "normal",
                },
            ).json()["job_id"]
            for i in range(3)
        ]
        page = client.get("/api/v1/jobs", params={"limit": 2}).json()
        assert [item["job_id"] for item in page["items"]] == [ids[2], ids[1]]
        assert page["page"]["has_more"] is True and page["page"]["limit"] == 2
        rest = client.get(
            "/api/v1/jobs", params={"limit": 2, "cursor": page["page"]["next_cursor"]}
        ).json()
        assert [item["job_id"] for item in rest["items"]] == [ids[0]]
        assert rest["page"]["has_more"] is False and rest["page"]["next_cursor"] is None
        batch = client.get("/api/v1/jobs", params={"class": "batch"}).json()
        assert {item["job_id"] for item in batch["items"]} == {ids[1], ids[2]}
        queued = client.get("/api/v1/jobs", params={"state": "queued"}).json()
        assert len(queued["items"]) == 3
        assert client.get("/api/v1/jobs", params={"state": "completed"}).json()["items"] == []
        assert client.post("/api/v1/queue/resume").status_code == 202


def test_cancel_a_queued_job_and_refuse_a_terminal_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _client(tmp_path, monkeypatch) as client:
        client.post("/api/v1/queue/pause")
        job_id = client.post("/api/v1/jobs", json={"task": "general.chat", "prompt": "x"}).json()[
            "job_id"
        ]
        cancelled = client.post(f"/api/v1/jobs/{job_id}/cancel")
        assert cancelled.status_code == 202
        assert cancelled.json() == {"job_id": job_id, "state": "cancelled", "already": False}
        refused = client.post(f"/api/v1/jobs/{job_id}/cancel")
        assert refused.status_code == 409
        assert refused.json()["error"]["code"] == "JOB_NOT_CANCELLABLE"
        missing = client.post("/api/v1/jobs/01NOPE0000000000000000000/cancel")
        assert missing.status_code == 404
        assert missing.json()["error"]["code"] == "JOB_NOT_FOUND"
        client.post("/api/v1/queue/resume")


def test_the_explanation_is_a_lookup_of_the_decision_that_routed_the_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _client(tmp_path, monkeypatch) as client:
        job_id = client.post("/api/v1/jobs", json={"task": "general.chat", "prompt": "x"}).json()[
            "job_id"
        ]
        document = _wait(client, job_id)
        explanation = client.get(f"/api/v1/jobs/{job_id}/explanation")
        assert explanation.status_code == 200
        payload = explanation.json()
        assert payload["decision_id"] == document["routing"]["decision_id"]
        assert payload["selected"]["canonical_id"] == document["model"]["canonical_id"]
        # The same document, from the decision's own endpoint: a lookup, never a copy.
        direct = client.get(f"/api/v1/routing-decisions/{payload['decision_id']}").json()
        assert direct == payload
        assert client.get("/api/v1/jobs/01NOPE0000000000000000000/explanation").status_code == 404
        assert client.get("/api/v1/jobs/01NOPE0000000000000000000").status_code == 404
        assert client.get("/api/v1/jobs/01NOPE0000000000000000000/stream").status_code == 404


def test_queue_full_is_429_with_the_numbers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _client(tmp_path, monkeypatch, env={"LOADCOACH_QUEUE__MAX_DEPTH": "1"}) as client:
        client.post("/api/v1/queue/pause")
        assert (
            client.post("/api/v1/jobs", json={"task": "general.chat", "prompt": "a"}).status_code
            == 202
        )
        full = client.post("/api/v1/jobs", json={"task": "general.chat", "prompt": "b"})
        assert full.status_code == 429
        assert full.json()["error"]["code"] == "QUEUE_FULL"
        assert full.json()["error"]["details"] == {"active": 1, "max_depth": 1}
        client.post("/api/v1/queue/resume")


def test_queue_and_system_status_report_the_documented_shape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _client(tmp_path, monkeypatch) as client:
        job_id = client.post("/api/v1/jobs", json={"task": "general.chat", "prompt": "x"}).json()[
            "job_id"
        ]
        _wait(client, job_id)
        queue = client.get("/api/v1/queue").json()
        assert set(queue) >= {
            "depth_by_state",
            "depth_by_class",
            "oldest_queued_age_seconds",
            "starving",
            "dispatch_latency_ms",
            "executions",
            "residency",
            "circuit_breakers",
            "throughput",
            "flags",
            "last_recovery",
        }
        assert queue["flags"] == {"paused": False, "draining": False}
        assert queue["dispatch_latency_ms"]["samples"] >= 1
        assert queue["throughput"]["completed_last_5m"] >= 1
        assert queue["last_recovery"]["touched"] == 0
        status = client.get("/api/v1/system/status").json()
        assert "telemetry" in status and status["starving"] == 0
        drained = client.post("/api/v1/queue/drain").json()
        assert drained["draining"] is True and drained["in_flight"] == 0
        assert client.post("/api/v1/queue/resume").json()["draining"] is False


def test_a_stream_replays_persisted_events_after_last_event_id_and_closes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _client(tmp_path, monkeypatch) as client:
        job_id = client.post("/api/v1/jobs", json={"task": "general.chat", "prompt": "x"}).json()[
            "job_id"
        ]
        _wait(client, job_id)
        with client.stream(
            "GET", f"/api/v1/jobs/{job_id}/stream", headers={"Last-Event-ID": "2"}
        ) as stream:
            frames = _frames("".join(stream.iter_text()))
        sequences = [int(i) for i, _, _ in frames if i]
        assert sequences and min(sequences) == 3
        assert frames[-1][1] == "job.completed"


def test_post_generate_with_a_repeated_key_returns_the_original_job_without_re_executing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """api.md §4, now durable: the key is found in the table, not in a process-local registry."""
    with _client(tmp_path, monkeypatch) as client:
        body = {"task": "general.chat", "prompt": "hello", "idempotency_key": "SYNC1"}
        first = client.post("/api/v1/generate", json=body)
        again = client.post("/api/v1/generate", json=body)
        assert first.status_code == again.status_code == 200
        assert first.json()["job_id"] == again.json()["job_id"]
        assert again.json()["output"]["text"] == first.json()["output"]["text"]
        listed = client.get("/api/v1/jobs", params={"limit": 10}).json()["items"]
        assert [item["job_id"] for item in listed] == [first.json()["job_id"]]
        assert listed[0]["state_reason"] == "synchronous"
        assert len(listed[0]["attempts"]) == 1
        assert json.dumps(listed[0])  # the document is JSON-serializable throughout
