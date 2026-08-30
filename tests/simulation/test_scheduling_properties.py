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


# --- admission and residency (queue §5, §6; ADR-0027) ------------------------------------------

GIB = 1024**3


# A sim_model of S bytes needs S x 1.05 (weights) + 0.5 GiB (KV at general.chat's 4096-token
# served context) + 256 MiB (activation) + 512 MiB (headroom) free: about 9.65 GiB for 8 GiB.


def test_insufficient_vram_defers_with_numbers_and_resumes_when_it_frees(tmp_path: Path) -> None:
    """Acceptance criterion 3: defers with a reason rather than failing or thrashing."""
    sim = Simulation(
        tmp_path, models=(sim_model("alpha:8b", size_bytes=8 * GIB, load_seconds=1.0),)
    )
    try:
        sim.provider.script("job", GenerationSpec(duration_seconds=5.0, chunks=1))
        sim.occupy(0, 10 * GIB)  # 6 GiB free; the model needs about 9.65 GiB free
        runtime = sim.start_queue()
        job_id = sim.submit("job").job_id
        sim.run_for(30)
        record = sim.job(job_id)
        assert record.state is JobState.WAITING_RESOURCES
        assert record.state_reason == "INSUFFICIENT_RESOURCES"
        assert record.lease_owner is None and record.lease_expires_at is None
        assert record.error_text is not None and "free by device" in record.error_text
        events = sim.events(job_id)
        assert [t for _, t in events] == ["job.queued", "job.leased", "job.waiting_resources"]
        from sqlalchemy import select

        from loadcoach.infrastructure.db.models import JobEvent

        with sim.database.read() as session:
            data = session.execute(
                select(JobEvent.data_json).where(
                    JobEvent.job_id == job_id, JobEvent.event_type == "job.waiting_resources"
                )
            ).scalar_one()
        assert isinstance(data, dict)
        assert data["free_bytes_by_gpu"] == {"0": 6 * GIB}
        assert data["required_bytes"] is not None and data["required_bytes"] > 6 * GIB
        assert data["headroom_bytes"] == 512 * 1024**2
        # Thirty seconds of the scheduler's re-evaluation changed nothing: no claim-defer thrash.
        assert len(sim.provider.calls) == 0
        assert runtime.scheduler is not None and runtime.scheduler.requeued == 0

        sim.occupy(0, 0)  # the other tenant leaves
        sim.run_for(30)
        record = sim.job(job_id)
        assert record.state is JobState.COMPLETED
        types = [t for _, t in sim.events(job_id)]
        assert types[:5] == [
            "job.queued",
            "job.leased",
            "job.waiting_resources",
            "job.queued",
            "job.leased",
        ]
        assert types.count("job.waiting_resources") == 1
        assert runtime.scheduler.requeued == 1
    finally:
        sim.close()


def test_two_gpus_are_never_summed_for_admission(tmp_path: Path) -> None:
    """Queue §12's two-GPU fixture: bigger than either device, smaller than their sum: deferred."""
    sim = Simulation(
        tmp_path,
        models=(sim_model("big:14b", size_bytes=14 * GIB, load_seconds=1.0),),
        gpus=((0, 12 * GIB), (1, 12 * GIB)),
    )
    try:
        sim.start_queue()
        job_id = sim.submit("job").job_id
        sim.run_for(120)
        record = sim.job(job_id)
        assert record.state is JobState.WAITING_RESOURCES
        assert record.error_text is not None
        assert "'0': 12884901888" in record.error_text and "'1': 12884901888" in record.error_text
        assert len(sim.provider.calls) == 0 and sim.provider.loads == 0
    finally:
        sim.close()


def test_the_per_device_aggregate_defers_a_second_job_while_the_first_holds_the_device(
    tmp_path: Path,
) -> None:
    """Above max_concurrent_jobs = 1, concurrent jobs targeting GPU 0 sum against GPU 0's memory."""
    sim = Simulation(
        tmp_path,
        models=(
            sim_model("alpha:8b", size_bytes=8 * GIB, load_seconds=1.0),
            sim_model("beta:8b", size_bytes=8 * GIB, load_seconds=1.0),
        ),
        execution=ExecutionSettings(max_concurrent_jobs=2),
    )
    try:
        sim.provider.script("a", GenerationSpec(duration_seconds=20.0, chunks=4))
        sim.provider.script("b", GenerationSpec(duration_seconds=5.0, chunks=1))
        runtime = sim.start_queue()
        alpha = sim.canonical_id("alpha:8b")
        beta = sim.canonical_id("beta:8b")
        first = sim.submit("a", model=alpha).job_id
        sim.run_for(3)  # alpha loaded and executing: 8 GiB used of 16, 8 GiB free
        second = sim.submit("b", model=beta).job_id
        sim.run_for(5)
        assert sim.job(first).state is JobState.EXECUTING
        assert sim.job(second).state is JobState.WAITING_RESOURCES  # 8 GiB free < 9.65 needed
        sim.run_for(60)
        assert sim.job(first).state is JobState.COMPLETED
        assert sim.job(second).state is JobState.COMPLETED
        # Once alpha's job finished, alpha was idle and evictable; beta's load evicted it.
        assert sim.provider.unloads == 1
        assert runtime.scheduler is not None and runtime.scheduler.requeued == 1
        assert (_seconds(sim, second, "started_at") or 0) >= 21.0
    finally:
        sim.close()


