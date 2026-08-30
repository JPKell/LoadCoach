"""The simulator's own mechanics, proven before any scheduler exists to point it at.

A simulator written after the scheduler tests what was built; one written before must be shown to
be a faithful instrument on its own terms. These tests drive stand-in workers through the same
primitives the real worker will use — the driver's ``sleep``, a :class:`SimulatedWakeup`, the
simulated provider's chunked stream — and assert the interleaving, the clock, cancellation within
one chunk, timeout, failure injection, load-on-demand accounting and determinism.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from baseaicore import RuntimeProfile
from modelrack import (
    CancellationToken,
    GenerationCancelled,
    GenerationRequest,
    Message,
    ProviderTimeout,
    ProviderUnavailable,
    Role,
    StreamFailed,
    TokenDelta,
)
from modelrack.testing import FakeFailure, FakeFailureMode
from tests.simulation.simulator import (
    DEFAULT_START,
    Driver,
    FakeClock,
    GenerationSpec,
    SimulatedProvider,
    SimulatedWakeup,
    Simulation,
    SimulationError,
    sim_model,
    sim_prompt,
)


def _request(provider: SimulatedProvider, label: str, **kwargs: object) -> GenerationRequest:
    return GenerationRequest(
        identity=provider.resolve("alpha:8b"),
        messages=(Message(role=Role.USER, content=sim_prompt(label)),),
        runtime_profile=RuntimeProfile(),
        **kwargs,  # type: ignore[arg-type]  # timeout_seconds / cancel, passed through by tests
    )


def test_fake_clock_is_monotonic_and_settable() -> None:
    clock = FakeClock()
    assert clock.now() == DEFAULT_START
    assert clock.advance(90) == DEFAULT_START + timedelta(seconds=90)
    with pytest.raises(SimulationError):
        clock.set(DEFAULT_START)
    with pytest.raises(ValueError, match="aware"):
        FakeClock(datetime(2026, 1, 1))  # noqa: DTZ001 — the naive value is the point


def test_driver_dispatches_actions_in_time_then_insertion_order() -> None:
    clock = FakeClock()
    driver = Driver(clock)
    seen: list[tuple[str, datetime]] = []
    driver.schedule(DEFAULT_START + timedelta(seconds=5), lambda: seen.append(("b", clock.now())))
    driver.schedule(DEFAULT_START + timedelta(seconds=1), lambda: seen.append(("a", clock.now())))
    driver.schedule(DEFAULT_START + timedelta(seconds=5), lambda: seen.append(("c", clock.now())))
    driver.every(2.0, lambda: seen.append(("tick", clock.now())), first_at=DEFAULT_START)
    driver.run_for(5)
    labels = [label for label, _ in seen]
    assert labels == ["tick", "a", "tick", "tick", "b", "c"]
    assert seen[-1][1] == DEFAULT_START + timedelta(seconds=5)
    assert clock.now() == DEFAULT_START + timedelta(seconds=5)


def test_two_workers_sleeping_interleave_deterministically() -> None:
    """Only one thread runs at a time and the order is fixed by the clock."""
    clock = FakeClock()
    driver = Driver(clock)
    trace: list[tuple[str, int]] = []

    def worker(name: str, period: float) -> None:
        for _ in range(3):
            trace.append((name, int((clock.now() - DEFAULT_START).total_seconds())))
            driver.sleep(period)

    driver.add_worker("fast", lambda: worker("fast", 2.0))
    driver.add_worker("slow", lambda: worker("slow", 3.0))
    driver.run_for(10)
    assert trace == [
        ("fast", 0),
        ("slow", 0),
        ("fast", 2),
        ("slow", 3),
        ("fast", 4),
        ("slow", 6),
    ]
    assert driver.workers_finished()
    driver.stop()


def test_wakeup_returns_early_when_set_and_immediately_when_already_set() -> None:
    clock = FakeClock()
    driver = Driver(clock)
    wakeup = SimulatedWakeup(driver)
    results: list[tuple[bool, int]] = []

    def worker() -> None:
        woken = wakeup.wait(60.0)  # would block a minute; woken at t=10
        results.append((woken, int((clock.now() - DEFAULT_START).total_seconds())))
        wakeup.clear()
        woken = wakeup.wait(5.0)  # nothing sets it: times out at t=15
        results.append((woken, int((clock.now() - DEFAULT_START).total_seconds())))
        wakeup.set()  # set while "busy"...
        woken = wakeup.wait(100.0)  # ...so the next wait returns at once, no time passes
        results.append((woken, int((clock.now() - DEFAULT_START).total_seconds())))

    driver.add_worker("w", worker)
    driver.schedule(DEFAULT_START + timedelta(seconds=10), wakeup.set)
    driver.run_for(120)
    assert results == [(True, 10), (False, 15), (True, 15)]
    driver.stop()


def test_blocking_from_the_driver_thread_is_refused() -> None:
    driver = Driver(FakeClock())
    with pytest.raises(SimulationError, match="thread"):
        driver.sleep(1.0)


def test_a_worker_exception_surfaces_on_the_driver_thread() -> None:
    driver = Driver(FakeClock())

    def broken() -> None:
        driver.sleep(1.0)
        message = "worker blew up"
        raise RuntimeError(message)

    driver.add_worker("broken", broken)
    with pytest.raises(RuntimeError, match="blew up"):
        driver.run_for(5)
    driver.stop()


def test_stop_unwinds_a_blocked_worker() -> None:
    driver = Driver(FakeClock())
    exited: list[str] = []

    def worker() -> None:
        try:
            while True:
                driver.sleep(1.0)
        finally:
            exited.append("yes")

    driver.add_worker("loop", worker)
    driver.run_for(3)
    driver.stop()
    assert exited == ["yes"]
    assert driver.workers_finished()


def _provider(driver: Driver, **specs: GenerationSpec) -> SimulatedProvider:
    return SimulatedProvider(
        driver,
        models=(sim_model("alpha:8b", load_seconds=5.0),),
        specs={label: (spec,) for label, spec in specs.items()},
    )


def test_a_generation_takes_its_simulated_duration_and_loads_on_demand() -> None:
    clock = FakeClock()
    driver = Driver(clock)
    provider = _provider(driver, short=GenerationSpec(duration_seconds=10.0, chunks=4))
    events: list[object] = []
    stamps: list[float] = []

    def worker() -> None:
        for event in provider.stream(_request(provider, "short")):
            events.append(event)
            stamps.append((clock.now() - DEFAULT_START).total_seconds())

    driver.add_worker("w", worker)
    driver.run_for(60)
    assert provider.loads == 1  # a cold model was loaded first: 5 s
    assert [type(e).__name__ for e in events] == ["TokenDelta"] * 4 + ["StreamCompleted"]
    assert stamps == [7.5, 10.0, 12.5, 15.0, 15.0]  # 5 s load, then 2.5 s per chunk
    assert clock.now() == DEFAULT_START + timedelta(seconds=60)  # run_for advances to its end
    assert provider.resident_names() == frozenset({"alpha:8b"})
    # A second generation on the resident model spends no load time: first delta after one
    # chunk's share (2.5 s), the last 7.5 s after that.
    stamps.clear()
    events.clear()
    driver.add_worker("w2", worker)
    driver.run_for(60)
    assert provider.loads == 1
    assert stamps[0] == 62.5
    assert stamps[-1] - stamps[0] == 7.5
    driver.stop()


def test_cancellation_takes_effect_within_one_chunk_with_the_partial_text() -> None:
    clock = FakeClock()
    driver = Driver(clock)
    provider = _provider(driver, long=GenerationSpec(duration_seconds=100.0, chunks=10))
    token = CancellationToken()
    events: list[object] = []
    ended_at: list[datetime] = []

    def worker() -> None:
        events.extend(provider.stream(_request(provider, "long", cancel=token)))
        ended_at.append(clock.now())

    driver.add_worker("w", worker)
    # Load 5 s, first delta at t=15, second at t=25; the cancel lands at t=32, inside the sleep
    # before the third delta, and takes effect at that boundary (t=35) — within one chunk.
    driver.schedule(DEFAULT_START + timedelta(seconds=32), token.cancel)
    driver.run_for(200)
    assert isinstance(events[-1], StreamFailed)
    assert isinstance(events[-1].error, GenerationCancelled)
    assert len([e for e in events if isinstance(e, TokenDelta)]) == 2
    assert events[-1].partial_text == "long-0 long-1 "
    assert ended_at == [DEFAULT_START + timedelta(seconds=35)]  # not t=105, the natural end
    driver.stop()


def test_timeout_and_scripted_failures_arrive_as_the_fake_delivers_them() -> None:
    clock = FakeClock()
    driver = Driver(clock)
    provider = _provider(
        driver,
        slow=GenerationSpec(duration_seconds=400.0, chunks=4),
        dead=GenerationSpec(failure=FakeFailure(mode=FakeFailureMode.UNAVAILABLE, after_chunks=2)),
        gone=GenerationSpec(failure=FakeFailure(mode=FakeFailureMode.UNAVAILABLE)),
    )
    outcomes: dict[str, object] = {}

    def worker() -> None:
        outcomes["slow"] = list(provider.stream(_request(provider, "slow", timeout_seconds=300.0)))
        outcomes["dead"] = list(provider.stream(_request(provider, "dead")))
        try:
            list(provider.stream(_request(provider, "gone")))
        except ProviderUnavailable as exc:
            outcomes["gone"] = exc

    driver.add_worker("w", worker)
    driver.run_for(10_000)
    slow = outcomes["slow"]
    assert isinstance(slow, list) and isinstance(slow[-1], StreamFailed)
    assert isinstance(slow[-1].error, ProviderTimeout)
    dead = outcomes["dead"]
    assert isinstance(dead, list) and isinstance(dead[-1], StreamFailed)
    assert isinstance(dead[-1].error, ProviderUnavailable)
    assert len([e for e in dead if isinstance(e, TokenDelta)]) == 2
    assert isinstance(outcomes["gone"], ProviderUnavailable)
    driver.stop()


def test_per_label_scripts_are_consumed_in_order_and_the_last_repeats() -> None:
    driver = Driver(FakeClock())
    provider = SimulatedProvider(driver, models=(sim_model("alpha:8b", load_seconds=0.0),))
    provider.script(
        "flaky",
        GenerationSpec(
            duration_seconds=1.0,
            chunks=1,
            failure=FakeFailure(FakeFailureMode.TIMEOUT, after_chunks=0),
        ),
        GenerationSpec(duration_seconds=1.0, chunks=1, text="fine"),
    )
    kinds: list[str] = []

    def worker() -> None:
        for _ in range(3):
            last = list(provider.stream(_request(provider, "flaky")))[-1]
            kinds.append(type(last).__name__)

    driver.add_worker("w", worker)
    driver.run_for(100)
    assert kinds == ["StreamFailed", "StreamCompleted", "StreamCompleted"]
    driver.stop()


def test_simulation_builds_a_migrated_database_with_discovered_models(tmp_path: Path) -> None:
    simulation = Simulation(tmp_path, gpus=((0, 16 * 1024**3), (1, 8 * 1024**3)))
    try:
        snapshot = simulation.snapshot()
        assert [gpu.index for gpu in snapshot.gpus] == [0, 1]
        assert snapshot.timestamp == DEFAULT_START
        from loadcoach.services.models import list_registry

        assert [entry.provider_model_name for entry in list_registry(simulation.database)] == [
            "alpha:8b"
        ]
        # Loading a model occupies its device; unloading frees it.
        identity = simulation.provider.resolve("alpha:8b")

        def worker() -> None:
            simulation.provider.load(identity, RuntimeProfile())
            simulation.driver.sleep(1.0)
            simulation.provider.unload(identity)

        simulation.add_worker("w", worker)
        simulation.run_for(5.5)  # past the 5 s load: loaded, not yet unloaded
        assert simulation.gpus[0].used_bytes == 4 * 1024**3
        assert simulation.snapshot().gpus[0].vram_used_bytes == 4 * 1024**3
        simulation.run_for(1)
        assert simulation.gpus[0].used_bytes == 0
        assert simulation.provider.unloads == 1
    finally:
        simulation.close()


def test_the_same_scenario_produces_the_same_log_twice(tmp_path: Path) -> None:
    """Determinism is the property everything else rests on."""

    def run(path: Path) -> tuple[tuple[datetime, str], ...]:
        simulation = Simulation(path)
        try:
            provider = simulation.provider
            provider.script("job", GenerationSpec(duration_seconds=3.0, chunks=3))

            def worker(name: str) -> None:
                for _ in range(2):
                    list(provider.stream(_request(provider, "job")))
                    simulation.driver.sleep(0.5)

            simulation.add_worker("a", lambda: worker("a"))
            simulation.add_worker("b", lambda: worker("b"))
            simulation.attach_scheduler(lambda now: None, interval_seconds=1.0)
            simulation.run_for(30)
            return simulation.driver.log
        finally:
            simulation.close()

    first = run(tmp_path / "one")
    second = run(tmp_path / "two")
    assert first == second
    assert len(first) > 10
    assert first[0][0] == DEFAULT_START and first[0][0].tzinfo is UTC
