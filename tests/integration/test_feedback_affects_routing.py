"""Production evidence changes routing, visibly (dev-plan P7; routing §6, §11; queue §7).

The five tests dev-plan P7 names live here or in ``test_feedback_api.py``: the factor neutral below
the minimum and then applied; a failing model deprioritized, excluded and re-probed — the whole
cycle, with the reason readable at every step; incremental recomputation equal to a full one, as a
property over random attempts; and production evidence never overwriting benchmark evidence, both
visible in one explanation with their own labels.
"""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import pytest
from baseaicore import new_id
from modelrack.testing import FakeProvider, FakeScript
from sqlalchemy import select
from tests.integration.test_evidence_routing_change import (
    MACHINE,
    _bundle,
    _capability,
    _database,
    _facts,
    _identity,
    _model,
    _record,
    _snapshot,
    _template,
)
from tests.integration.test_jobs_api import _client, _wait

from loadcoach.domain.circuit_breaker import BreakerState, CircuitBreakers
from loadcoach.domain.reliability import PRODUCTION_MINIMUM_SAMPLES, WINDOWS
from loadcoach.infrastructure.db.models import (
    CapabilityEvidence,
    Job,
    JobAttempt,
    Model,
    ReliabilityStat,
)
from loadcoach.services.evidence import import_bundle
from loadcoach.services.feedback import FeedbackSubmission, record_feedback
from loadcoach.services.reliability import (
    breaker_samples,
    recompute_all,
    recompute_pair,
    stats_for,
)
from loadcoach.services.routing import RouteRequest, RoutingPolicy, load_task_profile, route

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from pathlib import Path

    from loadcoach.services.database import Database

T = datetime(2026, 8, 29, 12, 0, tzinfo=UTC)
TASK = "general.chat"


# ------------------------------------------------------------------------------------ helpers


def _seed(tmp_path: Path) -> tuple[Database, dict[str, str]]:
    """Two identical 8B models, ``alpha`` and ``beta``: routing ties, so only evidence decides."""
    provider = FakeProvider(
        FakeScript(models=(_model("alpha:8b", "a" * 64), _model("beta:8b", "b" * 64)))
    )
    database = _database(tmp_path, provider)
    with database.read() as session:
        ids = {
            row.provider_model_name.split(":")[0]: row.id
            for row in session.execute(select(Model)).scalars()
        }
    return database, ids


def _attempt(
    database: Database,
    *,
    model_id: str,
    at: datetime,
    outcome: str,
    task: str = TASK,
    latency_ms: int | None = 800,
    output_tokens: int | None = 100,
    source: str = "ideapress",
) -> str:
    """One finished job with one attempt, written the way the worker writes them."""
    version = load_task_profile(database, task).version
    answered = outcome in ("completed", "validation_failed")
    with database.write() as session:
        job = Job(
            id=new_id(),
            task_profile_id=task,
            task_profile_version=version,
            source=source,
            state="completed" if outcome == "completed" else "failed",
            created_at=at,
            queued_at=at,
            started_at=at,
            completed_at=at,
            selected_model_id=model_id,
            attempt=1,
            max_attempts=1,
            validation_passed=(outcome == "completed") if answered else None,
            response_text="x" if answered else None,
        )
        session.add(job)
        session.flush()
        session.add(
            JobAttempt(
                job_id=job.id,
                attempt=1,
                model_id=model_id,
                runtime_profile_hash="h",
                rank=1,
                started_at=at,
                completed_at=at,
                outcome=outcome,
                provider_ms=latency_ms,
                output_tokens=output_tokens,
            )
        )
        return job.id


def _attempts(
    database: Database,
    *,
    model_id: str,
    outcomes: Sequence[str],
    start: datetime,
    step_seconds: float = 20.0,
) -> list[str]:
    return [
        _attempt(
            database, model_id=model_id, at=start + timedelta(seconds=step_seconds * i), outcome=o
        )
        for i, o in enumerate(outcomes)
    ]


def _decide(
    database: Database,
    *,
    now: datetime,
    breakers: CircuitBreakers | None = None,
    task: str = TASK,
) -> dict[str, Any]:
    result = route(
        database,
        RouteRequest(task=task, estimated_input_tokens=1000),
        provider=_facts(),
        policy=RoutingPolicy(machine_fingerprint=MACHINE),
        snapshot=_snapshot(),
        open_circuit_breakers=frozenset() if breakers is None else breakers.excluded(),
        circuit_breaker_details=None if breakers is None else breakers.details(),
        now=now,
    )
    return result.explanation.payload