def test_jobs_on_different_devices_run_concurrently(tmp_path: Path) -> None:
    """The aggregate is per device: a job on GPU 1 does not wait for GPU 0.

    GPU 0 (12 GiB) holds one 8 GiB model with 4 GiB to spare; the second job cannot fit there
    beside it and is admitted on GPU 1 instead of waiting — devices are independent.
    """
    sim = Simulation(
        tmp_path,
        models=(
            sim_model("alpha:8b", size_bytes=8 * GIB, load_seconds=1.0),
            sim_model("beta:8b", size_bytes=8 * GIB, load_seconds=1.0),
        ),
        gpus=((0, 12 * GIB), (1, 16 * GIB)),
        placement={"alpha:8b": 0, "beta:8b": 1},
        execution=ExecutionSettings(max_concurrent_jobs=2),
    )
    try:
        sim.provider.script("a", GenerationSpec(duration_seconds=20.0, chunks=4))
        sim.provider.script("b", GenerationSpec(duration_seconds=20.0, chunks=4))
        sim.start_queue()
        first = sim.submit("a", model=sim.canonical_id("alpha:8b")).job_id
        sim.run_for(3)
        second = sim.submit("b", model=sim.canonical_id("beta:8b")).job_id
        sim.run_for(60)
        assert sim.job(first).state is JobState.COMPLETED
        assert sim.job(second).state is JobState.COMPLETED
        # Started while the first was still running, on the other device.
        assert (_seconds(sim, second, "started_at") or 0) < (
            _seconds(sim, first, "completed_at") or 0
        )
        assert {sim.job(first).target_gpu_index, sim.job(second).target_gpu_index} == {0, 1}
    finally:
        sim.close()


def test_affinity_batching_cuts_model_loads_without_breaching_the_wait_bound(
    tmp_path: Path,
) -> None:
    """Queue §12: affinity improves the load count without becoming a starvation source."""

    def run(path: Path, *, affinity: bool) -> tuple[int, float]:
        sim = Simulation(
            path,
            models=(
                sim_model("alpha:4b", size_bytes=4 * GIB, load_seconds=20.0),
                sim_model("beta:4b", size_bytes=4 * GIB, load_seconds=20.0),
            ),
        )
        try:
            sim.provider.script("job", GenerationSpec(duration_seconds=5.0, chunks=1))
            runtime = sim.start_queue()
            if not affinity:
                runtime.resident_model_ids = lambda: frozenset()  # the mutation: no affinity
            alpha, beta = sim.canonical_id("alpha:4b"), sim.canonical_id("beta:4b")
            blocker = sim.submit("job", model=alpha).job_id
            sim.run_for(1)  # the worker is busy: everything below queues at equal priority
            job_ids = [
                sim.submit("job", model=alpha if index % 2 == 0 else beta).job_id
                for index in range(10)
            ]
            sim.run_for(600)
            assert sim.job(blocker).state is JobState.COMPLETED
            assert all(sim.job(job_id).state is JobState.COMPLETED for job_id in job_ids)
            slowest = max(_seconds(sim, job_id, "completed_at") or 0 for job_id in job_ids)
            return sim.provider.loads, slowest
        finally:
            sim.close()

    loads_with, slowest_with = run(tmp_path / "affinity", affinity=True)
    loads_without, slowest_without = run(tmp_path / "plain", affinity=False)
    assert loads_with <= 3, loads_with  # alpha once (the blocker), beta once, maybe alpha again
    assert loads_without >= 8, loads_without  # alternating: nearly every job reloads
    assert slowest_with < slowest_without
    # The bound: no job waited longer than the whole batch plus every load it could imply.
    assert slowest_with <= 11 * 5 + 3 * 20 + 5


def test_idle_models_are_unloaded_after_unload_idle_seconds(tmp_path: Path) -> None:
    from loadcoach.config import ResidencySettings

    sim = Simulation(
        tmp_path,
        models=(sim_model("alpha:8b", load_seconds=1.0),),
        residency=ResidencySettings(unload_idle_seconds=30, max_resident_models=1),
    )
    try:
        sim.provider.script("job", GenerationSpec(duration_seconds=4.0, chunks=1))
        runtime = sim.start_queue()
        assert runtime.residency is not None
        job_id = sim.submit("job").job_id
        sim.run_for(10)
        assert sim.job(job_id).state is JobState.COMPLETED
        assert sim.provider.resident_names() == frozenset({"alpha:8b"})
        assert runtime.residency.resident_canonical_ids() == frozenset(
            {sim.canonical_id("alpha:8b")}
        )
        assert sim.gpus[0].used_bytes == 4 * GIB
        sim.run_for(50)
        assert sim.provider.unloads == 1
        assert sim.provider.resident_names() == frozenset()
        assert runtime.residency.resident_canonical_ids() == frozenset()
        assert sim.gpus[0].used_bytes == 0
        from sqlalchemy import select

        from loadcoach.infrastructure.db.models import Residency

        with sim.database.read() as session:
            rows = session.execute(select(Residency)).scalars().all()
        assert len(rows) == 1
        assert rows[0].resident is False and rows[0].unload_reason == "idle"
        assert rows[0].gpu_index == 0 and rows[0].vram_bytes == float(4 * GIB)
    finally:
        sim.close()


