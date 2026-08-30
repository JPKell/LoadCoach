"""The pure statistics behind ``reliability_stats``: every number here was worked out by hand.

Not one expected value in this file was produced by running the implementation. A wrong
agreement statistic produces *plausible* numbers and stays wrong for months; hand-computed
fixtures are the only thing that catches it (development plan P7, unit 1).
"""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta

import pytest

from loadcoach.domain.reliability import (
    FACTOR_WINDOWS,
    MINIMUM_PERCENTILE_SAMPLES,
    PRODUCTION_MINIMUM_SAMPLES,
    REGRESSION_MINIMUM_SAMPLES,
    RELIABILITY_FACTOR_FLOOR,
    WINDOW_7D,
    WINDOW_30D,
    WINDOW_ALL,
    WINDOWS,
    AttemptOutcome,
    FeedbackOutcome,
    ReliabilityLedger,
    WindowStats,
    acceptance_credit,
    compute_stats,
    counts_as_success,
    detect_regression,
    in_window,
    neutral_factor,
    percentile,
    reliability_factor,
    window_named,
)

T0 = datetime(2026, 8, 1, 0, 0, 0, tzinfo=UTC)
NOW = T0 + timedelta(hours=1)


def _attempts(
    *outcomes: str,
    latency_ms: int | None = None,
    output_tokens: int | None = None,
    start: datetime = T0,
    step_seconds: float = 1.0,
) -> list[AttemptOutcome]:
    return [
        AttemptOutcome(
            at=start + timedelta(seconds=step_seconds * index),
            outcome=outcome,
            latency_ms=latency_ms,
            output_tokens=output_tokens,
        )
        for index, outcome in enumerate(outcomes)
    ]


# ------------------------------------------------------------------------- classification


@pytest.mark.parametrize(
    ("outcome", "success"),
    [
        ("completed", True),
        ("validation_failed", True),  # the provider answered; what it said was wrong
        ("provider_error", False),
        ("timeout", False),
        ("context_exceeded", False),
        ("cancelled", False),
    ],
)
def test_the_success_rule_is_the_breakers_rule(outcome: str, success: bool) -> None:
    assert counts_as_success(outcome) is success


@pytest.mark.parametrize(
    ("feedback", "credit"),
    [
        (FeedbackOutcome(at=T0, accepted=True), 1.0),
        (FeedbackOutcome(at=T0, accepted=True, edited=True), 0.5),
        (FeedbackOutcome(at=T0, accepted=False), 0.0),
        (FeedbackOutcome(at=T0, accepted=False, edited=True), 0.0),
        (FeedbackOutcome(at=T0, accepted=True, validation_passed=False), 0.0),
        (FeedbackOutcome(at=T0, accepted=True, validation_passed=True), 1.0),
    ],
)
def test_acceptance_credit_table(feedback: FeedbackOutcome, credit: float) -> None:
    assert acceptance_credit(feedback) == credit


# ----------------------------------------------------------------------------- the window


def test_window_boundary_is_strict_below_and_inclusive_above() -> None:
    now = T0 + timedelta(days=10)
    horizon = now - timedelta(days=7)
    assert in_window(horizon, window=WINDOW_7D, now=now) is False
    assert in_window(horizon + timedelta(microseconds=1), window=WINDOW_7D, now=now) is True
    assert in_window(now, window=WINDOW_7D, now=now) is True
    assert in_window(now + timedelta(microseconds=1), window=WINDOW_7D, now=now) is False
    assert in_window(T0 - timedelta(days=400), window=WINDOW_ALL, now=now) is True
    assert in_window(now + timedelta(seconds=1), window=WINDOW_ALL, now=now) is False


def test_windows_are_named_and_ordered_freshest_first() -> None:
    assert [w.name for w in WINDOWS] == ["7d", "30d", "all"]
    assert window_named("30d") is WINDOW_30D
    assert WINDOW_7D.seconds == 7 * 86_400 and WINDOW_ALL.seconds is None
    with pytest.raises(ValueError, match="unknown reliability window"):
        window_named("90d")


# --------------------------------------------------------------------------- percentiles


