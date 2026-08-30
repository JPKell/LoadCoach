"""Content retention (spec §14, data model §3, P5-15): text goes, hashes and history stay."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest
from sqlalchemy import select
from tests.integration.test_jobs_api import _client, _wait

from loadcoach.infrastructure.db.models import Job, JobEvent
from loadcoach.services.database import Database
from loadcoach.services.retention import SCRUBBED_MARKER, scrub_content


def _job(database: Database, job_id: str) -> Job:
    with database.read() as session:
        job = session.get(Job, job_id)
        assert job is not None
        session.expunge(job)
        return job


def test_finished_jobs_lose_their_text_after_the_retention_and_keep_everything_else(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _client(tmp_path, monkeypatch) as client:
        job_id = str(
            client.post("/api/v1/jobs", json={"task": "general.chat", "prompt": "keep me"}).json()[
                "job_id"
            ]
        )
        assert _wait(client, job_id)["state"] == "completed"
        queued = str(
            client.post("/api/v1/queue/pause").status_code
            and client.post(
                "/api/v1/jobs", json={"task": "general.chat", "prompt": "waiting"}
            ).json()["job_id"]
        )
        database: Database = client.app.state.database  # type: ignore[attr-defined]  # FastAPI
        before = _job(database, job_id)
        assert before.response_text == "the answer" and before.response_hash
        assert isinstance(before.request_json, dict) and "messages" in before.request_json

        # Not old enough: nothing happens.
        untouched = scrub_content(database, now=datetime.now(UTC), retention_hours=24)
        assert untouched.scrubbed_jobs == 0
        assert _job(database, job_id).response_text == "the answer"

        far_future = datetime.now(UTC) + timedelta(hours=25)
        outcome = scrub_content(database, now=far_future, retention_hours=24)
        assert outcome.scrubbed_jobs == 1 and outcome.scrubbed_events >= 1
        after = _job(database, job_id)
        assert after.response_text is None and after.prompt_text is None
        assert after.response_hash == before.response_hash  # hashes always stored
        assert after.prompt_hash == before.prompt_hash
        assert after.output_tokens == before.output_tokens and after.total_ms == before.total_ms
        assert after.selected_model_id == before.selected_model_id and after.state == "completed"
        assert isinstance(after.request_json, dict)
        assert "messages" not in after.request_json and after.request_json["task"] == "general.chat"
        assert SCRUBBED_MARKER in after.request_json
        with database.read() as session:
            results = (
                session.execute(
                    select(JobEvent).where(
                        JobEvent.job_id == job_id, JobEvent.event_type == "job.completed"
                    )
                )
                .scalars()
                .all()
            )
        payloads = [dict(cast("dict[str, Any]", e.data_json or {})) for e in results]
        assert payloads and all("output" not in data for data in payloads)
        assert all(SCRUBBED_MARKER in data for data in payloads)
        # A queued job keeps its transcript until it has run, however old the cutoff.
        waiting = _job(database, queued)
        assert isinstance(waiting.request_json, dict) and "messages" in waiting.request_json
        # Idempotent: the second sweep finds nothing to do.
        assert scrub_content(database, now=far_future, retention_hours=24).scrubbed_jobs == 0
        document = client.get(f"/api/v1/jobs/{job_id}").json()
        assert document["output"]["text"] is None
        # F9 (M5C-9): retention's docstring promises the page and the API can say "content
        # removed by retention" rather than showing nothing — now they do.
        assert document["retention"]["content_scrubbed_at"] is not None
        page = client.get(f"/jobs/{job_id}", headers={"Accept": "text/html"}).text
        assert "Content removed by retention" in page
        assert "A database backup taken" in page and "keeps the text" in page
        fresh = client.get(f"/api/v1/jobs/{queued}").json()
        assert fresh["retention"]["content_scrubbed_at"] is None
        client.post("/api/v1/queue/resume")


def test_the_scheduler_runs_the_sweep_with_the_runtime_retention_and_retain_content_disables_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _client(tmp_path, monkeypatch) as client:
        job_id = str(
            client.post("/api/v1/jobs", json={"task": "general.chat", "prompt": "x"}).json()[
                "job_id"
            ]
        )
        assert _wait(client, job_id)["state"] == "completed"
        runtime = client.app.state.queue_runtime  # type: ignore[attr-defined]  # FastAPI
        database: Database = client.app.state.database  # type: ignore[attr-defined]  # FastAPI
        assert (
            client.put("/api/v1/settings", json={"storage.content_retention_hours": 0}).status_code
            == 200
        )
        runtime.apply_runtime_settings()
        assert runtime.content_retention_hours == 0
        runtime.sweep_retention(datetime.now(UTC) + timedelta(seconds=1))
        assert _job(database, job_id).response_text is None

    with _client(
        tmp_path, monkeypatch, env={"LOADCOACH_STORAGE__RETAIN_CONTENT": "true"}
    ) as client:
        job_id = str(
            client.post("/api/v1/jobs", json={"task": "general.chat", "prompt": "y"}).json()[
                "job_id"
            ]
        )
        assert _wait(client, job_id)["state"] == "completed"
        runtime = client.app.state.queue_runtime  # type: ignore[attr-defined]  # FastAPI
        database = client.app.state.database  # type: ignore[attr-defined]  # FastAPI
        runtime.content_retention_hours = 0
        runtime.sweep_retention(datetime.now(UTC) + timedelta(days=1))
        assert _job(database, job_id).response_text == "the answer"
        refused = client.put("/api/v1/settings", json={"storage.retain_content": False})
        assert refused.status_code == 403
