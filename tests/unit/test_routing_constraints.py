"""The VRAM/KV estimator and routing §4's ten hard constraints.

Every constraint must reject for the right reason **with the right numbers** — the whole point of
this filter is that "nothing was eligible" is never the whole answer.
"""

from __future__ import annotations

from datetime import UTC, datetime

from baseaicore import RuntimeProfile
from sweatmeter import GpuSample, TelemetrySnapshot

from loadcoach.domain.routing.constraints import (
    ACTIVATION_OVERHEAD_BYTES,
    LOADING_OVERHEAD_FACTOR,
    ConstraintInputs,
    device_fits,
    estimate_vram,
    evaluate_constraints,
    free_vram_by_gpu,
    kv_bytes_per_token,
)
from loadcoach.domain.routing.subject import (
    ExecutionSubject,
    ModelFacts,
    ProviderFacts,
    ServedContext,
    ServedContextSource,
)

GIB = 1024**3


def _snapshot(
    *gpus: tuple[int, int | None, int | None], ram: int | None = None
) -> TelemetrySnapshot:
    """Build a snapshot from ``(index, total, used)`` triples; ``None`` means unreported."""
    from baseaicore import UNSUPPORTED

    samples = tuple(
        GpuSample(
            index=index,
            vram_total_bytes=UNSUPPORTED if total is None else total,
            vram_used_bytes=UNSUPPORTED if used is None else used,
        )
        for index, total, used in gpus
    )
    return TelemetrySnapshot(
        timestamp=datetime(2026, 8, 29, tzinfo=UTC),
        gpus=samples,
        ram_available_bytes=UNSUPPORTED if ram is None else ram,
    )


def _subject(
    *,
    canonical_id: str = "fake/m@sha256:aaaa",
    size_bytes: int | None = 8 * GIB,
    served: int = 8192,
    source: ServedContextSource = "configured",
    max_context: int | None = 32768,
    available: bool = True,
    provider: ProviderFacts | None = None,
    is_remote: bool = False,
    layers: int | None = 32,
    kv_heads: int | None = 8,
    head_dim: int | None = 128,
    requested_context: int | None = None,
) -> ExecutionSubject:
    facts = ModelFacts(
        model_id="01ABCDEFGHJKMNPQRSTVWXYZ00",
        canonical_id=canonical_id,
        provider_kind="fake",
        provider_model_name="m",
        available=available,
        unavailable_reason=None if available else "not reported by the last discovery",
        max_context=max_context,
        size_bytes=size_bytes,
        parameter_count=8_000_000_000,
        layers=layers,
        kv_heads=kv_heads,
        head_dim=head_dim,
        is_remote=is_remote,
    )
    return ExecutionSubject(
        facts=facts,
        provider=provider or ProviderFacts(context_configurable=True, supports_tool_use=True),
        runtime_profile=RuntimeProfile(
            context_size=requested_context if requested_context is not None else served
        ),
        served_context=ServedContext(tokens=served, source=source),
    )


# --- the estimator -------------------------------------------------------------------------


def test_kv_bytes_per_token_theoretical_arithmetic() -> None:
    per_token, source, assumed = kv_bytes_per_token(
        layers=32, kv_heads=8, head_dim=128, kv_cache_precision="f16"
    )
    assert per_token == 2 * 32 * 8 * 128 * 2.0
    assert source == "theoretical"
    assert assumed is False


def test_kv_precision_is_assumed_and_labelled_when_the_profile_says_nothing() -> None:
    per_token, source, assumed = kv_bytes_per_token(
        layers=32, kv_heads=8, head_dim=128, kv_cache_precision=None
    )
    assert per_token == 2 * 32 * 8 * 128 * 2.0
    assert source == "theoretical"
    assert assumed is True


def test_measured_kv_figure_wins_over_the_theoretical_one() -> None:
    per_token, source, assumed = kv_bytes_per_token(
        layers=32, kv_heads=8, head_dim=128, kv_cache_precision="f16", observed=123.5
    )
    assert (per_token, source, assumed) == (123.5, "observed", False)


def test_missing_geometry_yields_no_kv_figure_not_zero() -> None:
    per_token, source, _ = kv_bytes_per_token(
        layers=None, kv_heads=8, head_dim=128, kv_cache_precision="f16"
    )
    assert per_token is None
    assert source == "unknown"