def test_nearest_rank_percentiles_by_hand() -> None:
    twenty = [100 * i for i in range(1, 21)]  # 100 … 2000
    assert percentile(twenty, 0.50) == 1000  # ceil(10) = 10th
    assert percentile(twenty, 0.95) == 1900  # ceil(19) = 19th
    assert percentile(twenty, 1.0) == 2000
    twenty_one = [100 * i for i in range(1, 22)]  # 100 … 2100
    assert percentile(twenty_one, 0.50) == 1100  # ceil(10.5) = 11th
    assert percentile(twenty_one, 0.95) == 2000  # ceil(19.95) = 20th
    assert percentile([5, 1, 3], 0.5) == 3  # order does not matter
    with pytest.raises(ValueError, match="empty"):
        percentile([], 0.5)
    with pytest.raises(ValueError, match="in \\(0, 1\\]"):
        percentile([1], 0.0)


# ------------------------------------------------------------------------------ counts


def _fixture_a() -> tuple[list[AttemptOutcome], list[FeedbackOutcome]]:
    """Twenty attempts and six verdicts, chosen so every count and rate is a short fraction."""
    attempts = (
        _attempts(*["completed"] * 12, latency_ms=1000, output_tokens=50)
        + _attempts(
            *["validation_failed"] * 4,
            latency_ms=2000,
            output_tokens=40,
            start=T0 + timedelta(minutes=1),
        )
        + _attempts("provider_error", "context_exceeded", start=T0 + timedelta(minutes=2))
        + _attempts("timeout", start=T0 + timedelta(minutes=3))
        + _attempts("cancelled", start=T0 + timedelta(minutes=4))
    )
    feedback = [
        FeedbackOutcome(at=T0, accepted=True, quality_score=0.8),
        FeedbackOutcome(at=T0, accepted=True, edited=True, quality_score=0.6),
        FeedbackOutcome(at=T0, accepted=False),
        FeedbackOutcome(at=T0, accepted=True, validation_passed=False, quality_score=0.2),
        FeedbackOutcome(at=T0, accepted=True, quality_score=1.0),
        FeedbackOutcome(at=T0, accepted=True),
    ]
    return attempts, feedback


def test_counts_and_rates_by_hand() -> None:
    attempts, feedback = _fixture_a()
    stats = compute_stats(attempts, feedback, window=WINDOW_ALL, now=NOW)

    assert stats.attempts == 20
    assert stats.cancellations == 1
    assert stats.counted == 19
    assert stats.successes == 16  # 12 completed + 4 validation_failed
    assert stats.validation_passes == 12
    assert stats.errors == 2  # provider_error + context_exceeded
    assert stats.timeouts == 1
    assert stats.failures == 3

    assert stats.success_rate().value == pytest.approx(16 / 19)
    assert stats.validation_pass_rate().value == pytest.approx(12 / 16)
    assert stats.validated_success_rate().value == pytest.approx(12 / 19)
    assert stats.error_rate().value == pytest.approx(2 / 19)
    assert stats.timeout_rate().value == pytest.approx(1 / 19)
    assert stats.success_rate().samples == 19
    assert stats.validation_pass_rate().samples == 16

    # 16 latencies: below the percentile bound, so absent — with the count and the bound stated.
    assert stats.latency_count == 16
    assert stats.p50_latency_ms is None and stats.p95_latency_ms is None
    assert stats.p95().reason == f"16 sample(s); {MINIMUM_PERCENTILE_SAMPLES} needed"

    # Output tokens: 12 × 50 + 4 × 40 = 760 over 16 attempts.
    assert stats.output_token_count == 16
    assert stats.mean_output_tokens == pytest.approx(760 / 16)
    # Throughput: 50 tok / 1 s twelve times, 40 tok / 2 s four times → (600 + 80) / 16.
    assert stats.tokens_per_second_count == 16
    assert stats.mean_tokens_per_second == pytest.approx(680 / 16)

    # Acceptance credits: 1 + 0.5 + 0 + 0 + 1 + 1 = 3.5 over six verdicts.
    assert stats.feedback_count == 6
    assert stats.acceptance_rate == pytest.approx(3.5 / 6)
    # Four quality scores is below the mean bound.
    assert stats.quality_count == 4
    assert stats.mean_quality is None
    assert stats.quality().reason == "4 sample(s); 5 needed"

    document = stats.as_json()
    assert document["counted"] == 19
    assert document["success_rate"] == {
        "value": pytest.approx(16 / 19),
        "samples": 19,
        "minimum": 5,
        "reason": None,
    }
    assert document["mean_quality"]["value"] is None
    assert document["mean_quality"]["reason"] == "4 sample(s); 5 needed"


