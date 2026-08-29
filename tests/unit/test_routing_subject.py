"""Runtime profile resolution and served-context derivation (ADR-0023)."""

from __future__ import annotations

from baseaicore import RuntimeProfile

from loadcoach.domain.routing.subject import (
    ProviderFacts,
    resolve_runtime_profile,
    resolve_served_context,
    signals_by_capability,
)


def test_default_profile_is_legal_and_stably_hashed() -> None:
    """ADR-0023 §1: RuntimeProfile() is a legal profile; there is no unprofiled execution."""
    resolved = resolve_runtime_profile(defaults=RuntimeProfile())
    assert resolved == RuntimeProfile()
    assert resolved.profile_hash == RuntimeProfile().profile_hash
    assert resolved.profile_hash


def test_resolution_order_is_default_then_per_model_then_profile_then_override() -> None:
    resolved = resolve_runtime_profile(
        defaults=RuntimeProfile(context_size=2048, keep_alive="5m"),
        per_model=RuntimeProfile(context_size=8192),
        min_context_tokens=16384,
        context_configurable=True,
        override=RuntimeProfile(kv_cache_precision="q8_0"),
    )
    # The per-model level wins over the default; the task-profile rule does not overwrite an
    # explicitly stated size; the override adds a field without erasing the others.
    assert resolved.context_size == 8192
    assert resolved.keep_alive == "5m"
    assert resolved.kv_cache_precision == "q8_0"


def test_task_profile_sets_context_when_nothing_configured_one() -> None:
    """ADR-0023 §4: LoadCoach sets context_size rather than hoping, where it can."""
    resolved = resolve_runtime_profile(
        defaults=RuntimeProfile(),
        min_context_tokens=16384,
        context_configurable=True,
    )
    assert resolved.context_size == 16384


def test_task_profile_does_not_set_context_when_provider_refuses_configuration() -> None:
    resolved = resolve_runtime_profile(
        defaults=RuntimeProfile(),
        min_context_tokens=16384,
        context_configurable=False,
    )
    assert resolved.context_size is None


def test_served_context_sources_in_priority_order() -> None:
    configured = resolve_served_context(
        profile=RuntimeProfile(context_size=32768),
        provider=ProviderFacts(context_configurable=True, reported_served_context=4096),
        max_context=131072,
    )
    assert configured is not None
    assert (configured.tokens, configured.source) == (32768, "configured")

    reported = resolve_served_context(
        profile=RuntimeProfile(),
        provider=ProviderFacts(reported_served_context=4096),
        max_context=131072,
    )
    assert reported is not None
    assert (reported.tokens, reported.source) == (4096, "reported")

    assumed = resolve_served_context(
        profile=RuntimeProfile(), provider=ProviderFacts(), max_context=131072
    )
    assert assumed is not None
    assert (assumed.tokens, assumed.source) == (131072, "assumed")


def test_a_context_the_provider_will_ignore_is_not_reported_as_configured() -> None:
    """ADR-0023 §4: a recorded context that never happened is a fabricated measurement."""
    served = resolve_served_context(
        profile=RuntimeProfile(context_size=32768),
        provider=ProviderFacts(context_configurable=False),
        max_context=131072,
    )
    assert served is not None
    assert (served.tokens, served.source) == (131072, "assumed")


def test_no_context_at_all_is_absent_not_zero() -> None:
    """A model with no advertised maximum and no reported context yields None, never 0."""
    assert (
        resolve_served_context(profile=RuntimeProfile(), provider=ProviderFacts(), max_context=None)
        is None
    )


def test_signals_group_by_capability_preserving_order() -> None:
    from loadcoach.domain.routing.subject import CapabilitySignal

    signals = (
        CapabilitySignal(capability_id="reasoning", source="declared", score=1.0, confidence=0.5),
        CapabilitySignal(capability_id="tool_use", source="declared", score=1.0, confidence=0.5),
        CapabilitySignal(capability_id="reasoning", source="manual", score=0.8, confidence=0.6),
    )
    grouped = signals_by_capability(signals)
    assert set(grouped) == {"reasoning", "tool_use"}
    assert [signal.source for signal in grouped["reasoning"]] == ["declared", "manual"]