def test_estimate_uses_served_context_not_advertised_maximum() -> None:
    served = estimate_vram(
        size_bytes=8 * GIB, served_context=4096, layers=32, kv_heads=8, head_dim=128
    )
    advertised = estimate_vram(
        size_bytes=8 * GIB, served_context=131072, layers=32, kv_heads=8, head_dim=128
    )
    assert served.total_bytes is not None
    assert advertised.total_bytes is not None
    assert advertised.total_bytes > served.total_bytes
    assert served.served_context == 4096
    assert served.total_bytes == (
        int(8 * GIB * LOADING_OVERHEAD_FACTOR)
        + int(2 * 32 * 8 * 128 * 2.0 * 4096)
        + ACTIVATION_OVERHEAD_BYTES
    )


def test_unknown_weight_size_makes_the_estimate_unknown_never_zero() -> None:
    estimate = estimate_vram(
        size_bytes=None, served_context=4096, layers=32, kv_heads=8, head_dim=128
    )
    assert estimate.total_bytes is None
    assert estimate.weights_bytes is None
    assert estimate.unknown_reason is not None
    assert "weight size" in estimate.unknown_reason


def test_free_vram_is_absent_when_only_one_of_total_and_used_is_reported() -> None:
    snapshot = _snapshot((0, 24 * GIB, None), (1, None, 2 * GIB))
    assert free_vram_by_gpu(snapshot) == {0: None, 1: None}


def test_unknown_estimate_does_not_fit_any_device() -> None:
    estimate = estimate_vram(size_bytes=None, served_context=4096)
    fits = device_fits(estimate, _snapshot((0, 24 * GIB, 0)), headroom_bytes=0)
    assert [fit.fits for fit in fits] == [False]


# --- the constraints ------------------------------------------------------------------------


def test_model_unavailable_names_the_reason() -> None:
    rejection, _, _ = evaluate_constraints(
        _subject(available=False),
        estimate_vram(size_bytes=None, served_context=8192),
        ConstraintInputs(),
    )
    assert rejection is not None
    assert rejection.reason == "model_unavailable"
    assert rejection.detail["reason"] == "not reported by the last discovery"


def test_advertised_131072_served_4096_is_rejected_as_context_too_small() -> None:
    """The named failure ADR-0023 exists to prevent: never admitted and silently truncated."""
    subject = _subject(served=4096, source="configured", max_context=131072)
    rejection, _, _ = evaluate_constraints(
        subject,
        estimate_vram(size_bytes=8 * GIB, served_context=4096, layers=32, kv_heads=8, head_dim=128),
        ConstraintInputs(min_context_tokens=16384),
    )
    assert rejection is not None
    assert rejection.reason == "context_too_small"
    assert rejection.detail["served_context"] == 4096
    assert rejection.detail["min_context_tokens"] == 16384
    assert rejection.detail["advertised_max_context"] == 131072
    assert rejection.detail["served_context_source"] == "configured"


def test_context_not_configurable_when_an_explicit_ask_cannot_be_honoured() -> None:
    """An operator asked for 16 384; the provider will serve its own 131 072 and ignore the ask."""
    subject = _subject(
        served=131072,
        source="assumed",
        max_context=131072,
        provider=ProviderFacts(context_configurable=False),
        requested_context=16384,
    )
    rejection, _, _ = evaluate_constraints(
        subject,
        estimate_vram(size_bytes=1 * GIB, served_context=131072, layers=1, kv_heads=1, head_dim=8),
        ConstraintInputs(min_context_tokens=16384),
    )
    assert rejection is not None
    assert rejection.reason == "context_not_configurable"
    assert rejection.detail["requested_context"] == 16384
    assert rejection.detail["served_context"] == 131072
    assert rejection.detail["context_configurable"] is False


def test_an_assumed_context_that_meets_the_requirement_is_admitted_not_rejected() -> None:
    """ADR-0023 §4: not configurable and assumed is a flag on the decision, not a rejection."""
    subject = _subject(
        served=32768,
        source="assumed",
        max_context=32768,
        provider=ProviderFacts(context_configurable=False),
    )
    rejection, _, _ = evaluate_constraints(
        subject,
        estimate_vram(size_bytes=1 * GIB, served_context=32768, layers=1, kv_heads=1, head_dim=8),
        ConstraintInputs(min_context_tokens=4096),
    )
    assert rejection is None