def _candidate(payload: dict[str, Any], suffix: str) -> dict[str, Any]:
    for candidate in payload["candidates"]:
        if candidate["canonical_id"].endswith(suffix):
            return dict(candidate)
    message = f"no candidate ending in {suffix!r}; rejected: {payload['rejected']}"
    raise AssertionError(message)


def _rejected(payload: dict[str, Any], suffix: str) -> dict[str, Any]:
    for item in payload["rejected"]:
        if item["canonical_id"].endswith(suffix):
            return dict(item)
    message = f"no rejection ending in {suffix!r}; candidates: {payload['candidates']}"
    raise AssertionError(message)


def _refresh(database: Database, breakers: CircuitBreakers, *, now: datetime) -> None:
    """What ``QueueRuntime.refresh_breakers`` does, with the same sample source."""
    since = now - timedelta(seconds=breakers.window_seconds)
    breakers.update(breaker_samples(database, since=since), now=now)


ALPHA = "a" * 12
BETA = "b" * 12


# ------------------------------------------------------------------------- neutral, then live


def test_reliability_factor_is_neutral_below_the_minimum_then_applied(tmp_path: Path) -> None:
    """dev-plan P7 Tests: neutral (exactly 1.0, saying why) at n−1; live at n."""
    database, ids = _seed(tmp_path)
    try:
        _attempts(
            database,
            model_id=ids["alpha"],
            outcomes=["provider_error"] * (PRODUCTION_MINIMUM_SAMPLES - 1),
            start=T - timedelta(hours=1),
        )
        recompute_pair(database, model_id=ids["alpha"], task_profile_id=TASK, now=T)
        payload = _decide(database, now=T)
        alpha = _candidate(payload, ALPHA)
        assert alpha["factors"]["reliability"] == 1.0
        detail = alpha["factors"]["reliability_detail"]
        assert detail["neutral"] is True and detail["window"] is None
        assert detail["source"] == "production" and detail["minimum_samples"] == 20
        assert "7d=19" in detail["reason"]
        assert payload["selected"]["canonical_id"].endswith(ALPHA), "a tie falls to alpha"

        _attempt(database, model_id=ids["alpha"], at=T - timedelta(minutes=1), outcome="timeout")
        recompute_pair(database, model_id=ids["alpha"], task_profile_id=TASK, now=T)
        payload = _decide(database, now=T)
        alpha = _candidate(payload, ALPHA)
        assert alpha["factors"]["reliability"] == 0.5
        detail = alpha["factors"]["reliability_detail"]
        assert detail["neutral"] is False and detail["window"] == "7d"
        assert detail["attempts"] == 20 and detail["success_rate"] == 0.0
        assert detail["error_rate"] == pytest.approx(19 / 20)
        assert detail["timeout_rate"] == pytest.approx(1 / 20)
        assert "0 of 20 attempts answered (0%)" in detail["reason"]
        assert payload["selected"]["canonical_id"].endswith(BETA)
        assert alpha["final_score"] == pytest.approx(alpha["task_fit"] * 0.5)
    finally:
        database.close()


# ------------------------------------------------------------------- feedback changes routing


def test_feedback_from_ideapress_changes_routing_and_is_visible_in_the_explanation(
    tmp_path: Path,
) -> None:
    """P7 AC1/AC2: rejected output deprioritizes the model, with the acceptance in the record.

    IdeaPress is a scaffold and cannot send feedback yet, so the verdicts arrive through the same
    service its ``POST /jobs/{id}/feedback`` calls would reach, with ``source="ideapress"``.
    """
    database, ids = _seed(tmp_path)
    try:
        start = T - timedelta(hours=2)
        alpha_jobs = _attempts(
            database, model_id=ids["alpha"], outcomes=["completed"] * 20, start=start
        )
        _attempts(database, model_id=ids["beta"], outcomes=["completed"] * 20, start=start)
        for model_id in (ids["alpha"], ids["beta"]):
            recompute_pair(database, model_id=model_id, task_profile_id=TASK, now=T)
        before = _decide(database, now=T)
        assert before["selected"]["canonical_id"].endswith(ALPHA)
        assert _candidate(before, ALPHA)["factors"]["reliability"] == 1.0

        for job_id in alpha_jobs[:5]:
            outcome = record_feedback(
                database,
                job_id,
                FeedbackSubmission(source="ideapress", accepted=False, quality_score=0.1),
                now=T,
            )
            assert outcome.created and outcome.model_id == ids["alpha"]

        after = _decide(database, now=T)
        assert after["selected"]["canonical_id"].endswith(BETA), (
            "five rejections must move the decision"
        )
        alpha = _candidate(after, ALPHA)
        assert alpha["factors"]["reliability"] == pytest.approx(0.75)
        detail = alpha["factors"]["reliability_detail"]
        assert detail["acceptance_rate"] == 0.0 and detail["feedback_count"] == 5
        assert "caller acceptance 0% over 5 verdicts" in detail["reason"]
        assert _candidate(after, BETA)["factors"]["reliability"] == 1.0
        stats = stats_for(database, model_id=ids["alpha"], task_profile_id=TASK)
        assert stats["7d"].feedback_count == 5 and stats["7d"].acceptance_rate == 0.0
        assert stats["7d"].mean_quality == pytest.approx(0.1)

        # The same source changing its mind updates the same five records — idempotent per
        # (job, source) — and the factor recovers.
        for job_id in alpha_jobs[:5]:
            assert not record_feedback(
                database, job_id, FeedbackSubmission(source="ideapress", accepted=True), now=T
            ).created
        recovered = _decide(database, now=T)
        assert _candidate(recovered, ALPHA)["factors"]["reliability"] == 1.0
        assert recovered["selected"]["canonical_id"].endswith(ALPHA)
    finally:
        database.close()