def test_rates_are_absent_below_five_counted_attempts() -> None:
    stats = compute_stats(
        _attempts("completed", "completed", "provider_error", "provider_error"),
        window=WINDOW_ALL,
        now=NOW,
    )
    assert stats.counted == 4
    assert stats.success_rate().value is None
    assert stats.success_rate().reason == "4 sample(s); 5 needed"
    assert stats.error_rate().value is None
    five = compute_stats(_attempts(*["completed"] * 4, "timeout"), window=WINDOW_ALL, now=NOW)
    assert five.success_rate().value == pytest.approx(0.8)
    assert five.timeout_rate().value == pytest.approx(0.2)


def test_percentiles_appear_at_exactly_twenty_latencies() -> None:
    def latencies(count: int) -> list[AttemptOutcome]:
        return [
            AttemptOutcome(at=T0 + timedelta(seconds=i), outcome="completed", latency_ms=100 * i)
            for i in range(1, count + 1)
        ]

    nineteen = compute_stats(latencies(19), window=WINDOW_ALL, now=NOW)
    assert nineteen.latency_count == 19 and nineteen.p50_latency_ms is None
    twenty = compute_stats(latencies(20), window=WINDOW_ALL, now=NOW)
    assert (twenty.p50_latency_ms, twenty.p95_latency_ms) == (1000, 1900)
    twenty_one = compute_stats(latencies(21), window=WINDOW_ALL, now=NOW)
    assert (twenty_one.p50_latency_ms, twenty_one.p95_latency_ms) == (1100, 2000)


def test_throughput_needs_both_inputs_and_a_positive_latency() -> None:
    assert (
        AttemptOutcome(at=T0, outcome="completed", latency_ms=0, output_tokens=5).tokens_per_second
        is None
    )
    assert AttemptOutcome(at=T0, outcome="completed", latency_ms=500).tokens_per_second is None
    assert AttemptOutcome(at=T0, outcome="completed", output_tokens=5).tokens_per_second is None
    assert AttemptOutcome(
        at=T0, outcome="completed", latency_ms=500, output_tokens=25
    ).tokens_per_second == pytest.approx(50.0)


def test_only_samples_inside_the_window_count() -> None:
    now = T0 + timedelta(days=40)
    old = _attempts(*["provider_error"] * 6, start=T0)  # 40 days ago: 30d and 7d forget it
    mid = _attempts(*["timeout"] * 6, start=now - timedelta(days=20))  # 30d and all only
    recent = _attempts(*["completed"] * 6, start=now - timedelta(days=1))
    everything = old + mid + recent
    seven = compute_stats(everything, window=WINDOW_7D, now=now)
    thirty = compute_stats(everything, window=WINDOW_30D, now=now)
    all_time = compute_stats(everything, window=WINDOW_ALL, now=now)
    assert (seven.attempts, seven.successes, seven.timeouts, seven.errors) == (6, 6, 0, 0)
    assert (thirty.attempts, thirty.successes, thirty.timeouts, thirty.errors) == (12, 6, 6, 0)
    assert (all_time.attempts, all_time.successes, all_time.timeouts, all_time.errors) == (
        18,
        6,
        6,
        6,
    )


# ------------------------------------------------------------------ incremental == full


