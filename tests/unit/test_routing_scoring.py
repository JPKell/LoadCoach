"""Capability scoring: the absent-evidence rule, source precedence, and the adjustment factors."""

from __future__ import annotations

import pytest
from baseaicore import RuntimeProfile

from loadcoach.domain.routing.scoring import (
    DECLARED_PRIOR_CONFIDENCE,
    DECLARED_PRIOR_SCORE,
    PARAMETER_BAND_PRIOR_CEILING,
    PARAMETER_BAND_PRIOR_FLOOR,
    PRODUCTION_MINIMUM_SAMPLES,
    ScoringInputs,
    adjustment_factors,
    parameter_band_priors,
    resolve_capability,
    score_subject,
)
from loadcoach.domain.routing.subject import (
    CapabilitySignal,
    ExecutionSubject,
    ModelFacts,
    ProviderFacts,
    ServedContext,
)

PROFILE_HASH = RuntimeProfile(context_size=32768).profile_hash
OTHER_HASH = RuntimeProfile(context_size=4096).profile_hash


def _subject(
    *signals: CapabilitySignal,
    canonical_id: str = "fake/m@sha256:aaaa",
    parameter_count: int | None = 8_000_000_000,
) -> ExecutionSubject:
    return ExecutionSubject(
        facts=ModelFacts(
            model_id="01ABCDEFGHJKMNPQRSTVWXYZ00",
            canonical_id=canonical_id,
            provider_kind="fake",
            provider_model_name="m",
            parameter_count=parameter_count,
        ),
        provider=ProviderFacts(),
        runtime_profile=RuntimeProfile(context_size=32768),
        served_context=ServedContext(tokens=32768, source="configured"),
        signals=signals,
    )


def _benchmark(
    capability: str,
    score: float,
    *,
    profile_hash: str = PROFILE_HASH,
    match_state: str = "bound",
    confidence: float = 0.62,
) -> CapabilitySignal:
    return CapabilitySignal(
        capability_id=capability,
        source="benchmark",
        score=score,
        confidence=confidence,
        runtime_profile_hash=profile_hash,
        match_state=match_state,
        sample_count=40,
    )


# --- the absent-evidence rule ---------------------------------------------------------------


def test_absent_capability_is_excluded_from_numerator_and_denominator_never_scored_zero() -> None:
    """routing §5: absence of evidence is not evidence of incapacity."""
    subject = _subject(_benchmark("reasoning", 0.8), parameter_count=None)
    fit = score_subject(subject, ScoringInputs(weights={"reasoning": 0.5, "long_context": 0.5}))
    absent = next(c for c in fit.capabilities if c.capability_id == "long_context")
    assert absent.score is None
    assert absent.confidence is None
    assert absent.source == "absent"
    assert absent.note is not None
    # Denominator is the present weight alone (0.5), so the absent half neither adds nor dilutes.
    assert fit.present_weight == 0.5
    assert fit.task_fit == (0.5 * 0.8 * 0.62) / 0.5

    # Scored as zero, task_fit would have been half this. Stated explicitly so a regression that
    # reintroduces the zero fails here rather than silently halving every score.
    assert fit.task_fit != (0.5 * 0.8 * 0.62) / 1.0


def test_a_capability_with_no_signal_and_no_parameter_count_is_absent() -> None:
    fit = score_subject(_subject(parameter_count=None), ScoringInputs(weights={"reasoning": 1.0}))
    assert fit.present_weight == 0.0
    assert fit.measured_weight == 0.0
    assert fit.task_fit == 0.0
    assert fit.capabilities[0].source == "absent"


# --- ADR-0023 §3: evidence matches only its own subject --------------------------------------


def test_profile_mismatched_evidence_is_absent_with_both_hashes_and_a_remedy() -> None:
    subject = _subject(_benchmark("reasoning", 0.9, profile_hash=OTHER_HASH))
    fit = score_subject(subject, ScoringInputs(weights={"reasoning": 1.0}))
    score = fit.capabilities[0]
    assert score.source == "evidence_profile_mismatch"
    assert score.score is None
    assert score.measured_profile_hash == OTHER_HASH
    assert score.note is not None
    assert OTHER_HASH in score.note
    assert PROFILE_HASH in score.note
    assert score.remedy is not None
    assert "--context-size 32768" in score.remedy
    assert fit.present_weight == 0.0