# --------------------------------------------------------------------------- the whole cycle


def test_a_failing_model_is_deprioritized_then_excluded_then_reprobed_then_healthy(
    tmp_path: Path,
) -> None:
    """dev-plan P7 Tests, and its named failure mode: healthy → deprioritized → excluded →
    cool-down → probed → healthy again, with the reason readable in every explanation."""
    database, ids = _seed(tmp_path)
    breakers = CircuitBreakers()  # 600 s window, 5 samples, 50 %, 300 s cool-down (P5-9)
    alpha = ids["alpha"]
    try:
        # 1. Healthy: twenty answers in the last 400 s.
        _attempts(
            database, model_id=alpha, outcomes=["completed"] * 20, start=T - timedelta(seconds=400)
        )
        recompute_pair(database, model_id=alpha, task_profile_id=TASK, now=T)
        _refresh(database, breakers, now=T)
        healthy = _decide(database, now=T, breakers=breakers)
        assert healthy["selected"]["canonical_id"].endswith(ALPHA)
        detail = _candidate(healthy, ALPHA)["factors"]["reliability_detail"]
        assert detail["window"] == "7d" and "20 of 20 attempts answered" in detail["reason"]

        # 2. Deprioritized: four errors — a 17 % failure rate, below the breaker's threshold,
        #    but the factor moves at once and says why.
        _attempts(
            database,
            model_id=alpha,
            outcomes=["provider_error"] * 4,
            start=T + timedelta(seconds=20),
        )
        t2 = T + timedelta(seconds=100)
        recompute_pair(database, model_id=alpha, task_profile_id=TASK, now=t2)
        _refresh(database, breakers, now=t2)
        assert breakers.excluded() == frozenset()
        deprioritized = _decide(database, now=t2, breakers=breakers)
        assert deprioritized["selected"]["canonical_id"].endswith(BETA)
        candidate = _candidate(deprioritized, ALPHA)
        assert candidate["factors"]["reliability"] == pytest.approx(0.5 + 0.5 * 20 / 24)
        assert (
            "20 of 24 attempts answered (83%)"
            in candidate["factors"]["reliability_detail"]["reason"]
        )

        # 3. Excluded: twenty more errors push the ten-minute window past 50 %.
        _attempts(
            database,
            model_id=alpha,
            outcomes=["provider_error"] * 20,
            start=T + timedelta(seconds=100),
        )
        t3 = T + timedelta(seconds=500)
        recompute_pair(database, model_id=alpha, task_profile_id=TASK, now=t3)
        _refresh(database, breakers, now=t3)
        assert breakers.excluded() == frozenset({f"fake/alpha:8b@sha256:{ALPHA}"})
        excluded = _decide(database, now=t3, breakers=breakers)
        assert excluded["selected"]["canonical_id"].endswith(BETA)
        rejection = _rejected(excluded, ALPHA)
        assert rejection["reason"] == "recently_failing"
        assert rejection["detail"]["state"] == "open"
        assert "excluded until" in rejection["detail"]["reason"]
        opened_at = datetime.fromisoformat(rejection["detail"]["opened_at"])
        assert opened_at == T + timedelta(seconds=480)

        # 4. Cool-down elapsed: half-open, one probe allowed; while it is out, still excluded.
        t4 = opened_at + timedelta(seconds=300)
        _refresh(database, breakers, now=t4)
        assert breakers.excluded() == frozenset()
        half_open = _decide(database, now=t4, breakers=breakers)
        assert half_open["selected"]["canonical_id"].endswith(BETA)  # eligible, still deprioritized
        assert _candidate(half_open, ALPHA)["factors"]["reliability"] == pytest.approx(
            0.5 + 0.5 * 20 / 44
        )
        verdict = next(v for v in breakers.verdicts() if v.canonical_id.endswith(ALPHA))
        assert verdict.state is BreakerState.HALF_OPEN
        assert breakers.allow_probe(verdict.canonical_id, now=t4) is True
        probing = _decide(database, now=t4, breakers=breakers)
        assert _rejected(probing, ALPHA)["detail"]["probe_in_flight"] is True
        assert _rejected(probing, ALPHA)["detail"]["reason"] == "cool-down elapsed; probe in flight"

        # 5. The probe answers: closed, and the failures that opened it no longer count.
        _attempt(database, model_id=alpha, at=t4 + timedelta(seconds=5), outcome="completed")
        t5 = t4 + timedelta(seconds=10)
        recompute_pair(database, model_id=alpha, task_profile_id=TASK, now=t5)
        _refresh(database, breakers, now=t5)
        assert breakers.excluded() == frozenset()
        verdict = next(v for v in breakers.verdicts() if v.canonical_id.endswith(ALPHA))
        assert verdict.state is BreakerState.CLOSED and verdict.samples == 0
        closed = _decide(database, now=t5, breakers=breakers)
        assert closed["rejected"] == []
        detail = _candidate(closed, ALPHA)["factors"]["reliability_detail"]
        assert detail["attempts"] == 45 and "21 of 45 attempts answered" in detail["reason"]
        assert closed["selected"]["canonical_id"].endswith(BETA)  # deprioritized, not excluded

        # 6. Healthy again: the bad ten minutes age out of both factor windows.
        t6 = T + timedelta(days=31)
        recompute_pair(database, model_id=alpha, task_profile_id=TASK, now=t6)
        _refresh(database, breakers, now=t6)
        healthy_again = _decide(database, now=t6, breakers=breakers)
        assert healthy_again["selected"]["canonical_id"].endswith(ALPHA)
        detail = _candidate(healthy_again, ALPHA)["factors"]["reliability_detail"]
        assert detail["neutral"] is True and detail["value"] == 1.0
        assert stats_for(database, model_id=alpha, task_profile_id=TASK)["all"].attempts == 45
    finally:
        database.close()


