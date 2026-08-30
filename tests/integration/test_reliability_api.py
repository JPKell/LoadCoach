"""``GET /reliability``, the Reliability page, the ``reliability`` health component and the CLI.

P7 acceptance criteria 2 and 3: feedback is reflected in ``GET /reliability``; regression
warnings appear in health. Every statistic the API returns carries its sample count and, when
absent, the reason (ADR-0016), and the page renders an absent value as an em dash carrying that
reason — never as a number.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from tests.integration.test_feedback_affects_routing import _attempts
from tests.integration.test_jobs_api import _client, _wait
from typer.testing import CliRunner

from loadcoach.cli.main import app as cli
from loadcoach.infrastructure.db.models import Model
from loadcoach.services.database import Database
from loadcoach.services.reliability import recompute_pair

runner = CliRunner()
TASK = "general.chat"


def _model_id(client: TestClient) -> str:
    database: Database = client.app.state.database  # type: ignore[attr-defined]  # FastAPI app
    with database.read() as session:
        return session.execute(select(Model.id)).scalar_one()


def test_reliability_is_empty_and_healthy_before_any_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _client(tmp_path, monkeypatch) as client:
        response = client.get("/api/v1/reliability")
        assert response.status_code == 200
        body = response.json()
        assert body["reliability"] == [] and body["regressions"] == []
        assert body["windows"] == ["7d", "30d", "all"]
        assert body["minimums"]["factor_attempts"] == 20
        page = client.get("/reliability")
        assert page.status_code == 200 and "No production evidence yet" in page.text
        health = client.get("/api/v1/health").json()
        component = next(c for c in health["components"] if c["name"] == "reliability")
        assert component == {
            "name": "reliability",
            "status": "ok",
            "detail": "no production evidence yet",
        }


def test_feedback_is_reflected_in_get_reliability_with_bounded_statistics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P7 AC2, with ``source: ideapress`` because IdeaPress itself cannot send feedback yet."""
    with _client(tmp_path, monkeypatch) as client:
        job_ids = []
        for _ in range(3):
            submitted = client.post("/api/v1/jobs", json={"task": TASK, "prompt": "hello"})
            job_ids.append(str(submitted.json()["job_id"]))
            assert _wait(client, job_ids[-1])["state"] == "completed"
        for job_id, accepted in zip(job_ids, (True, False, True), strict=True):
            assert (
                client.post(
                    f"/api/v1/jobs/{job_id}/feedback",
                    json={"source": "ideapress", "accepted": accepted, "quality_score": 0.5},
                ).status_code
                == 201
            )
        body = client.get("/api/v1/reliability").json()
        assert len(body["reliability"]) == 1
        entry = body["reliability"][0]
        assert entry["task_profile_id"] == TASK
        week = entry["windows"]["7d"]
        assert week["attempts"] == 3 and week["successes"] == 3 and week["validation_passes"] == 3
        # Three verdicts is below the rate bound: absent, with the count and the reason.
        assert week["acceptance_rate"] == {
            "value": None,
            "samples": 3,
            "minimum": 5,
            "reason": "3 sample(s); 5 needed",
        }
        assert week["success_rate"]["value"] is None and week["success_rate"]["samples"] == 3
        assert week["p95_latency_ms"]["reason"] == "3 sample(s); 20 needed"
        assert entry["factor"]["neutral"] is True and entry["factor"]["value"] == 1.0
        assert entry["regression"]["status"] == "insufficient_samples"
        assert entry["circuit_breaker"]["state"] == "closed"
        assert body["regressions"] == []

        filtered = client.get("/api/v1/reliability", params={"task": "code.review"}).json()
        assert filtered["reliability"] == []
        by_model = client.get(
            "/api/v1/reliability", params={"model": entry["model"]["canonical_id"]}
        ).json()
        assert len(by_model["reliability"]) == 1

        page = client.get("/reliability")
        assert page.status_code == 200
        assert entry["model"]["canonical_id"] in page.text
        assert 'title="3 sample(s); 5 needed"' in page.text  # the dash carries its reason
        assert 'aria-label="Unavailable: 3 sample(s); 5 needed"' in page.text
        assert "not evaluated" in page.text


def test_a_regression_appears_in_health_and_on_the_page(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P7 AC3: a synthetic degradation against the model's own baseline is named in ``/health``."""
    with _client(tmp_path, monkeypatch) as client:
        database: Database = client.app.state.database  # type: ignore[attr-defined]  # FastAPI app
        model_id = _model_id(client)
        now = datetime.now(UTC)
        # Baseline: 100 attempts three weeks ago, 90 validated. Recent: 30 attempts, 18 validated.
        _attempts(
            database,
            model_id=model_id,
            outcomes=["completed"] * 90 + ["validation_failed"] * 10,
            start=now - timedelta(days=21),
        )
        _attempts(
            database,
            model_id=model_id,
            outcomes=["completed"] * 18 + ["validation_failed"] * 12,
            start=now - timedelta(hours=2),
        )
        recompute_pair(database, model_id=model_id, task_profile_id=TASK, now=now)

        health = client.get("/api/v1/health")
        assert health.status_code == 200  # degraded, not unavailable
        component = next(c for c in health.json()["components"] if c["name"] == "reliability")
        assert component["status"] == "degraded"
        assert TASK in component["detail"] and "regression:" in component["detail"]
        assert "60% over 30 recent attempts vs 90% over 100 before" in component["detail"]

        body = client.get("/api/v1/reliability").json()
        assert len(body["regressions"]) == 1
        entry = body["reliability"][0]
        assert entry["regression"]["status"] == "regressed"
        assert entry["regression"]["drop"] == pytest.approx(0.3)
        assert entry["regression"]["z_score"] == pytest.approx(3.8435, abs=0.001)
        assert entry["factor"]["window"] == "7d" and entry["factor"]["attempts"] == 30
        assert entry["factor"]["value"] == pytest.approx(0.5 + 0.5 * 0.6)

        page = client.get("/reliability")
        assert ">regression<" in page.text and "Regressions" in page.text
        assert "60% over 30 recent attempts" in page.text

        shown = runner.invoke(cli, ["reliability", "show"])
        assert shown.exit_code == 0, shown.output
        assert "trend: regression:" in shown.stdout and "factor 0.800" in shown.stdout
        as_json = runner.invoke(cli, ["reliability", "show", "--json", "--task", TASK])
        assert as_json.exit_code == 0
        import json

        assert json.loads(as_json.stdout)["reliability"][0]["regression"]["status"] == "regressed"
        assert (
            "no production evidence yet"
            in runner.invoke(cli, ["reliability", "show", "--task", "code.review"]).stdout
        )
