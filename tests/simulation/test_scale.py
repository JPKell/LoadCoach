"""The scheduler at scale (dev-plan P9 unit 11): a thousand mixed-class jobs on a fake clock.

Marked ``performance``: the simulation drives the real queue, routing and workers through the
real SQLite file, so it costs real seconds. What it proves — every job completes, interactive
work waits least, the background wait stays inside the starvation bound, and the machine is
kept busy — is what queue §4 and §12 promise at a scale the property tests do not reach.
"""

from __future__ import annotations

import statistics
import time
from pathlib import Path

import pytest
from tests.simulation.simulator import GenerationSpec, Simulation, sim_model

from loadcoach.config import ExecutionSettings, QueueSettings
from loadcoach.domain.priority import JobClass
from loadcoach.domain.queue_state import JobState

pytestmark = pytest.mark.performance

JOBS = 1000
WORKERS = 4
MIX: tuple[tuple[JobClass, float, float], ...] = (
    # class, share of the load, scripted duration in simulated seconds
    (JobClass.INTERACTIVE, 0.20, 1.0),
    (JobClass.NORMAL, 0.40, 2.0),
    (JobClass.BACKGROUND, 0.25, 3.0),
    (JobClass.BATCH, 0.15, 4.0),
)
STARVATION_BOUND_SECONDS = 34 * 60
"""The background bound the running-clock starvation test derives from the policy (queue §4)."""


def test_a_thousand_mixed_jobs_complete_with_the_promised_ordering_and_bounds(
    tmp_path: Path,
) -> None:
    sim = Simulation(
        tmp_path,
        models=(sim_model("alpha:8b", load_seconds=0.0),),
        execution=ExecutionSettings(max_concurrent_jobs=WORKERS, max_attempts=2),
        queue=QueueSettings(max_depth=JOBS + 10, max_active_per_source=JOBS + 10),
    )
    wall_started = time.perf_counter()
    try:
        for job_class, _share, duration in MIX:
            sim.provider.script(
                job_class.value, GenerationSpec(duration_seconds=duration, chunks=2)
            )
        sim.start_queue(workers=WORKERS)
        submitted: dict[JobClass, list[str]] = {job_class: [] for job_class, _, _ in MIX}
        # Everything arrives within the first minute, in a repeating interleave, so every class
        # is competing from the start.
        for index in range(JOBS):
            share_point = (index % 20) / 20.0
            cumulative = 0.0
            chosen = MIX[-1][0]
            for job_class, share, _ in MIX:
                cumulative += share
                if share_point < cumulative:
                    chosen = job_class
                    break
            submitted[chosen].append(sim.submit(chosen.value, job_class=chosen).job_id)
            if index % 50 == 49:
                sim.run_for(3.0)
        # Work to do: 200 × 1 + 400 × 2 + 250 × 3 + 150 × 4 = 2 350 simulated seconds over four
        # workers ≈ 590 s if the machine is never idle. Give it an hour of simulated time.
        sim.run_for(3600.0)
        states = {
            job_class: [sim.job(j).state for j in ids] for job_class, ids in submitted.items()
        }
        assert all(state is JobState.COMPLETED for group in states.values() for state in group), {
            c.value: sorted({s.value for s in g}) for c, g in states.items()
        }

        waits: dict[JobClass, list[float]] = {}
        finished_at = 0.0
        for job_class, ids in submitted.items():
            for job_id in ids:
                record = sim.job(job_id)
                assert record.queued_at is not None and record.started_at is not None
                waits.setdefault(job_class, []).append(
                    (record.started_at - record.queued_at).total_seconds()
                )
                assert record.completed_at is not None
                finished_at = max(finished_at, (record.completed_at - sim.start).total_seconds())
        medians = {job_class: statistics.median(values) for job_class, values in waits.items()}
        assert medians[JobClass.INTERACTIVE] <= medians[JobClass.NORMAL] <= medians[JobClass.BATCH]
        assert max(waits[JobClass.BACKGROUND]) <= STARVATION_BOUND_SECONDS
        assert max(waits[JobClass.BATCH]) <= STARVATION_BOUND_SECONDS
        # Four workers on 2 350 s of scripted work ≈ 590 s if the machine is never idle. The
        # simulated provider attributes a job's time from its first chunk, so the ideal is a
        # little lower than the arithmetic; well under twice it is the claim — idle gaps would be
        # the scheduler's, not the work's.
        ideal = sum(share * JOBS * duration for _, share, duration in MIX) / WORKERS
        assert 0.85 * ideal <= finished_at <= 2.0 * ideal, (finished_at, ideal)
        wall = time.perf_counter() - wall_started
        waits_line = ", ".join(
            f"{job_class.value} {medians[job_class]:.0f} s" for job_class, _, _ in MIX
        )
        print(  # noqa: T201 — the report
            f"\n{JOBS} jobs on {WORKERS} workers: last completion at {finished_at:.0f} s "
            f"simulated; median waits {waits_line}; max background wait "
            f"{max(waits[JobClass.BACKGROUND]):.0f} s; {wall:.1f} s of real time"
        )
        assert wall <= 300.0, f"the simulation took {wall:.0f} s of real time"
    finally:
        sim.close()
