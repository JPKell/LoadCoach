"""The circuit breaker state machine over a window of samples (queue §7)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from loadcoach.domain.circuit_breaker import (
    AttemptSample,
    BreakerState,
    CircuitBreakers,
    evaluate,
)

T0 = datetime(2026, 8, 29, 12, 0, 0, tzinfo=UTC)


def _samples(
    *outcomes: bool, start: datetime = T0, step_seconds: float = 10.0
) -> list[AttemptSample]:
    return [
        AttemptSample(at=start + timedelta(seconds=step_seconds * i), succeeded=ok)
        for i, ok in enumerate(outcomes)
    ]


def test_below_the_minimum_sample_count_a_bad_rate_does_not_open() -> None:
    verdict = evaluate(
        "m", _samples(False, False, False, False), now=T0 + timedelta(minutes=1), previous=None
    )
    assert verdict.state is BreakerState.CLOSED
    assert verdict.failure_rate == 1.0 and verdict.samples == 4
    assert verdict.excludes is False


def test_the_breaker_opens_at_the_threshold_with_a_reason_and_an_expiry() -> None:
    now = T0 + timedelta(minutes=1)
    verdict = evaluate(
        "m", _samples(True, False, False, True, False, False), now=now, previous=None
    )
    assert verdict.state is BreakerState.OPEN
    assert verdict.failure_rate == 4 / 6
    assert verdict.opened_at == T0 + timedelta(seconds=50)
    assert verdict.expires_at == verdict.opened_at + timedelta(seconds=300)
    assert "4 of 6" in verdict.reason and "excluded until" in verdict.reason
    assert verdict.excludes is True
    assert verdict.as_json()["state"] == "open"


def test_samples_outside_the_window_are_forgotten() -> None:
    old = _samples(False, False, False, False, False, start=T0 - timedelta(hours=1))
    assert evaluate("m", old, now=T0, previous=None).state is BreakerState.CLOSED
    assert evaluate("m", old, now=T0).samples == 0


def test_open_excludes_until_the_cooldown_then_allows_one_probe() -> None:
    samples = _samples(False, False, False, False, False)
    opened = evaluate("m", samples, now=T0 + timedelta(minutes=1), previous=None)
    still = evaluate("m", samples, now=T0 + timedelta(minutes=4), previous=opened)
    assert still.state is BreakerState.OPEN and still.excludes
    half = evaluate("m", samples, now=opened.expires_at, previous=still)  # type: ignore[arg-type]
    assert half.state is BreakerState.HALF_OPEN
    assert half.excludes is False  # one probe may route to it
    assert half.reason == "cool-down elapsed; one probe allowed"


def test_a_successful_probe_closes_and_a_failed_one_reopens_with_a_fresh_cooldown() -> None:
    samples = _samples(False, False, False, False, False)
    opened = evaluate("m", samples, now=T0 + timedelta(minutes=1), previous=None)
    assert opened.expires_at is not None
    half = evaluate("m", samples, now=opened.expires_at, previous=opened)
    probe_ok = samples + [
        AttemptSample(at=opened.expires_at + timedelta(seconds=5), succeeded=True)
    ]
    closed = evaluate("m", probe_ok, now=opened.expires_at + timedelta(seconds=6), previous=half)
    assert closed.state is BreakerState.CLOSED and closed.opened_at is None
    assert closed.closed_at == opened.expires_at + timedelta(seconds=5)
    # The failures that opened it no longer count: only samples after the close do.
    assert closed.samples == 0 and closed.failure_rate is None
    again = evaluate("m", probe_ok, now=opened.expires_at + timedelta(minutes=1), previous=closed)
    assert again.state is BreakerState.CLOSED
    probe_bad = samples + [
        AttemptSample(at=opened.expires_at + timedelta(seconds=5), succeeded=False)
    ]
    reopened = evaluate("m", probe_bad, now=opened.expires_at + timedelta(seconds=6), previous=half)
    assert reopened.state is BreakerState.OPEN
    assert reopened.opened_at == opened.expires_at + timedelta(seconds=5)
    assert reopened.expires_at == reopened.opened_at + timedelta(seconds=300)


def test_the_registry_tracks_models_and_lets_one_probe_through_at_a_time() -> None:
    breakers = CircuitBreakers(cooldown_seconds=60)
    samples = {"bad": _samples(False, False, False, False, False), "good": _samples(True, True)}
    breakers.update(samples, now=T0 + timedelta(minutes=1))
    assert breakers.excluded() == frozenset({"bad"})
    assert set(breakers.details()) == {"bad"}
    assert breakers.allow_probe("bad", now=T0 + timedelta(minutes=1)) is False  # still open
    breakers.update(samples, now=T0 + timedelta(minutes=3))
    assert breakers.excluded() == frozenset()  # half-open: a probe may route to it
    assert breakers.allow_probe("bad", now=T0 + timedelta(minutes=3)) is True
    assert breakers.allow_probe("bad", now=T0 + timedelta(minutes=3)) is False  # one at a time
    assert breakers.excluded() == frozenset({"bad"})  # excluded while the probe is out
    assert breakers.allow_probe("good", now=T0 + timedelta(minutes=3)) is False
    assert {v.canonical_id: v.state for v in breakers.verdicts()} == {
        "bad": BreakerState.HALF_OPEN,
        "good": BreakerState.CLOSED,
    }


def test_a_cancelled_probe_is_handed_back_so_the_next_job_may_probe() -> None:
    """A probe that never reported must not exclude the model until never (P7's failure mode)."""
    breakers = CircuitBreakers(cooldown_seconds=60)
    samples = {"bad": _samples(False, False, False, False, False)}
    breakers.update(samples, now=T0 + timedelta(minutes=1))
    breakers.update(samples, now=T0 + timedelta(minutes=3))
    at = T0 + timedelta(minutes=3)
    assert breakers.allow_probe("bad", now=at) is True
    assert breakers.excluded() == frozenset({"bad"})
    verdict = next(v for v in breakers.verdicts() if v.canonical_id == "bad")
    assert verdict.probe_started_at == at and verdict.reason == "cool-down elapsed; probe in flight"
    assert verdict.as_json()["probe_started_at"] is not None
    assert breakers.release_probe("bad") is True
    assert breakers.release_probe("bad") is False
    assert breakers.excluded() == frozenset()
    assert breakers.allow_probe("bad", now=at) is True  # the next job probes instead


def test_a_probe_that_never_reports_is_released_after_one_cooldown() -> None:
    """A crashed probe worker cannot leave the breaker half-open-and-busy for ever."""
    breakers = CircuitBreakers(cooldown_seconds=60)
    samples = {"bad": _samples(False, False, False, False, False)}
    breakers.update(samples, now=T0 + timedelta(minutes=1))
    breakers.update(samples, now=T0 + timedelta(minutes=3))
    assert breakers.allow_probe("bad", now=T0 + timedelta(minutes=3)) is True
    breakers.update(samples, now=T0 + timedelta(minutes=3, seconds=59))
    assert breakers.excluded() == frozenset({"bad"})  # still waiting on the probe
    breakers.update(samples, now=T0 + timedelta(minutes=4))
    assert breakers.excluded() == frozenset()  # presumed lost; another probe may go
    assert next(v for v in breakers.verdicts()).state is BreakerState.HALF_OPEN
