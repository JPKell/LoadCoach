"""``POST /jobs/{id}/feedback`` (api.md §6): idempotent per ``(job, source)``, attributed by token.

The two tests dev-plan P7 names — idempotency per ``(job, source)`` and conflicting sources both
retained — live here, over HTTP, because the attribution rule is the whole point: a source the
caller cannot forge is what makes "idempotent per source" a guarantee rather than a convention.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from tests.integration.test_generate import NOW
from tests.integration.test_jobs_api import _client, _wait
from typer.testing import CliRunner

from loadcoach.cli.main import app as cli
from loadcoach.infrastructure.db.models import ApiToken, Feedback
from loadcoach.services.database import Database

runner = CliRunner()


def _completed_job(client: TestClient, *, source: str = "ideapress") -> str:
    response = client.post(
        "/api/v1/jobs",
        json={"task": "general.chat", "prompt": "hello"},
        headers={"X-Client-Name": source},
    )
    assert response.status_code == 202, response.text
    job_id = str(response.json()["job_id"])
    assert _wait(client, job_id)["state"] == "completed"
    return job_id


def _rows(client: TestClient, job_id: str) -> list[Feedback]:
    database: Database = client.app.state.database  # type: ignore[attr-defined]  # FastAPI app
    with database.read() as session:
        rows = session.execute(
            select(Feedback).where(Feedback.job_id == job_id).order_by(Feedback.source)
        ).scalars()
        return [
            Feedback(
                job_id=row.job_id,
                source=row.source,
                accepted=row.accepted,
                quality_score=row.quality_score,
                edited=row.edited,
                notes=row.notes,
            )
            for row in rows
        ]


def test_feedback_is_idempotent_per_job_and_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second call from the same source updates the record — one row, the latest verdict."""
    with _client(tmp_path, monkeypatch) as client:
        job_id = _completed_job(client)
        first = client.post(
            f"/api/v1/jobs/{job_id}/feedback",
            json={"source": "ideapress", "accepted": True, "quality_score": 0.8},
            headers={"X-Client-Name": "ideapress"},
        )
        assert first.status_code == 201, first.text
        body = first.json()
        assert body["created"] is True and body["source"] == "ideapress"
        assert body["accepted"] is True and body["quality_score"] == 0.8
        assert body["validation"] == {"passed": None, "detail": None}

        second = client.post(
            f"/api/v1/jobs/{job_id}/feedback",
            json={
                "accepted": False,
                "quality_score": 0.2,
                "edited": True,
                "validation": {"passed": False, "detail": {"missing": ["summary"]}},
                "notes": "changed my mind",
            },
            headers={"X-Client-Name": "ideapress"},
        )
        assert second.status_code == 200, second.text
        updated = second.json()
        assert updated["created"] is False
        assert updated["feedback_id"] == body["feedback_id"]
        assert updated["accepted"] is False and updated["edited"] is True
        assert updated["validation"] == {"passed": False, "detail": {"missing": ["summary"]}}
        assert updated["created_at"] == body["created_at"]
        assert updated["updated_at"] >= body["updated_at"]

        rows = _rows(client, job_id)
        assert len(rows) == 1
        assert rows[0].accepted is False and rows[0].quality_score == 0.2

        document = client.get(f"/api/v1/jobs/{job_id}").json()
        assert [item["source"] for item in document["feedback"]] == ["ideapress"]
        assert document["feedback"][0]["notes"] == "changed my mind"