def test_an_excluded_measurement_scores_the_prior_it_displaced() -> None:
    """ADR-0088: no penalty for having been measured, and no word of the reason lost."""
    subject = _subject(_benchmark("reasoning", 0.9, profile_hash=OTHER_HASH))
    fit = score_subject(
        subject,
        ScoringInputs(weights={"reasoning": 1.0}, parameter_priors={"fake/m@sha256:aaaa": 0.5}),
    )
    score = fit.capabilities[0]
    assert score.source == "evidence_profile_mismatch"
    assert score.score == 0.5
    assert score.confidence == 0.3
    assert score.measured_profile_hash == OTHER_HASH
    assert score.remedy is not None
    # It scores like a prior and is not counted as evidence: the gate and the low_evidence flag
    # both read the source, never the number.
    assert not score.measured
    assert fit.measured_weight == 0.0


def test_an_excluded_measurement_scores_exactly_what_no_measurement_would() -> None:
    """The defect this closes was an *ordering*: benchmarked ranked below never-benchmarked."""
    priors = {"fake/m@sha256:aaaa": 0.5}
    excluded = score_subject(
        _subject(_benchmark("reasoning", 0.9, profile_hash=OTHER_HASH)),
        ScoringInputs(weights={"reasoning": 1.0}, parameter_priors=priors),
    )
    never = score_subject(
        _subject(), ScoringInputs(weights={"reasoning": 1.0}, parameter_priors=priors)
    )
    assert excluded.task_fit == never.task_fit
    assert never.capabilities[0].source == "prior"


def test_an_excluded_measurement_with_no_prior_is_still_absent() -> None:
    """The fallback is the prior it displaced; where there is none, so is the unmeasured sibling."""
    fit = score_subject(
        _subject(_benchmark("reasoning", 0.9, profile_hash=OTHER_HASH)),
        ScoringInputs(weights={"reasoning": 1.0}),
    )
    assert fit.capabilities[0].source == "evidence_profile_mismatch"
    assert fit.capabilities[0].score is None


def test_unbound_evidence_never_contributes() -> None:
    """ADR-0022 §4: a name_only match against a digest-carrying row is not this model."""
    subject = _subject(_benchmark("reasoning", 0.9, match_state="name_only"))
    fit = score_subject(subject, ScoringInputs(weights={"reasoning": 1.0}))
    assert fit.capabilities[0].source == "evidence_unbound"
    assert fit.capabilities[0].score is None


# --- source precedence ------------------------------------------------------------------------


def test_benchmark_evidence_outranks_every_prior() -> None:
    subject = _subject(
        _benchmark("reasoning", 0.9),
        CapabilitySignal(capability_id="reasoning", source="manual", score=0.2, confidence=0.4),
        CapabilitySignal(capability_id="reasoning", source="declared", score=1.0, confidence=0.5),
    )
    fit = score_subject(subject, ScoringInputs(weights={"reasoning": 1.0}))
    assert fit.capabilities[0].source == "benchmark"
    assert fit.capabilities[0].score == 0.9


def test_a_declared_flag_scores_as_a_neutral_prior_not_as_a_measured_one() -> None:
    """The stored 1.0 means "the provider declares this flag"; routing §5.1 contributes 0.5."""
    subject = _subject(
        CapabilitySignal(capability_id="tool_use", source="declared", score=1.0, confidence=0.5)
    )
    fit = score_subject(subject, ScoringInputs(weights={"tool_use": 1.0}))
    assert fit.capabilities[0].source == "declared"
    assert fit.capabilities[0].score == DECLARED_PRIOR_SCORE
    assert fit.capabilities[0].confidence == DECLARED_PRIOR_CONFIDENCE
    assert fit.measured_weight == 0.0


