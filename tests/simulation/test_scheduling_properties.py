"""The scheduling properties (queue §12), proven by simulation over the real queue code.

Every test here drives the real worker loop, the real scheduler tick and the real queue
statements against a real database, with only time, the provider's work and the GPU faked.
Where a property is about a mechanism (the keeper, the sweep), the test also switches that
mechanism off and asserts the property *fails* — so the test is known to be watching the
mechanism and not something that happens to be true anyway.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from modelrack.testing import FakeFailure, FakeFailureMode
from tests.simulation.simulator import (
    DEFAULT_START,
    GenerationSpec,
    Simulation,
    sim_model,
)

from loadcoach.config import ExecutionSettings, QueueSettings
from loadcoach.domain.priority import JobClass
from loadcoach.domain.queue_state import JobState


@pytest.fixture
def simulation(tmp_path: Path) -> Iterator[Simulation]:
    sim = Simulation(tmp_path, models=(sim_model("alpha:8b", load_seconds=0.0),))
    try:
        yield sim
    finally:
        sim.close()


def _seconds(sim: Simulation, job_id: str, attribute: str) -> float | None:
    value = getattr(sim.job(job_id), attribute)
    return None if value is None else (value - DEFAULT_START).total_seconds()


# --- the pipeline ----------------------------------------------------------------------------


def test_a_job_runs_through_the_real_pipeline_to_completion(simulation: Simulation) -> None:
    simulation.provider.script(
        "hello", GenerationSpec(duration_seconds=6.0, chunks=3, text="hi there")
    )
    simulation.start_queue()
    job_id = simulation.submit("hello").job_id
    simulation.run_for(30)
    record = simulation.job(job_id)
    assert record.state is JobState.COMPLETED
    assert record.response_text == "hi there"
    assert record.attempt == 1
    assert record.lease_owner is None and record.lease_expires_at is None
    assert record.selected_model_id is not None and record.runtime_profile_hash is not None
    assert record.queue_wait_ms == 0  # an idle worker was woken by the enqueue
    assert record.provider_ms is not None and record.total_ms is not None
    assert [event_type for _, event_type in simulation.events(job_id)] == [
        "job.queued",
        "job.leased",
        "job.admitted",
        "job.executing",
        "job.validating",
        "job.completed",
    ]
    assert simulation.attempts(job_id) == [(1, "completed")]
    assert _seconds(simulation, job_id, "completed_at") == 6.0


def test_an_idle_worker_dispatches_on_the_enqueue_wake_up_not_the_next_poll(
    simulation: Simulation,
) -> None:
    """ADR-0010: the in-process wake-up is what meets the dispatch budget without busy-waiting."""
    simulation.provider.script("quick", GenerationSpec(duration_seconds=1.0, chunks=1))
    simulation.start_queue()
    simulation.run_for(30)  # the worker has backed off to its 1 s idle poll by now
    job_ids: list[str] = []
    simulation.at(30.3, lambda: job_ids.append(simulation.submit("quick").job_id))
    simulation.run_for(5)
    assert simulation.job(job_ids[0]).state is JobState.COMPLETED
    # Claimed at the enqueue instant, not up to a second later.
    assert _seconds(simulation, job_ids[0], "started_at") == pytest.approx(30.3)
    assert simulation.job(job_ids[0]).queue_wait_ms == 0


def test_priority_ordering_across_classes(simulation: Simulation) -> None:
    """Queue §1: interactive before normal before background before batch, FIFO within a class."""
    simulation.provider.script("job", GenerationSpec(duration_seconds=10.0, chunks=2))
    simulation.start_queue()
    order: list[tuple[str, str]] = []

    def submit_all() -> None:
        for job_class in (
            JobClass.BATCH,
            JobClass.BACKGROUND,
            JobClass.NORMAL,
            JobClass.INTERACTIVE,
        ):
            for suffix in ("first", "second"):
                job_id = simulation.submit("job", job_class=job_class).job_id
                order.append((f"{job_class.value}-{suffix}", job_id))

    # Submit while the worker is busy so the whole set is queued at once and ordered by policy.
    blocker = simulation.submit("job").job_id
    simulation.run_for(1)
    submit_all()
    simulation.run_for(200)
    assert simulation.job(blocker).state is JobState.COMPLETED
    started = sorted(order, key=lambda pair: _seconds(simulation, pair[1], "started_at") or 0)
    assert [label for label, _ in started] == [
        "interactive-first",
        "interactive-second",
        "normal-first",
        "normal-second",
        "background-first",
        "background-second",
        "batch-first",
        "batch-second",
    ]


def test_the_concurrency_limit_holds_under_a_burst(tmp_path: Path) -> None:
    sim = Simulation(
        tmp_path,
        models=(sim_model("alpha:8b", load_seconds=0.0),),
        execution=ExecutionSettings(max_concurrent_jobs=2),
    )
    try:
        sim.provider.script("burst", GenerationSpec(duration_seconds=5.0, chunks=5))
        runtime = sim.start_queue()
        peak = 0

        def sample() -> None:
            nonlocal peak
            peak = max(peak, len(runtime.in_flight))

        sim.driver.every(0.5, sample, label="sample")
        job_ids = [sim.submit("burst").job_id for _ in range(10)]
        sim.run_for(60)
        assert all(sim.job(job_id).state is JobState.COMPLETED for job_id in job_ids)
        assert peak == 2
        # Ten 5-second jobs on two workers: 25 s of wall time, not 50.
        finished = max(_seconds(sim, job_id, "completed_at") or 0 for job_id in job_ids)
        assert finished == pytest.approx(25.0, abs=1.0)
    finally:
        sim.close()


# --- leases ----------------------------------------------------------------------------------


def test_the_keeper_renews_a_lease_across_an_attempt_five_times_longer_than_it(
    tmp_path: Path,
) -> None:
    """ADR-0029 §4: a 300 s generation under a 60 s lease is not reclaimed while the keeper runs."""
    sim = Simulation(
        tmp_path,
        models=(sim_model("alpha:8b", load_seconds=0.0),),
        execution=ExecutionSettings(max_concurrent_jobs=2),
    )
    try:
        sim.provider.script("long", GenerationSpec(duration_seconds=300.0, chunks=30))
        runtime = sim.start_queue()
        job_id = sim.submit("long").job_id
        sim.run_for(320)
        assert sim.job(job_id).state is JobState.COMPLETED
        assert sim.attempts(job_id) == [(1, "completed")]
        assert [t for _, t in sim.events(job_id)].count("job.leased") == 1
        assert runtime.scheduler is not None and runtime.scheduler.renewals >= 14
        assert len(sim.provider.calls) == 1  # executed exactly once, by one worker
    finally:
        sim.close()


def test_when_the_keeper_stalls_the_lease_expires_and_the_job_is_reclaimed_once(
    tmp_path: Path,
) -> None:
    """The mutation check for the test above: stall the keeper, and the reclaim happens.

    The keeper stalls from the first tick and recovers at t=70, after the reaper has requeued
    the job (lease expired at t=60) and the second worker has reclaimed it — the scheduler
    thread pausing and resuming, which is the event a lease exists to detect. The first
    worker's late completion is refused by the lease fence, so the job completes exactly once,
    by the reclaiming worker, and the history shows a single attempt row.
    """
    sim = Simulation(
        tmp_path,
        models=(sim_model("alpha:8b", load_seconds=0.0),),
        execution=ExecutionSettings(max_concurrent_jobs=2),
    )
    try:
        sim.provider.script("long", GenerationSpec(duration_seconds=300.0, chunks=30))
        runtime = sim.start_queue()
        assert runtime.scheduler is not None
        scheduler = runtime.scheduler
        scheduler.keeper_enabled = False
        job_id = sim.submit("long").job_id
        sim.at(70, lambda: setattr(scheduler, "keeper_enabled", True))
        sim.run_for(700)
        record = sim.job(job_id)
        assert record.state is JobState.COMPLETED
        types = [t for _, t in sim.events(job_id)]
        assert types.count("job.leased") == 2
        assert "job.queued" in types[1:]  # requeued by the reaper after the lease expired
        assert len(sim.provider.calls) == 2  # executed twice: the race the keeper prevents
        assert sim.attempts(job_id) == [(1, "completed")]  # the first worker's write was fenced
        assert record.attempt == 1
        assert types.count("job.completed") == 1
        # Completed by the second worker at 61 + 300 s, not by the first at 300 s.
        assert _seconds(sim, job_id, "completed_at") == pytest.approx(361.0, abs=1.5)
    finally:
        sim.close()


def test_attempt_numbering_continues_across_a_lost_lease_with_no_collision(
    tmp_path: Path,
) -> None:
    """ADR-0029 §2's required sequence: claim, in-lease corrective retry, lease loss, re-claim.

    ``code.review`` requires schema-valid JSON, so the first two attempts fail validation and the
    worker retries correctively in-lease (attempts 1 and 2 written). The third attempt is a long
    generation during which the keeper is stopped; the lease expires, the job is reclaimed and
    the reclaiming worker's attempt takes number 3. The first worker's late write is refused.
    """
    sim = Simulation(
        tmp_path,
        models=(sim_model("alpha:8b", load_seconds=0.0),),
        execution=ExecutionSettings(
            max_concurrent_jobs=2, max_attempts=6, attempt_backoff_seconds=1.0
        ),
    )
    try:
        bad = GenerationSpec(duration_seconds=2.0, chunks=1, text="not json at all")
        slow_bad = GenerationSpec(duration_seconds=300.0, chunks=30, text="still not json")
        good = GenerationSpec(
            duration_seconds=2.0, chunks=1, text='{"findings": [], "summary": "fine"}'
        )
        sim.provider.script("review", bad, bad, slow_bad, good)
        runtime = sim.start_queue()
        assert runtime.scheduler is not None
        scheduler = runtime.scheduler
        job_id = sim.submit("review", task="code.review").job_id
        # Let attempts 1 and 2 fail and be written, then stall the keeper during attempt 3.
        sim.at(20, lambda: setattr(scheduler, "keeper_enabled", False))
        sim.run_for(900)
        record = sim.job(job_id)
        assert record.state is JobState.COMPLETED, record.error_text
        attempts = sim.attempts(job_id)
        assert [number for number, _ in attempts] == [1, 2, 3]
        assert [outcome for _, outcome in attempts] == [
            "validation_failed",
            "validation_failed",
            "completed",
        ]
        assert record.attempt == 3
        assert [t for _, t in sim.events(job_id)].count("job.leased") == 2
    finally:
        sim.close()


def test_a_provider_failure_falls_back_and_a_timeout_is_recorded(tmp_path: Path) -> None:
    sim = Simulation(
        tmp_path,
        models=(sim_model("alpha:8b", load_seconds=0.0), sim_model("beta:8b", load_seconds=0.0)),
        execution=ExecutionSettings(attempt_backoff_seconds=0.0),
    )
    try:
        sim.provider.script(
            "flaky",
            GenerationSpec(
                duration_seconds=1.0,
                chunks=1,
                failure=FakeFailure(FakeFailureMode.UNAVAILABLE, after_chunks=0),
            ),
            GenerationSpec(duration_seconds=1.0, chunks=1, text="recovered"),
        )
        sim.start_queue()
        job_id = sim.submit("flaky", task="general.chat").job_id
        sim.run_for(60)
        record = sim.job(job_id)
        assert record.state is JobState.COMPLETED
        assert record.response_text == "recovered"
        assert [outcome for _, outcome in sim.attempts(job_id)] == ["provider_error", "completed"]
        types = [t for _, t in sim.events(job_id)]
        assert "job.retrying" in types
        assert types.count("job.admitted") == 2
    finally:
        sim.close()


def test_a_lost_lease_on_non_idempotent_work_fails_with_worker_lost(tmp_path: Path) -> None:
    sim = Simulation(
        tmp_path,
        models=(sim_model("alpha:8b", load_seconds=0.0),),
        queue=QueueSettings(lease_seconds=30, lease_renewal_interval_seconds=10),
    )
    try:
        sim.provider.script("long", GenerationSpec(duration_seconds=120.0, chunks=12))
        runtime = sim.start_queue()
        assert runtime.scheduler is not None
        runtime.scheduler.keeper_enabled = False
        job_id = sim.submit("long", idempotent=False).job_id
        sim.run_for(200)
        record = sim.job(job_id)
        assert record.state is JobState.FAILED
        assert record.state_reason == "worker_lost"
        assert record.error_code == "WORKER_LOST"
        assert _seconds(sim, job_id, "completed_at") == pytest.approx(31.0, abs=1.0)
        assert len(sim.provider.calls) == 1  # never re-run: that is what non-idempotent means
    finally:
        sim.close()