# ------------------------------------------------------------ incremental == full (service)


def test_per_event_recomputation_equals_a_full_recomputation_on_every_field(
    tmp_path: Path,
) -> None:
    """The service-level property: after a random sequence of attempts and verdicts, each
    recomputed for the one pair it touched, every stored row equals a recompute-all's."""
    rng = random.Random(20260830)  # noqa: S311 — reproducible inputs, not cryptography
    database, ids = _seed(tmp_path)
    tasks = (TASK, "general.reasoning")
    outcomes = (
        "completed",
        "completed",
        "validation_failed",
        "provider_error",
        "timeout",
        "cancelled",
    )
    now = T + timedelta(days=1)
    try:
        jobs: list[tuple[str, str, str]] = []
        for _ in range(120):
            model = rng.choice(("alpha", "beta"))
            task = rng.choice(tasks)
            at = T - timedelta(hours=rng.randrange(0, 45 * 24))
            job_id = _attempt(
                database,
                model_id=ids[model],
                at=at,
                outcome=rng.choice(outcomes),
                task=task,
                latency_ms=rng.choice([None, 300, 900, 2500]),
                output_tokens=rng.choice([None, 40, 160]),
            )
            jobs.append((job_id, ids[model], task))
            recompute_pair(database, model_id=ids[model], task_profile_id=task, now=now)
            if rng.random() < 0.3:
                target, target_model, target_task = rng.choice(jobs)
                record_feedback(  # recomputes its own pair
                    database,
                    target,
                    FeedbackSubmission(
                        source=rng.choice(("ideapress", "reviewer")),
                        accepted=rng.random() < 0.7,
                        quality_score=rng.choice([None, 0.2, 0.9]),
                        edited=rng.random() < 0.3,
                    ),
                    now=now,
                )
                # Feedback's own recomputation used ``now`` too, so the rows agree on the instant.
                recompute_pair(
                    database, model_id=target_model, task_profile_id=target_task, now=now
                )

        def snapshot() -> dict[tuple[str, str, str], dict[str, Any]]:
            with database.read() as session:
                rows = session.execute(select(ReliabilityStat)).scalars()
                return {
                    (row.model_id, row.task_profile_id, row.window): {
                        column: getattr(row, column)
                        for column in (
                            "attempts",
                            "successes",
                            "validation_passes",
                            "errors",
                            "timeouts",
                            "cancellations",
                            "latency_count",
                            "p50_latency_ms",
                            "p95_latency_ms",
                            "output_token_count",
                            "mean_output_tokens",
                            "tokens_per_second_count",
                            "mean_tokens_per_second",
                            "feedback_count",
                            "acceptance_rate",
                            "quality_count",
                            "mean_quality",
                        )
                    }
                    for row in rows
                }

        incremental = snapshot()
        pairs = recompute_all(database, now=now)
        full = snapshot()
        assert pairs == 4
        assert set(incremental) == set(full) and len(full) == 4 * len(WINDOWS)
        for key in full:
            assert incremental[key] == full[key], key
        assert any(row["attempts"] for row in full.values())
        assert any(row["feedback_count"] for row in full.values())
    finally:
        database.close()