def test_production_evidence_below_the_sample_floor_is_not_used() -> None:
    thin = CapabilitySignal(
        capability_id="reasoning",
        source="production",
        score=1.0,
        confidence=0.9,
        sample_count=PRODUCTION_MINIMUM_SAMPLES - 1,
    )
    fit = score_subject(
        _subject(thin, parameter_count=None), ScoringInputs(weights={"reasoning": 1.0})
    )
    assert fit.capabilities[0].source == "absent"


def test_a_signal_below_min_confidence_is_discarded() -> None:
    weak = CapabilitySignal(capability_id="reasoning", source="manual", score=0.9, confidence=0.01)
    fit = score_subject(
        _subject(weak, parameter_count=None),
        ScoringInputs(weights={"reasoning": 1.0}, min_confidence=0.05),
    )
    assert fit.capabilities[0].source == "absent"


def test_require_evidence_refuses_declared_manual_and_band_priors() -> None:
    subject = _subject(
        CapabilitySignal(capability_id="reasoning", source="manual", score=0.9, confidence=0.4),
        CapabilitySignal(capability_id="reasoning", source="declared", score=1.0, confidence=0.5),
    )
    fit = score_subject(
        subject,
        ScoringInputs(
            weights={"reasoning": 1.0},
            parameter_priors={"fake/m@sha256:aaaa": 0.5},
            require_evidence=True,
        ),
    )
    assert fit.capabilities[0].source == "absent"


# --- the parameter band -------------------------------------------------------------------


def test_parameter_band_priors_are_relative_capped_and_omit_unreported_counts() -> None:
    priors = parameter_band_priors({"small": 1_000, "large": 100_000, "unknown": None})
    assert set(priors) == {"small", "large"}
    assert priors["small"] == PARAMETER_BAND_PRIOR_FLOOR
    assert priors["large"] == PARAMETER_BAND_PRIOR_CEILING
    assert all(
        PARAMETER_BAND_PRIOR_FLOOR <= v <= PARAMETER_BAND_PRIOR_CEILING for v in priors.values()
    )


def test_a_single_installed_model_gets_the_midpoint_not_an_extreme() -> None:
    priors = parameter_band_priors({"only": 8_000_000_000})
    assert priors["only"] == (PARAMETER_BAND_PRIOR_FLOOR + PARAMETER_BAND_PRIOR_CEILING) / 2


def test_band_prior_fills_a_capability_with_no_other_signal() -> None:
    fit = score_subject(
        _subject(),
        ScoringInputs(weights={"reasoning": 1.0}, parameter_priors={"fake/m@sha256:aaaa": 0.55}),
    )
    assert fit.capabilities[0].source == "prior"
    assert fit.capabilities[0].score == 0.55
    assert fit.measured_weight == 0.0
    assert fit.present_weight == 1.0


# --- low_evidence and the factors -------------------------------------------------------------


def test_measured_weight_counts_only_measurements_so_priors_still_read_as_low_evidence() -> None:
    subject = _subject(
        _benchmark("reasoning", 0.8),
        CapabilitySignal(capability_id="tool_use", source="declared", score=1.0, confidence=0.5),
    )
    fit = score_subject(subject, ScoringInputs(weights={"reasoning": 0.4, "tool_use": 0.6}))
    assert fit.present_weight == 1.0
    assert fit.measured_weight == 0.4


def test_adjustment_factors_stay_inside_their_documented_ranges() -> None:
    neutral = adjustment_factors(_subject())
    assert neutral.reliability == 1.0
    assert neutral.availability == 1.0
    assert neutral.residency == 1.0
    assert neutral.cost == 1.0
    assert neutral.product == 1.0

    resident = adjustment_factors(
        _subject(), resident_models=frozenset({"fake/m@sha256:aaaa"}), prefer_resident_bonus=0.05
    )
    assert resident.residency == 1.05

    remote_facts = ModelFacts(
        model_id="01ABCDEFGHJKMNPQRSTVWXYZ01",
        canonical_id="remote/m@sha256:bbbb",
        provider_kind="openai_compatible",
        provider_model_name="m",
        is_remote=True,
    )
    remote_subject = ExecutionSubject(
        facts=remote_facts,
        provider=ProviderFacts(),
        runtime_profile=RuntimeProfile(),
        served_context=ServedContext(tokens=8192, source="reported"),
    )
    assert adjustment_factors(remote_subject, remote_cost_factor=0.9).cost == 0.9


