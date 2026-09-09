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
from loadcoach.domain.routing.scoring import CapabilityScore
from loadcoach.domain.routing.subject import (
    AdapterFacts,
    ExecutionSubject,
    ModelFacts,
    ProviderFacts,
    ServedContext,
    ServedContextSource,
)

GIB = 1024**3


def _score(
    capability_id: str,
    score: float | None,
    *,
    source: str = "benchmark",
    weight: float = 1.0,
    **extra: object,
) -> CapabilityScore:
    """One resolved capability, as scoring would hand it to the constraint filter."""
    return CapabilityScore(
        capability_id=capability_id,
        weight=weight,
        score=score,
        confidence=None if score is None else 0.9,
        source="absent" if score is None else source,
        **extra,  # type: ignore[arg-type]  # note/remedy/hash fields, all str | None
    )


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
    enabled: bool = True,
    provider: ProviderFacts | None = None,
    is_remote: bool = False,
    layers: int | None = 32,
    kv_heads: int | None = 8,
    head_dim: int | None = 128,
    requested_context: int | None = None,
    adapter: AdapterFacts | None = None,
    provider_kind: str = "fake",
    runtime_profile: RuntimeProfile | None = None,
) -> ExecutionSubject:
    facts = ModelFacts(
        model_id="01ABCDEFGHJKMNPQRSTVWXYZ00",
        canonical_id=canonical_id,
        provider_kind=provider_kind,
        provider_model_name="m",
        available=available,
        unavailable_reason=None if available else "not reported by the last discovery",
        enabled=enabled,
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
        runtime_profile=runtime_profile
        if runtime_profile is not None
        else RuntimeProfile(
            context_size=requested_context if requested_context is not None else served
        ),
        served_context=ServedContext(tokens=served, source=source),
        adapter=adapter,
    )