def test_max_resident_models_evicts_the_least_recently_used_per_device(tmp_path: Path) -> None:
    from loadcoach.config import ResidencySettings

    sim = Simulation(
        tmp_path,
        models=(
            sim_model("alpha:4b", size_bytes=4 * GIB, load_seconds=1.0),
            sim_model("beta:4b", size_bytes=4 * GIB, load_seconds=1.0),
            sim_model("gamma:4b", size_bytes=4 * GIB, load_seconds=1.0),
        ),
        residency=ResidencySettings(unload_idle_seconds=3600, max_resident_models=2),
    )
    try:
        sim.provider.script("job", GenerationSpec(duration_seconds=2.0, chunks=1))
        sim.start_queue()
        for name in ("alpha:4b", "beta:4b", "alpha:4b", "gamma:4b"):
            sim.submit("job", model=sim.canonical_id(name))
            sim.run_for(10)
        # alpha was used more recently than beta, so beta is the one gamma evicts.
        assert sim.provider.resident_names() == frozenset({"alpha:4b", "gamma:4b"})
        assert sim.provider.unloads == 1
        from sqlalchemy import select

        from loadcoach.infrastructure.db.models import Model, Residency

        with sim.database.read() as session:
            evicted = session.execute(
                select(Model.provider_model_name, Residency.unload_reason)
                .join(Model, Model.id == Residency.model_id)
                .where(Residency.resident.is_(False))
            ).all()
        assert [(name, reason) for name, reason in evicted] == [
            ("beta:4b", f"evicted_for:{sim.canonical_id('gamma:4b')}")
        ]
    finally:
        sim.close()


# --- ageing under a running clock (queue §4, ADR-0029 §1) ------------------------------------


def _starvation_scenario(path: Path, *, sweep: bool) -> tuple[float | None, JobState]:
    """One worker, 30 s jobs; an interactive job every 60 s; three normal jobs always queued.

    A background job submitted at t=0 can only run once ageing lifts it to a fresh normal job's
    priority. At ten points per minute and a 300-point gap that is thirty minutes — plus one
    sweep interval and the jobs already ahead of it. Returns its start and final state.
    """
    sim = Simulation(
        path,
        models=(sim_model("alpha:8b", load_seconds=0.0),),
        queue=QueueSettings(ageing_priority_per_minute=10.0, max_wait_seconds=7200),
    )
    try:
        sim.provider.script("job", GenerationSpec(duration_seconds=30.0, chunks=1))
        runtime = sim.start_queue()
        assert runtime.scheduler is not None
        runtime.scheduler.sweep_enabled = sweep

        def refill() -> None:
            from loadcoach.services.queue import queue_snapshot

            depth = queue_snapshot(
                sim.database, now=sim.clock.now(), default_max_wait_seconds=7200
            ).depth_by_class.get("normal", 0)
            for _ in range(max(3 - depth, 0)):
                sim.submit("job", job_class=JobClass.NORMAL)

        def interactive() -> None:
            sim.submit("job", job_class=JobClass.INTERACTIVE)

        # The worker is busy with the first interactive job and three normal jobs are queued
        # before the background job arrives, so it competes from its first second.
        interactive()
        refill()
        sim.run_for(1)
        background = sim.submit("job", job_class=JobClass.BACKGROUND).job_id
        sim.driver.every(10.0, refill, label="refill")
        sim.driver.every(60.0, interactive, label="interactive")
        sim.run_for(45 * 60)
        return _seconds(sim, background, "started_at"), sim.job(background).state
    finally:
        sim.close()


def test_the_starvation_bound_holds_under_a_running_clock_with_continuous_interactive_load(
    tmp_path: Path,
) -> None:
    """Acceptance criterion 2, proven by simulation: the background job's wait is bounded.

    The bound, from the policy: the 300-point gap to a fresh normal job at ten points a minute is
    thirty minutes; the normal jobs it competes with age too, and a filler job waits up to ninety
    seconds in the queue (fifteen points, ninety seconds more); then one sweep interval, the job
    executing at that moment and one interactive job that may be ahead — thirty seconds each.
    Thirty-four minutes. With the sweep switched off — a startup-only recomputation — the same
    job is still queued at forty-five minutes: the mutation this test exists to catch
    (ADR-0029 §1).
    """
    started, state = _starvation_scenario(tmp_path / "with-sweep", sweep=True)
    assert state is JobState.COMPLETED
    assert started is not None
    bound = 30 * 60 + 90 + 30 + 30 + 30
    assert 29 * 60 <= started <= bound, (started, bound)

    started_without, state_without = _starvation_scenario(tmp_path / "no-sweep", sweep=False)
    assert started_without is None
    assert state_without is JobState.QUEUED