def test_resolve_capability_is_a_pure_function_of_its_arguments() -> None:
    args = {
        "capability_id": "reasoning",
        "weight": 0.5,
        "signals": (_benchmark("reasoning", 0.7),),
        "runtime_profile_hash": PROFILE_HASH,
        "profile_fields": {"context_size": 32768},
        "min_confidence": 0.05,
        "band_prior": 0.5,
        "require_evidence": False,
    }
    first = resolve_capability(**args)  # type: ignore[arg-type]  # a homogeneous kwargs dict cannot be typed more precisely without repeating the signature
    second = resolve_capability(**args)  # type: ignore[arg-type]  # same call, asserting purity
    assert first == second


def _residency_subject(canonical_id: str, *, adapter: str | None = None) -> ExecutionSubject:
    """A minimal subject for the residency arithmetic: only the base identity and adapter matter."""
    from loadcoach.domain.routing.subject import AdapterFacts

    facts = ModelFacts(
        model_id="01M",
        canonical_id=canonical_id,
        provider_kind="llamacpp",
        provider_model_name=canonical_id.split("/")[1].split("@")[0],
    )
    return ExecutionSubject(
        facts=facts,
        provider=ProviderFacts(adapter_hot_swap=adapter is not None),
        runtime_profile=RuntimeProfile(),
        served_context=ServedContext(tokens=4096, source="configured"),
        adapter=(
            None
            if adapter is None
            else AdapterFacts(
                adapter_id="01A",
                name=adapter,
                artifact_digest="sha256:" + "9" * 64,
                base_model_name="qwen",
            )
        ),
    )


BASE_A = "llamacpp/qwen@sha256:" + "1" * 64
BASE_B = "llamacpp/gemma@sha256:" + "2" * 64


def test_an_adapter_switch_on_the_resident_base_pays_nothing() -> None:
    """ADR-0066: what occupies a device is the base, so every adapter on it scores as resident."""
    resident = frozenset({BASE_A})

    bare = adjustment_factors(_residency_subject(BASE_A), resident_models=resident)
    with_terse = adjustment_factors(
        _residency_subject(BASE_A, adapter="terse"), resident_models=resident
    )
    with_pirate = adjustment_factors(
        _residency_subject(BASE_A, adapter="pirate"), resident_models=resident
    )

    assert bare.residency == with_terse.residency == with_pirate.residency == 1.05
    assert with_terse.residency_detail is not None
    assert with_terse.residency_detail["level"] == "resident_base"


def test_a_base_switch_pays_the_penalty_exactly_once() -> None:
    factors = adjustment_factors(
        _residency_subject(BASE_B), resident_models=frozenset({BASE_A}), base_switch_penalty=0.10
    )

    assert factors.residency == pytest.approx(0.90)
    assert factors.residency_detail is not None
    assert factors.residency_detail["level"] == "base_switch"
    assert factors.residency_detail["base_switch_penalty"] == 0.10


def test_nothing_resident_is_neutral_and_never_a_penalty() -> None:
    """Residency unknown and residency empty are the same evidence: there is no swap to charge."""
    factors = adjustment_factors(_residency_subject(BASE_B), resident_models=frozenset())

    assert factors.residency == 1.0
    assert factors.residency_detail is not None
    assert factors.residency_detail["level"] == "nothing_resident"


def test_ignore_residency_zeroes_both_terms_and_says_so() -> None:
    resident = frozenset({BASE_A})

    on_base = adjustment_factors(
        _residency_subject(BASE_A), resident_models=resident, ignore_residency=True
    )
    off_base = adjustment_factors(
        _residency_subject(BASE_B), resident_models=resident, ignore_residency=True
    )

    assert on_base.residency == off_base.residency == 1.0
    assert on_base.residency_detail is not None
    assert on_base.residency_detail["ignore_residency"] is True
    assert on_base.residency_detail["level"] == "ignored"