def test_conflicting_sources_are_both_retained(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two callers may honestly disagree about one output; neither overwrites the other."""
    with _client(tmp_path, monkeypatch) as client:
        job_id = _completed_job(client)
        assert (
            client.post(
                f"/api/v1/jobs/{job_id}/feedback",
                json={"accepted": True, "quality_score": 0.9},
                headers={"X-Client-Name": "ideapress"},
            ).status_code
            == 201
        )
        assert (
            client.post(
                f"/api/v1/jobs/{job_id}/feedback",
                json={"accepted": False, "quality_score": 0.1, "notes": "unusable"},
                headers={"X-Client-Name": "reviewer"},
            ).status_code
            == 201
        )
        rows = _rows(client, job_id)
        assert [(row.source, row.accepted) for row in rows] == [
            ("ideapress", True),
            ("reviewer", False),
        ]
        document = client.get(f"/api/v1/jobs/{job_id}").json()
        assert {item["source"]: item["accepted"] for item in document["feedback"]} == {
            "ideapress": True,
            "reviewer": False,
        }


def test_feedback_source_is_the_tokens_name_when_a_token_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """api.md §6: the body's ``source`` is ignored when a token is present; ``write`` scope."""
    with _client(tmp_path, monkeypatch) as client:
        job_id = _completed_job(client)
        database: Database = client.app.state.database  # type: ignore[attr-defined]  # FastAPI app
        with database.write() as session:
            for name, raw, scope in (("reader", "read-token", "read"), ("writer", "wt", "write")):
                session.add(
                    ApiToken(
                        name=name,
                        token_sha256=hashlib.sha256(raw.encode()).hexdigest(),
                        scope=scope,
                        created_at=NOW,
                    )
                )
        payload: dict[str, Any] = {"source": "spoofed", "accepted": True}
        assert client.post(f"/api/v1/jobs/{job_id}/feedback", json=payload).status_code == 401
        forbidden = client.post(
            f"/api/v1/jobs/{job_id}/feedback",
            json=payload,
            headers={"Authorization": "Bearer read-token"},
        )
        assert forbidden.status_code == 403
        assert forbidden.json()["error"]["code"] == "FORBIDDEN"
        allowed = client.post(
            f"/api/v1/jobs/{job_id}/feedback",
            json=payload,
            headers={"Authorization": "Bearer wt", "X-Client-Name": "also-spoofed"},
        )
        assert allowed.status_code == 201, allowed.text
        assert allowed.json()["source"] == "writer"
        assert [row.source for row in _rows(client, job_id)] == ["writer"]


def test_feedback_source_falls_back_to_the_body_then_anonymous(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _client(tmp_path, monkeypatch) as client:
        job_id = _completed_job(client)
        from_body = client.post(
            f"/api/v1/jobs/{job_id}/feedback", json={"source": "ideapress", "accepted": True}
        )
        assert from_body.status_code == 201 and from_body.json()["source"] == "ideapress"
        anonymous = client.post(f"/api/v1/jobs/{job_id}/feedback", json={"accepted": False})
        assert anonymous.status_code == 201 and anonymous.json()["source"] == "anonymous"
        assert [row.source for row in _rows(client, job_id)] == ["anonymous", "ideapress"]


def test_feedback_on_an_unknown_job_is_404_and_a_bad_body_is_400(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _client(tmp_path, monkeypatch) as client:
        missing = client.post(
            "/api/v1/jobs/01NOSUCHJOB0000000000000000/feedback", json={"accepted": True}
        )
        assert missing.status_code == 404
        assert missing.json()["error"]["code"] == "JOB_NOT_FOUND"
        job_id = _completed_job(client)
        too_high = client.post(
            f"/api/v1/jobs/{job_id}/feedback", json={"accepted": True, "quality_score": 1.5}
        )
        assert too_high.status_code == 400
        assert too_high.json()["error"]["code"] == "VALIDATION_ERROR"
        assert too_high.json()["error"]["details"]["fields"][0]["path"] == "quality_score"
        no_verdict = client.post(f"/api/v1/jobs/{job_id}/feedback", json={"quality_score": 0.5})
        assert no_verdict.status_code == 400
        unknown_field = client.post(
            f"/api/v1/jobs/{job_id}/feedback", json={"accepted": True, "rating": 5}
        )
        assert unknown_field.status_code == 400
        assert _rows(client, job_id) == []


def test_the_cli_records_feedback_through_the_same_service(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``loadcoach job feedback`` (spec §7.2), idempotent per ``--source`` like the endpoint."""
    with _client(tmp_path, monkeypatch) as client:
        job_id = _completed_job(client)
        first = runner.invoke(cli, ["job", "feedback", job_id, "--accepted", "--quality", "0.7"])
        assert first.exit_code == 0, first.output
        assert "feedback recorded" in first.stdout and "'cli'" in first.stdout
        second = runner.invoke(
            cli, ["job", "feedback", job_id, "--rejected", "--edited", "--notes", "meh", "--json"]
        )
        assert second.exit_code == 0, second.output
        import json

        record = json.loads(second.stdout)
        assert record["created"] is False and record["accepted"] is False
        assert record["edited"] is True and record["notes"] == "meh"
        assert [(row.source, row.accepted) for row in _rows(client, job_id)] == [("cli", False)]
        missing = runner.invoke(
            cli, ["job", "feedback", "01NOSUCHJOB0000000000000000", "--accepted"]
        )
        assert missing.exit_code == 5
        out_of_range = runner.invoke(
            cli, ["job", "feedback", job_id, "--accepted", "--quality", "2"]
        )
        assert out_of_range.exit_code != 0