def test_context_limit_exceeded_carries_both_numbers() -> None:
    rejection, _, _ = evaluate_constraints(
        _subject(served=8192),
        estimate_vram(size_bytes=1 * GIB, served_context=8192, layers=1, kv_heads=1, head_dim=8),
        ConstraintInputs(required_context=20000),
    )
    assert rejection is not None
    assert rejection.reason == "context_limit_exceeded"
    assert rejection.detail == {
        "required_context": 20000,
        "served_context": 8192,
        "served_context_source": "configured",
    }


def test_capability_unsupported_names_the_capability() -> None:
    rejection, _, _ = evaluate_constraints(
        _subject(provider=ProviderFacts(supports_tool_use=False)),
        estimate_vram(size_bytes=1 * GIB, served_context=8192, layers=1, kv_heads=1, head_dim=8),
        ConstraintInputs(requires_capabilities=("tool_use",)),
    )
    assert rejection is not None
    assert rejection.reason == "capability_unsupported"
    assert rejection.detail["capability"] == "tool_use"


def test_two_gpus_are_never_summed_and_the_rejection_names_both() -> None:  # ADR-0027 §2
    """14 GB model, 9.8 GB free on GPU 0 and 7.1 GB on GPU 1: 16.9 GB total, and it does not fit."""
    subject = _subject(size_bytes=13 * GIB, served=8192)
    estimate = estimate_vram(
        size_bytes=13 * GIB, served_context=8192, layers=32, kv_heads=8, head_dim=128
    )
    assert estimate.total_bytes is not None
    free_zero = 9_800_000_000
    free_one = 7_100_000_000
    assert estimate.total_bytes > free_zero
    assert estimate.total_bytes > free_one
    assert estimate.total_bytes < free_zero + free_one

    snapshot = _snapshot((0, 24 * GIB, 24 * GIB - free_zero), (1, 24 * GIB, 24 * GIB - free_one))
    rejection, fits, target = evaluate_constraints(
        subject, estimate, ConstraintInputs(snapshot=snapshot, vram_headroom_bytes=0)
    )
    assert rejection is not None
    assert rejection.reason == "insufficient_vram"
    assert target is None
    assert rejection.detail["estimated_bytes"] == estimate.total_bytes
    assert rejection.detail["free_bytes_by_gpu"] == {"0": free_zero, "1": free_one}
    assert [fit.gpu_index for fit in fits] == [0, 1]


def test_a_device_that_fits_is_named_as_the_target() -> None:
    subject = _subject(size_bytes=4 * GIB, served=4096)
    estimate = estimate_vram(
        size_bytes=4 * GIB, served_context=4096, layers=32, kv_heads=8, head_dim=128
    )
    snapshot = _snapshot((0, 24 * GIB, 23 * GIB), (1, 24 * GIB, 2 * GIB))
    rejection, _, target = evaluate_constraints(
        subject, estimate, ConstraintInputs(snapshot=snapshot, vram_headroom_bytes=0)
    )
    assert rejection is None
    assert target == 1


def test_insufficient_ram_applies_on_a_machine_with_no_gpu() -> None:
    subject = _subject(size_bytes=8 * GIB, served=4096)
    estimate = estimate_vram(
        size_bytes=8 * GIB, served_context=4096, layers=32, kv_heads=8, head_dim=128
    )
    rejection, fits, target = evaluate_constraints(
        subject, estimate, ConstraintInputs(snapshot=_snapshot(ram=2 * GIB))
    )
    assert rejection is not None
    assert rejection.reason == "insufficient_ram"
    assert rejection.detail["free_bytes"] == 2 * GIB
    assert rejection.detail["estimated_bytes"] == estimate.total_bytes
    assert fits == ()
    assert target is None


def test_below_minimum_score_names_the_capability_and_both_numbers() -> None:
    rejection, _, _ = evaluate_constraints(
        _subject(),
        estimate_vram(size_bytes=1 * GIB, served_context=8192, layers=1, kv_heads=1, head_dim=8),
        ConstraintInputs(
            min_capability_scores={"code_review": 0.35}, resolved_scores={"code_review": 0.20}
        ),
    )
    assert rejection is not None
    assert rejection.reason == "below_minimum_score"
    assert rejection.detail == {"capability": "code_review", "score": 0.20, "minimum": 0.35}