def _random_events(
    rng: random.Random, *, count: int, span_hours: int
) -> tuple[list[AttemptOutcome], list[FeedbackOutcome]]:
    outcomes = (
        "completed",
        "completed",
        "completed",
        "validation_failed",
        "provider_error",
        "timeout",
        "context_exceeded",
        "cancelled",
    )
    attempts = [
        AttemptOutcome(
            at=T0 + timedelta(hours=rng.randrange(span_hours + 1)),
            outcome=rng.choice(outcomes),
            latency_ms=rng.choice([None, 250, 800, 1500, 4000]),
            output_tokens=rng.choice([None, 10, 120, 600]),
        )
        for _ in range(count)
    ]
    feedback = [
        FeedbackOutcome(
            at=T0 + timedelta(hours=rng.randrange(span_hours + 1)),
            accepted=rng.random() < 0.7,
            quality_score=rng.choice([None, 0.1, 0.5, 0.9]),
            edited=rng.random() < 0.3,
            validation_passed=rng.choice([None, True, False]),
        )
        for _ in range(count // 4)
    ]
    return attempts, feedback


@pytest.mark.parametrize("seed", [1, 2, 3, 4, 5])
def test_incremental_fold_equals_full_recomputation_on_every_field(seed: int) -> None:
    """The property, not an example: fold events in time order, trimming as the clock moves,
    and the ledger's statistics must equal a from-scratch computation at every checkpoint —
    including samples sitting exactly on the 7-day and 30-day boundaries, which are planted."""
    rng = random.Random(seed)  # noqa: S311 — reproducible inputs, not cryptography
    span_hours = 40 * 24
    final = T0 + timedelta(hours=span_hours)
    attempts, feedback = _random_events(rng, count=240, span_hours=span_hours)
    # Boundary plants: exactly on each horizon as of ``final``, exactly at ``final``, and after.
    for delta in (timedelta(days=7), timedelta(days=30), timedelta(0), -timedelta(hours=3)):
        attempts.append(AttemptOutcome(at=final - delta, outcome="provider_error", latency_ms=99))
        feedback.append(FeedbackOutcome(at=final - delta, accepted=False))

    events: list[tuple[datetime, AttemptOutcome | FeedbackOutcome]] = [
        *((a.at, a) for a in attempts),
        *((f.at, f) for f in feedback),
    ]
    events.sort(key=lambda item: item[0])
    checkpoints = sorted(rng.sample(range(len(events)), 6)) + [len(events)]

    ledgers = {window.name: ReliabilityLedger(window) for window in WINDOWS}
    seen_attempts: list[AttemptOutcome] = []
    seen_feedback: list[FeedbackOutcome] = []
    for index, (at, event) in enumerate(events, start=1):
        now = min(at, final)
        if isinstance(event, AttemptOutcome):
            seen_attempts.append(event)
            for name, ledger in ledgers.items():
                ledgers[name] = ledger.with_attempt(event).trimmed(now=now)
        else:
            seen_feedback.append(event)
            for name, ledger in ledgers.items():
                ledgers[name] = ledger.with_feedback(event).trimmed(now=now)
        if index in checkpoints:
            for window in WINDOWS:
                incremental = ledgers[window.name].stats(now=now)
                full = compute_stats(seen_attempts, seen_feedback, window=window, now=now)
                assert incremental == full, (seed, index, window.name)
    for window in WINDOWS:
        assert ledgers[window.name].stats(now=final) == compute_stats(
            attempts, feedback, window=window, now=final
        ), (seed, window.name)


def test_the_ledger_keeps_future_samples_until_the_clock_reaches_them() -> None:
    ledger = ReliabilityLedger(WINDOW_7D).with_attempt(
        AttemptOutcome(at=T0 + timedelta(days=1), outcome="completed")
    )
    assert ledger.trimmed(now=T0).stats(now=T0).attempts == 0
    assert ledger.trimmed(now=T0).attempts  # not forgotten
    assert ledger.stats(now=T0 + timedelta(days=1)).attempts == 1


# ------------------------------------------------------------------------------- factor


def _stats(
    window: str = "7d",
    *,
    completed: int = 0,
    validation_failed: int = 0,
    errors: int = 0,
    timeouts: int = 0,
    cancelled: int = 0,
    feedback_count: int = 0,
    acceptance_rate: float | None = None,
) -> WindowStats:
    return WindowStats(
        window=window,
        attempts=completed + validation_failed + errors + timeouts + cancelled,
        successes=completed + validation_failed,
        validation_passes=completed,
        errors=errors,
        timeouts=timeouts,
        cancellations=cancelled,
        feedback_count=feedback_count,
        acceptance_rate=acceptance_rate,
    )


def test_factor_boundary_at_n_minus_one_n_and_n_plus_one() -> None:
    below = reliability_factor({"7d": _stats(completed=15, errors=4)})  # 19 counted
    assert below.value == 1.0 and below.neutral and below.window is None
    assert f"fewer than {PRODUCTION_MINIMUM_SAMPLES}" in below.reason and "7d=19" in below.reason

    exact = reliability_factor({"7d": _stats(completed=20)})  # 20 counted, all good
    assert exact.value == 1.0 and not exact.neutral and exact.window == "7d"
    assert exact.attempts == 20 and exact.success_rate == 1.0

    # 21 counted: 14 completed, 4 validation failures, 2 errors, 1 timeout.
    # success = 18/21, validation = 14/18 → 0.5 + 0.5 × (18/21 × 14/18) = 0.5 + 0.5 × 2/3.
    above = reliability_factor(
        {"7d": _stats(completed=14, validation_failed=4, errors=2, timeouts=1, cancelled=3)}
    )
    assert above.attempts == 21  # cancellations never count
    assert above.value == pytest.approx(0.5 + 0.5 * (2 / 3))
    assert above.success_rate == pytest.approx(18 / 21)
    assert above.validation_pass_rate == pytest.approx(14 / 18)
    assert above.error_rate == pytest.approx(2 / 21)
    assert above.timeout_rate == pytest.approx(1 / 21)
    assert above.acceptance_rate is None and above.feedback_count == 0
    assert "18 of 21 attempts answered (86%)" in above.reason
    assert "14 of 18 validated (78%)" in above.reason


def test_factor_is_bounded_by_the_floor_and_never_clamped_from_outside() -> None:
    all_errors = reliability_factor({"7d": _stats(errors=20)})
    assert all_errors.value == RELIABILITY_FACTOR_FLOOR
    assert all_errors.validation_pass_rate is None  # nothing answered, so nothing to validate
    all_wrong = reliability_factor({"7d": _stats(validation_failed=20)})
    assert all_wrong.value == RELIABILITY_FACTOR_FLOOR
    rejected = reliability_factor(
        {"7d": _stats(completed=20, feedback_count=5, acceptance_rate=0.0)}
    )
    # Unanimous rejection moves the factor halfway to the floor and no further.
    assert rejected.value == pytest.approx(0.75)


def test_feedback_folds_in_with_its_own_weight_and_only_above_its_own_bound() -> None:
    # 21 counted as above (product 2/3); five verdicts at 50 % acceptance → term 0.75.
    with_feedback = reliability_factor(
        {
            "7d": _stats(
                completed=14,
                validation_failed=4,
                errors=2,
                timeouts=1,
                feedback_count=5,
                acceptance_rate=0.5,
            )
        }
    )
    assert with_feedback.value == pytest.approx(0.5 + 0.5 * (2 / 3) * 0.75)
    assert with_feedback.acceptance_rate == 0.5 and with_feedback.feedback_count == 5
    assert "caller acceptance 50% over 5 verdicts" in with_feedback.reason
    four_verdicts = reliability_factor(
        {"7d": _stats(completed=20, feedback_count=4, acceptance_rate=0.0)}
    )
    assert four_verdicts.value == 1.0 and four_verdicts.acceptance_rate is None


def test_factor_uses_the_freshest_window_with_enough_samples() -> None:
    factor = reliability_factor(
        {
            "7d": _stats("7d", completed=5),
            "30d": _stats("30d", completed=20, errors=5),
            "all": _stats("all", completed=100),
        }
    )
    assert factor.window == "30d" and factor.attempts == 25
    assert factor.value == pytest.approx(0.5 + 0.5 * 0.8)
    assert reliability_factor({}).neutral
    assert neutral_factor().reason.startswith("neutral: fewer than 20 counted attempts in the last")
    document = factor.as_json()
    assert document["source"] == "production" and document["minimum_samples"] == 20
    assert document["neutral"] is False


# --------------------------------------------------------------------------- regression


def _pair(
    *, baseline_n: int, baseline_passes: int, recent_n: int, recent_passes: int
) -> tuple[WindowStats, WindowStats]:
    recent = _stats("7d", completed=recent_passes, errors=recent_n - recent_passes)
    all_time = _stats(
        "all",
        completed=recent_passes + baseline_passes,
        errors=(recent_n - recent_passes) + (baseline_n - baseline_passes),
    )
    return recent, all_time


def test_regression_fires_on_a_synthetic_degradation_with_the_hand_computed_z() -> None:
    # Baseline 90 of 100; recent 18 of 30. drop = 0.30; pooled p = 108/130.
    # se = sqrt(0.830769 × 0.169231 × (1/30 + 1/100)) = sqrt(0.0060923) = 0.078053; z = 3.84.
    recent, all_time = _pair(baseline_n=100, baseline_passes=90, recent_n=30, recent_passes=18)
    verdict = detect_regression(recent=recent, all_time=all_time)
    assert verdict.status == "regressed" and verdict.regressed
    assert verdict.baseline_samples == 100 and verdict.recent_samples == 30
    assert verdict.baseline_rate == pytest.approx(0.9)
    assert verdict.recent_rate == pytest.approx(0.6)
    assert verdict.drop == pytest.approx(0.3)
    assert verdict.z_score == pytest.approx(3.8435, abs=0.001)
    assert verdict.reason.startswith("regression: validated success 60% over 30 recent attempts")
    assert verdict.as_json()["minimum_drop"] == 0.15


def test_regression_does_not_fire_on_noise_with_the_same_mean() -> None:
    # Baseline 90 of 100; recent 26 of 30 (87 %): a three-point wobble.
    recent, all_time = _pair(baseline_n=100, baseline_passes=90, recent_n=30, recent_passes=26)
    verdict = detect_regression(recent=recent, all_time=all_time)
    assert verdict.status == "stable"
    assert verdict.drop == pytest.approx(0.9 - 26 / 30)
    assert verdict.reason.startswith("stable:")


def test_regression_needs_both_an_absolute_drop_and_significance() -> None:
    # Significant but tiny: 4 500 of 5 000 → 1 700 of 2 000 is a five-point drop with a huge z.
    recent, all_time = _pair(
        baseline_n=5000, baseline_passes=4500, recent_n=2000, recent_passes=1700
    )
    tiny = detect_regression(recent=recent, all_time=all_time)
    assert tiny.status == "stable" and tiny.z_score is not None and tiny.z_score > 2.0
    assert tiny.drop == pytest.approx(0.05)
    # Large but not significant: 18 of 20 → 15 of 20. drop = 0.15; pooled 33/40 = 0.825;
    # se = sqrt(0.825 × 0.175 × 0.1) = 0.12016; z = 1.25.
    recent, all_time = _pair(baseline_n=20, baseline_passes=18, recent_n=20, recent_passes=15)
    weak = detect_regression(recent=recent, all_time=all_time)
    assert weak.status == "stable"
    assert weak.drop == pytest.approx(0.15)
    assert weak.z_score == pytest.approx(1.2483, abs=0.001)


def test_regression_is_not_evaluated_below_the_sample_bound_on_either_side() -> None:
    recent, all_time = _pair(baseline_n=100, baseline_passes=90, recent_n=19, recent_passes=5)
    verdict = detect_regression(recent=recent, all_time=all_time)
    assert verdict.status == "insufficient_samples" and not verdict.regressed
    assert verdict.recent_samples == 19 and verdict.baseline_samples == 100
    assert verdict.drop is None and verdict.z_score is None
    assert f"{REGRESSION_MINIMUM_SAMPLES} of each needed" in verdict.reason
    recent, all_time = _pair(baseline_n=19, baseline_passes=19, recent_n=40, recent_passes=5)
    assert detect_regression(recent=recent, all_time=all_time).status == "insufficient_samples"
    # Exactly at the bound on both sides it is evaluated.
    recent, all_time = _pair(baseline_n=20, baseline_passes=20, recent_n=20, recent_passes=5)
    assert detect_regression(recent=recent, all_time=all_time).status == "regressed"


def test_regression_over_random_series_fires_on_degradation_and_not_on_stationary_noise() -> None:
    """Two seeded Bernoulli series through the real window arithmetic, not hand-built counts."""
    now = T0 + timedelta(days=60)

    def series(rng: random.Random, *, older_p: float, recent_p: float) -> list[AttemptOutcome]:
        older = [
            AttemptOutcome(
                at=now - timedelta(days=8, hours=i),
                outcome="completed" if rng.random() < older_p else "provider_error",
            )
            for i in range(400)
        ]
        recent = [
            AttemptOutcome(
                at=now - timedelta(hours=1 + i),
                outcome="completed" if rng.random() < recent_p else "provider_error",
            )
            for i in range(80)
        ]
        return older + recent

    stationary = series(random.Random(11), older_p=0.85, recent_p=0.85)  # noqa: S311 — seeded
    quiet = detect_regression(
        recent=compute_stats(stationary, window=WINDOW_7D, now=now),
        all_time=compute_stats(stationary, window=WINDOW_ALL, now=now),
    )
    assert quiet.status == "stable", quiet.reason

    degraded = series(random.Random(11), older_p=0.85, recent_p=0.45)  # noqa: S311 — seeded
    loud = detect_regression(
        recent=compute_stats(degraded, window=WINDOW_7D, now=now),
        all_time=compute_stats(degraded, window=WINDOW_ALL, now=now),
    )
    assert loud.status == "regressed", loud.reason
    assert loud.recent_samples == 80 and loud.baseline_samples == 400


def test_the_all_window_never_drives_the_factor() -> None:
    """A bad day must age out of the factor: ``all`` is for the page and the regression baseline."""
    assert [w.name for w in FACTOR_WINDOWS] == ["7d", "30d"]
    only_all = reliability_factor({"all": _stats("all", errors=200)})
    assert only_all.neutral and only_all.value == 1.0
    assert "7d and 30d" in only_all.reason and "all=" not in only_all.reason
