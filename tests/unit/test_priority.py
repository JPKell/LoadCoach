"""Priority bands and the ageing arithmetic (queue §1, §4)."""

from __future__ import annotations

import pytest
from baseaicore import ValidationError

from loadcoach.domain.priority import (
    BANDS,
    JobClass,
    ageing_cap,
    band_of,
    base_priority,
    effective_priority,
    starvation_threshold_seconds,
)


def test_bands_are_the_documented_ones_and_do_not_overlap() -> None:
    assert BANDS[JobClass.INTERACTIVE] == (800, 999)
    assert BANDS[JobClass.NORMAL] == (400, 799)
    assert BANDS[JobClass.BACKGROUND] == (100, 399)
    assert BANDS[JobClass.BATCH] == (0, 99)
    ordered = sorted(BANDS.values())
    for (_, top), (bottom, _) in zip(ordered, ordered[1:], strict=False):
        assert top < bottom


def test_base_priority_defaults_to_the_band_bottom() -> None:
    for job_class, (bottom, _) in BANDS.items():
        assert base_priority(job_class) == bottom


@pytest.mark.parametrize("requested", [400, 600, 799])
def test_a_priority_inside_the_band_is_kept(requested: int) -> None:
    assert base_priority(JobClass.NORMAL, requested) == requested


@pytest.mark.parametrize("requested", [399, 800, -1, 1000])
def test_a_priority_outside_the_band_is_refused_with_the_band_named(requested: int) -> None:
    """The band is not escapable: a caller cannot buy an interactive slot for a normal job."""
    with pytest.raises(ValidationError) as excinfo:
        base_priority(JobClass.NORMAL, requested)
    assert excinfo.value.details["band"] == [400, 799]
    assert excinfo.value.details["requested"] == requested


def _aged_background(waiting_seconds: float) -> int:
    """A background job at the band bottom, under the shipped ageing defaults."""
    return effective_priority(
        base=100,
        job_class=JobClass.BACKGROUND,
        waiting_seconds=waiting_seconds,
        ageing_priority_per_minute=1.0,
        overflow_allowance=100,
    )


def test_effective_priority_rises_one_point_per_minute_by_default() -> None:
    assert _aged_background(0) == 100
    assert _aged_background(59) == 100  # floor, not round
    assert _aged_background(60) == 101
    assert _aged_background(300 * 60) == 400  # ties a fresh normal job
    assert _aged_background(301 * 60) == 401  # outranks it


def test_effective_priority_is_capped_at_band_top_plus_overflow() -> None:
    assert ageing_cap(JobClass.BACKGROUND, overflow_allowance=100) == 499
    assert (
        effective_priority(
            base=100,
            job_class=JobClass.BACKGROUND,
            waiting_seconds=10**7,
            ageing_priority_per_minute=1.0,
            overflow_allowance=100,
        )
        == 499
    )
    # Never a fresh interactive one (queue §4).
    assert (
        ageing_cap(JobClass.BACKGROUND, overflow_allowance=100) < band_of(JobClass.INTERACTIVE)[0]
    )


def test_a_clock_that_stepped_backwards_ages_nothing() -> None:
    assert (
        effective_priority(
            base=400,
            job_class=JobClass.NORMAL,
            waiting_seconds=-3600,
            ageing_priority_per_minute=1.0,
            overflow_allowance=100,
        )
        == 400
    )


def test_zero_ageing_rate_leaves_priority_at_base() -> None:
    assert (
        effective_priority(
            base=50,
            job_class=JobClass.BATCH,
            waiting_seconds=10**6,
            ageing_priority_per_minute=0.0,
            overflow_allowance=100,
        )
        == 50
    )


def test_starvation_threshold_is_half_the_jobs_own_bound() -> None:
    assert starvation_threshold_seconds(3600) == 1800.0
    assert starvation_threshold_seconds(10) == 5.0