def test_an_absent_score_is_never_below_a_minimum() -> None:
    """Absence of evidence is not evidence of incapacity — it cannot be below a floor."""
    rejection, _, _ = evaluate_constraints(
        _subject(),
        estimate_vram(size_bytes=1 * GIB, served_context=8192, layers=1, kv_heads=1, head_dim=8),
        ConstraintInputs(
            min_capability_scores={"code_review": 0.35}, resolved_scores={"code_review": None}
        ),
    )
    assert rejection is None


def test_excluded_by_policy_for_an_excluded_model_and_for_a_disallowed_remote() -> None:
    estimate = estimate_vram(
        size_bytes=1 * GIB, served_context=8192, layers=1, kv_heads=1, head_dim=8
    )
    by_name, _, _ = evaluate_constraints(
        _subject(),
        estimate,
        ConstraintInputs(exclude_models=("fake/m@sha256:aaaa",)),
    )
    assert by_name is not None
    assert by_name.reason == "excluded_by_policy"
    assert by_name.detail["rule"] == "exclude_models"

    by_remoteness, _, _ = evaluate_constraints(
        _subject(is_remote=True), estimate, ConstraintInputs(allow_remote_providers=False)
    )
    assert by_remoteness is not None
    assert by_remoteness.reason == "excluded_by_policy"
    assert by_remoteness.detail["rule"] == "allow_remote_providers"


def test_recently_failing_when_the_breaker_is_open() -> None:
    rejection, _, _ = evaluate_constraints(
        _subject(),
        estimate_vram(size_bytes=1 * GIB, served_context=8192, layers=1, kv_heads=1, head_dim=8),
        ConstraintInputs(open_circuit_breakers=frozenset({"fake/m@sha256:aaaa"})),
    )
    assert rejection is not None
    assert rejection.reason == "recently_failing"


def test_constraints_are_applied_in_the_documented_order() -> None:
    """A candidate failing several constraints reports the first one routing §4 lists."""
    subject = _subject(available=False, served=1024, is_remote=True)
    rejection, _, _ = evaluate_constraints(
        subject,
        estimate_vram(size_bytes=None, served_context=1024),
        ConstraintInputs(
            min_context_tokens=16384,
            allow_remote_providers=False,
            exclude_models=("fake/m@sha256:aaaa",),
        ),
    )
    assert rejection is not None
    assert rejection.reason == "model_unavailable"


def test_a_resident_model_fits_on_its_device_whatever_the_estimate_says() -> None:
    """Queue §5's one exception to 'unknown does not fit': the model is already loaded there."""
    subject = _subject(size_bytes=None)  # unknown estimate
    estimate = estimate_vram(
        size_bytes=None, served_context=8192, layers=32, kv_heads=8, head_dim=128
    )
    assert estimate.total_bytes is None
    snapshot = _snapshot((0, 16 * GIB, 15 * GIB), (1, 16 * GIB, 15 * GIB))
    rejection, _, target = evaluate_constraints(
        subject,
        estimate,
        ConstraintInputs(
            snapshot=snapshot, resident_devices={subject.facts.canonical_id: frozenset({1})}
        ),
    )
    assert rejection is None
    assert target == 1  # the device it is resident on, not merely the first
    rejection, _, _ = evaluate_constraints(subject, estimate, ConstraintInputs(snapshot=snapshot))
    assert rejection is not None and rejection.reason == "insufficient_vram"


def test_a_resident_device_is_preferred_over_another_device_that_merely_fits() -> None:
    """Loading a second copy elsewhere would be the thrash residency exists to prevent."""
    subject = _subject(size_bytes=2 * GIB)
    estimate = estimate_vram(
        size_bytes=2 * GIB, served_context=8192, layers=32, kv_heads=8, head_dim=128
    )
    snapshot = _snapshot((0, 16 * GIB, 0), (1, 16 * GIB, 15 * GIB))
    _, _, target = evaluate_constraints(
        subject,
        estimate,
        ConstraintInputs(
            snapshot=snapshot, resident_devices={subject.facts.canonical_id: frozenset({1})}
        ),
    )
    assert target == 1