def _adapter_subject(**kwargs: object) -> ExecutionSubject:
    """A candidate serving one compatible adapter, which is what the evidence gate filters."""
    facts = AdapterFacts(
        adapter_id="01ADAPTERADAPTERADAPTER00",
        name="terse",
        artifact_digest=f"sha256:{'b' * 64}",
        base_model_name="m",
        base_artifact_digest=f"sha256:{'a' * 64}",
        base_confidence="digest",
        declared_capabilities=("reasoning",),
        data_classification="confidential",
    )
    return _subject(
        canonical_id=f"fake/m@sha256:{'a' * 64}",
        adapter=facts,
        provider=ProviderFacts(
            context_configurable=True, supports_tool_use=True, adapter_hot_swap=True
        ),
        **kwargs,  # type: ignore[arg-type]  # the documented keyword surface of _subject
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


def test_a_disabled_model_is_rejected_before_anything_else_is_evaluated() -> None:
    """ADR-0118: an operator's exclusion is the named reason, not an incidental provider state."""
    rejection, _, _ = evaluate_constraints(
        _subject(enabled=False, available=False),
        estimate_vram(size_bytes=None, served_context=8192),
        ConstraintInputs(),
    )
    assert rejection is not None
    assert rejection.reason == "model_disabled"
    assert rejection.detail["enabled"] is False


def test_a_quantized_cache_on_ollama_is_rejected_by_name_before_availability() -> None:
    """ADR-0120 rule 4: a person's configuration mistake, named before the provider's state."""
    rejection, _, _ = evaluate_constraints(
        _subject(
            provider_kind="ollama",
            available=False,
            runtime_profile=RuntimeProfile(flash_attention=True, kv_cache_precision="q8_0"),
        ),
        estimate_vram(size_bytes=None, served_context=8192),
        ConstraintInputs(),
    )
    assert rejection is not None
    assert rejection.reason == "runtime_setting_unhonoured"
    assert rejection.detail["field"] == "flash_attention"
    assert rejection.detail["provider_kind"] == "ollama"


def test_a_quantized_cache_without_flash_attention_is_rejected_on_the_resolved_profile() -> None:
    """ADR-0120 rule 3: llama.cpp would silently serve f16."""
    rejection, _, _ = evaluate_constraints(
        _subject(
            provider_kind="llamacpp", runtime_profile=RuntimeProfile(kv_cache_precision="q4_0")
        ),
        estimate_vram(size_bytes=None, served_context=8192),
        ConstraintInputs(),
    )
    assert rejection is not None
    assert rejection.reason == "kv_cache_needs_flash_attention"
    assert rejection.detail["kv_cache_precision"] == "q4_0"


def test_a_quantized_cache_with_flash_attention_on_llamacpp_passes_this_gate() -> None:
    rejection, _, _ = evaluate_constraints(
        _subject(
            provider_kind="llamacpp",
            runtime_profile=RuntimeProfile(flash_attention=True, kv_cache_precision="q4_0"),
        ),
        estimate_vram(size_bytes=None, served_context=8192),
        ConstraintInputs(),
    )
    assert rejection is None or rejection.reason not in {
        "runtime_setting_unhonoured",
        "kv_cache_needs_flash_attention",
    }


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


def test_a_set_think_requires_thinking_control_and_names_who_asked() -> None:
    """ADR-0099 rule 4: the profile's control is a routing rejection, not a provider refusal."""
    estimate = estimate_vram(
        size_bytes=1 * GIB, served_context=8192, layers=1, kv_heads=1, head_dim=8
    )
    rejection, _, _ = evaluate_constraints(
        _subject(provider=ProviderFacts(supports_thinking_control=False)),
        estimate,
        ConstraintInputs(requires_thinking_control="task_profile"),
    )
    assert rejection is not None
    assert rejection.reason == "capability_unsupported"
    assert rejection.detail["capability"] == "thinking_control"
    assert rejection.detail["required_by"] == "task_profile"

    from_request, _, _ = evaluate_constraints(
        _subject(provider=ProviderFacts(supports_thinking_control=False)),
        estimate,
        ConstraintInputs(requires_thinking_control="request"),
    )
    assert from_request is not None
    assert from_request.detail["required_by"] == "request"

    # A provider that carries the control is not rejected for it, and a profile that asks for
    # nothing never imposes the requirement at all.
    for inputs in (
        ConstraintInputs(requires_thinking_control="task_profile"),
        ConstraintInputs(),
    ):
        provider = ProviderFacts(
            supports_thinking_control=inputs.requires_thinking_control is not None
        )
        allowed, _, _ = evaluate_constraints(_subject(provider=provider), estimate, inputs)
        assert allowed is None


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
            min_capability_scores={"code_review": 0.35},
            resolved_capabilities={"code_review": _score("code_review", 0.20)},
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
            min_capability_scores={"code_review": 0.35},
            resolved_capabilities={"code_review": _score("code_review", None)},
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


# --- the adapter evidence gate (ADR-0064 rule 3, ADR-0087) --------------------------------------


def _gate(
    subject: ExecutionSubject, resolved: CapabilityScore | None, **extra: object
) -> tuple[str, dict[str, object]] | None:
    """Run the constraint filter with the gate on and return the rejection, if any."""
    rejection, _, _ = evaluate_constraints(
        subject,
        estimate_vram(size_bytes=1 * GIB, served_context=8192, layers=1, kv_heads=1, head_dim=8),
        ConstraintInputs(
            require_adapter_evidence=True,
            top_weighted_capability="reasoning",
            resolved_capabilities={} if resolved is None else {"reasoning": resolved},
            **extra,  # type: ignore[arg-type]  # documented ConstraintInputs fields
        ),
    )
    return None if rejection is None else (rejection.reason, dict(rejection.detail))


def test_a_measured_adapter_subject_passes_the_evidence_gate() -> None:
    assert _gate(_adapter_subject(), _score("reasoning", 0.82)) is None


def test_a_production_signal_satisfies_the_evidence_gate() -> None:
    """ADR-0064 rule 3 admits benchmark *or* production; only claims are refused."""
    assert _gate(_adapter_subject(), _score("reasoning", 0.7, source="production")) is None


def test_an_adapter_nobody_measured_is_rejected_and_says_so() -> None:
    result = _gate(_adapter_subject(), _score("reasoning", None))
    assert result is not None
    reason, detail = result
    assert reason == "adapter_unmeasured"
    assert detail["resolved_source"] == "absent"
    assert "no measured evidence" in str(detail["problem"])


def test_a_declared_claim_does_not_satisfy_the_evidence_gate() -> None:
    """ADR-0087: the failure found live — a manifest's claim scoring 0.500 and routing on it."""
    result = _gate(_adapter_subject(), _score("reasoning", 0.5, source="declared"))
    assert result is not None
    reason, detail = result
    assert reason == "adapter_unmeasured"
    assert detail["resolved_source"] == "declared"
    assert "a claim and not a measurement" in str(detail["problem"])


def test_a_manual_score_does_not_satisfy_the_evidence_gate() -> None:
    """Evidence for the low_evidence flag, and still not a benchmark (ADR-0087 rule 2)."""
    result = _gate(_adapter_subject(), _score("reasoning", 0.9, source="manual"))
    assert result is not None
    assert result[1]["resolved_source"] == "manual"


def test_an_excluded_measurement_does_not_satisfy_the_evidence_gate() -> None:
    """The whole point: a signal scoring excludes no longer satisfies the gate that admits it."""
    result = _gate(
        _adapter_subject(),
        _score(
            "reasoning",
            0.5,
            source="evidence_profile_mismatch",
            note="evidence measured under runtime profile abc, executing under def",
            remedy="freeweight run start --context-size 8192",
            measured_profile_hash="abcdefabcdefabcd",
        ),
    )
    assert result is not None
    reason, detail = result
    assert reason == "adapter_unmeasured"
    assert detail["resolved_source"] == "evidence_profile_mismatch"
    assert detail["measured_profile_hash"] == "abcdefabcdefabcd"
    assert detail["executing_profile_hash"] != detail["measured_profile_hash"]
    assert str(detail["remedy"]).startswith("freeweight run start")
    assert "does not describe this execution" in str(detail["problem"])


def test_a_foreign_machine_measurement_does_not_satisfy_the_evidence_gate() -> None:
    result = _gate(
        _adapter_subject(),
        _score(
            "reasoning",
            0.5,
            source="evidence_foreign_machine",
            measured_machine_fingerprint="somewhere-else",
        ),
    )
    assert result is not None
    assert result[1]["measured_machine_fingerprint"] == "somewhere-else"


def test_the_gate_does_not_apply_to_a_pin() -> None:
    """A pin bypasses scoring, not the hard constraints; the caller turns the gate off for it."""
    rejection, _, _ = evaluate_constraints(
        _adapter_subject(),
        estimate_vram(size_bytes=1 * GIB, served_context=8192, layers=1, kv_heads=1, head_dim=8),
        ConstraintInputs(
            require_adapter_evidence=False,
            top_weighted_capability="reasoning",
            resolved_capabilities={"reasoning": _score("reasoning", None)},
        ),
    )
    assert rejection is None


def test_the_gate_does_not_apply_to_a_bare_base() -> None:
    """A base with no evidence at all is scored, not filtered — the gate is adapters only."""
    assert _gate(_subject(), _score("reasoning", None)) is None
