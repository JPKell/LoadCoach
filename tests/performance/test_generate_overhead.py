"""P4's overhead budget: ≤ 15 ms of LoadCoach's own work per generation, excluding provider time.

Marked ``performance`` and excluded from the default gate, like every budget assertion in the
suite. Overhead is measured as wall time minus the time spent inside the provider call — the two
figures the executor keeps separate precisely so this number means something. Conflating them is
the failure mode this phase's plan names, and it would make this test pass for free.
"""

from __future__ import annotations

import statistics
import time
from pathlib import Path
from typing import Any

import pytest
from modelrack.testing import FakeGeneration, FakeScript
from tests.integration.test_generate import _context, _model, _setup

from loadcoach.services.execution import GenerateRequest, execute

OVERHEAD_BUDGET_MS = 15
_WARMUP = 3
_MEASURED = 20

pytestmark = pytest.mark.performance


class _SlowProvider:
    """A provider that really sleeps inside ``stream``.

    The scripted delays on :class:`~modelrack.testing.FakeGeneration` describe the timings a
    generation *reports*; they do not make the fake take wall time, which is exactly what this
    test needs. Sleeping here is the instrument, and it is deliberately in the one place the
    executor measures as provider time.
    """

    def __init__(self, inner: Any, *, delay_seconds: float) -> None:
        self._inner = inner
        self._delay = delay_seconds

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def stream(self, request: Any) -> Any:
        time.sleep(self._delay)
        yield from self._inner.stream(request)


def test_loadcoach_overhead_stays_within_its_budget(tmp_path: Path) -> None:
    script = FakeScript(
        models=(_model(),),
        generations=(FakeGeneration(text="a short answer"),),
        repeat_final_generation=True,
    )
    database, provider = _setup(tmp_path, script)
    context = _context(provider)
    request = GenerateRequest(task="general.chat", prompt="hello")

    overheads: list[int] = []
    try:
        for index in range(_WARMUP + _MEASURED):
            outcome = execute(database, request, context)
            if index >= _WARMUP:
                overheads.append(outcome.overhead_ms)
                # The two figures are separate, and the provider's is not folded into ours.
                assert outcome.provider_ms >= 0
                assert outcome.total_ms >= outcome.provider_ms
    finally:
        database.close()

    median = statistics.median(overheads)
    worst = max(overheads)
    print(  # noqa: T201 — the measured number is this test's whole output
        f"\nloadcoach_overhead_ms over {_MEASURED} runs: "
        f"median {median:.1f}, p95 {sorted(overheads)[int(_MEASURED * 0.95) - 1]}, max {worst}"
    )
    assert median <= OVERHEAD_BUDGET_MS, (
        f"median overhead {median} ms exceeds {OVERHEAD_BUDGET_MS} ms"
    )


def test_the_budget_is_measured_against_a_provider_that_actually_takes_time(
    tmp_path: Path,
) -> None:
    """A budget met only because the fake returns instantly is not a budget.

    With a provider that takes ~100 ms, the overhead figure must stay the same: it is wall time
    minus provider time, so a slower provider moves ``total_ms`` and leaves overhead alone.
    """
    script = FakeScript(
        models=(_model(),),
        generations=(FakeGeneration(text="a short answer"),),
        repeat_final_generation=True,
    )
    database, fast = _setup(tmp_path, script)
    provider = _SlowProvider(fast, delay_seconds=0.08)
    context: Any = _context(provider)
    try:
        outcome = execute(database, GenerateRequest(task="general.chat", prompt="hello"), context)
    finally:
        database.close()
    assert outcome.provider_ms >= 75, outcome.provider_ms
    assert outcome.overhead_ms <= OVERHEAD_BUDGET_MS * 4, (
        f"overhead {outcome.overhead_ms} ms grew with provider time, so the two are conflated"
    )
    print(  # noqa: T201 — the measured numbers are this test's whole output
        f"\nslow provider: total {outcome.total_ms} ms, provider {outcome.provider_ms} ms, "
        f"overhead {outcome.overhead_ms} ms"
    )