# ------------------------------------------------------- production never overwrites benchmark


def test_production_evidence_never_overwrites_benchmark_evidence_and_both_are_visible(
    tmp_path: Path, golden_bundle: dict[str, Any], wrap_bundle: Callable[..., str]
) -> None:
    """routing §11: separate sources, separate confidence, both shown with their own labels."""
    database, ids = _seed(tmp_path)
    try:
        profile_hash = _decide(database, now=T)["selected"]["runtime_profile_hash"]
        beta = _identity(database, "beta:8b")
        outcome = import_bundle(
            database,
            wrap_bundle(
                _bundle(
                    _record(
                        _template(golden_bundle),
                        identity=beta,
                        capability="reasoning",
                        score=0.94,
                        confidence=0.81,
                        profile_hash=profile_hash,
                    )
                )
            ),
            now=T,
        )
        assert outcome.bound == 1
        _attempts(
            database,
            model_id=ids["beta"],
            outcomes=["completed"] * 15 + ["validation_failed"] * 5,
            start=T - timedelta(hours=1),
        )
        recompute_pair(database, model_id=ids["beta"], task_profile_id=TASK, now=T)

        payload = _decide(database, now=T)
        candidate = _candidate(payload, BETA)
        benchmark = _capability(payload, beta["canonical_id"], "reasoning")
        assert benchmark["source"] == "benchmark"
        assert benchmark["score"] == 0.94 and benchmark["confidence"] == 0.81
        production = candidate["factors"]["reliability_detail"]
        assert production["source"] == "production"
        assert production["window"] == "7d" and production["attempts"] == 20
        assert production["validation_pass_rate"] == pytest.approx(0.75)
        assert candidate["factors"]["reliability"] == pytest.approx(0.5 + 0.5 * 0.75)
        assert all(entry["source"] != "production" for entry in candidate["capabilities"]), (
            "production evidence acts through the factor, never through a capability score"
        )
        with database.read() as session:
            row = session.execute(
                select(CapabilityEvidence).where(CapabilityEvidence.capability_id == "reasoning")
            ).scalar_one()
            assert row.score == 0.94 and row.confidence == 0.81 and row.sample_count == 60
    finally:
        database.close()


# ------------------------------------------------------------------- the worker's own path


def test_every_attempt_the_worker_writes_updates_reliability_stats(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The incremental path through the real runtime: queued jobs and a synchronous call."""
    with _client(tmp_path, monkeypatch) as client:
        for _ in range(3):
            response = client.post("/api/v1/jobs", json={"task": TASK, "prompt": "hello"})
            assert response.status_code == 202
            assert _wait(client, response.json()["job_id"])["state"] == "completed"
        database: Database = client.app.state.database  # type: ignore[attr-defined]  # FastAPI app
        with database.read() as session:
            model_id = session.execute(select(Model.id)).scalar_one()
        stats = stats_for(database, model_id=model_id, task_profile_id=TASK)
        assert {window.name for window in WINDOWS} == set(stats)
        assert stats["7d"].attempts == 3 and stats["7d"].successes == 3
        assert stats["7d"].validation_passes == 3 and stats["7d"].latency_count == 3

        sync = client.post("/api/v1/generate", json={"task": TASK, "prompt": "hello"})
        assert sync.status_code == 200, sync.text
        stats = stats_for(database, model_id=model_id, task_profile_id=TASK)
        assert stats["7d"].attempts == 4 and stats["all"].attempts == 4
        feedback = client.post(
            f"/api/v1/jobs/{sync.json()['job_id']}/feedback",
            json={"accepted": True},
            headers={"X-Client-Name": "ideapress"},
        )
        assert feedback.status_code == 201
        assert (
            stats_for(database, model_id=model_id, task_profile_id=TASK)["7d"].feedback_count == 1
        )
