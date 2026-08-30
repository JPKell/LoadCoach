"""loadcoach.domain.routing.subject — the execution subject a routing decision scores.

A candidate is never a bare model: it is the pair ``(identity, resolved runtime profile)``
(:doc:`ADR-0023 <../../adr/0023-runtime-profile-resolution>`), plus the ``served_context`` derived
from that profile and the provider's capabilities. Everything downstream — the context
constraint, the KV term of the VRAM estimate, which evidence may contribute — reads one of those
two values, so both are resolved here, once, before any constraint runs.

Not named verbatim in dev-plan P3's file list. The Work item's first bullet ("Runtime profile
resolution and the ``served_context`` derivation ... every candidate is an execution subject") has
no other legal home: it is pure domain logic that :mod:`~loadcoach.domain.routing.constraints`,
:mod:`~loadcoach.domain.routing.scoring` and :mod:`~loadcoach.domain.routing.context_budget` all
depend on, and putting it in any one of them would make the other two import a sibling for it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

from baseaicore import RuntimeProfile

from loadcoach.domain.evidence_policy import CalibrationFacts

if TYPE_CHECKING:
    from collections.abc import Mapping
    from datetime import datetime

__all__ = [
    "CapabilitySignal",
    "ExecutionSubject",
    "ModelFacts",
    "ProviderFacts",
    "RuntimeOverrides",
    "ServedContext",
    "ServedContextSource",
    "resolve_runtime_profile",
    "resolve_served_context",
    "signals_by_capability",
]

ServedContextSource = Literal["configured", "reported", "assumed"]
"""Where a candidate's ``served_context`` came from (ADR-0023 §4)."""


@dataclass(frozen=True, slots=True)
class ModelFacts:
    """What the registry knows about one model, in the shape routing consumes.

    Every optional field is ``None`` when the provider never reported it — never ``0``. A missing
    ``size_bytes`` means the VRAM estimate cannot be produced, which is a different statement from
    "this model needs no VRAM" (ADR-0016).

    Attributes:
        model_id: The local ULID primary key, unique per row. Routing's final tie-break, so the
            candidate order is total even for two rows sharing a canonical ID.
        canonical_id: ``provider/name@sha256:digest`` (ADR-0008). Display and lookup only.
        provider_kind: The provider family, e.g. ``"ollama"``.
        provider_model_name: The name this provider reports.
        artifact_digest: The digest, when the provider exposed one. The executor calls the
            provider with the full identity triple rather than the name alone, because a tag can
            be repointed between discovery and execution and a name-only call would silently run
            a different model (ADR-0008, ADR-0024).
        available: Whether the most recent discovery still saw it.
        unavailable_reason: Why not, when ``available`` is false.
        max_context: The **advertised** maximum. Never a constraint input on its own (ADR-0023);
            it is only ever the last resort for ``served_context``, and then flagged ``assumed``.
        size_bytes: On-disk weight size, the base of the VRAM estimate.
        parameter_count: Total parameters, the input to the parameter-band prior (routing §5.1).
        layers: Transformer block count, for the theoretical KV figure.
        kv_heads: Key/value head count, for the theoretical KV figure.
        head_dim: Per-head dimension, for the theoretical KV figure.
        is_remote: Whether this model is served by a remote provider. Gates
            ``allow_remote_providers`` and the cost factor.
    """

    model_id: str
    canonical_id: str
    provider_kind: str
    provider_model_name: str
    artifact_digest: str | None = None
    available: bool = True
    unavailable_reason: str | None = None
    max_context: int | None = None
    size_bytes: int | None = None
    parameter_count: int | None = None
    layers: int | None = None
    kv_heads: int | None = None
    head_dim: int | None = None
    is_remote: bool = False


@dataclass(frozen=True, slots=True)
class ProviderFacts:
    """The provider-level facts routing needs, lifted out of ModelRack's own dataclass.

    Kept as a plain domain value rather than :class:`modelrack.ProviderCapabilities` so that
    ``domain/`` stays free of an import it would otherwise need only for four booleans, and so a
    test can state a provider's shape in one line.

    Attributes:
        healthy: Whether the provider answered its last health probe.
        context_configurable: Whether the served context can be set by the caller. Load-bearing
            (ADR-0023 §4): false is what turns a derived context into ``assumed``.
        reported_served_context: The context the provider says it will serve, when it exposes one.
        supports_tool_use: Whether the provider accepts tool definitions.
        supports_structured_output: Whether it can be constrained to a JSON Schema.
        supports_streaming: Whether it can produce incremental output.
        is_remote: Whether this provider is somewhere other than this machine. Gates
            ``allow_remote_providers`` and the cost factor.
    """

    healthy: bool = True
    context_configurable: bool = False
    reported_served_context: int | None = None
    supports_tool_use: bool = False
    supports_structured_output: bool = False
    supports_streaming: bool = False
    is_remote: bool = False


@dataclass(frozen=True, slots=True)
class CapabilitySignal:
    """One piece of knowledge about one model's ability at one capability.

    Covers every source in routing §5/§5.1 uniformly, so scoring resolves precedence in one place
    instead of once per source.

    Attributes:
        capability_id: A SetSpec capability, e.g. ``"code_review"``.
        source: ``"benchmark"``, ``"production"``, ``"manual"``, ``"declared"`` or ``"prior"``.
        score: The ability, in ``[0.0, 1.0]``. For ``"declared"`` this is the stored 1.0 meaning
            "the provider declares this flag", **not** a scoring contribution — scoring maps it to
            a neutral prior (routing §5.1).
        confidence: How much weight the evidence policy attaches to it. LoadCoach applies this
            number; it never recomputes it (routing §5).
        runtime_profile_hash: The profile the measurement was taken under. Benchmark evidence only.
        machine_fingerprint: The machine it was taken on. Benchmark evidence only.
        measured_at: When. Benchmark and production evidence only.
        sample_count: How many observations stand behind it.
        match_state: ``"bound"``, ``"unmatched"`` or ``"ambiguous_name_only"`` for imported
            evidence (ADR-0022 §4). Anything but ``"bound"`` never contributes.
        age_days: How old the measurement is, in whole days from ``measured_at`` — never from
            ``computed_at`` (ADR-0022 §2). Computed where the clock lives, so that scoring stays
            a pure function of its inputs.
        stale: Whether the evidence carries a staleness badge.
        stale_reason: Which of ADR-0017's four reasons raised it.
        calibration: For a ``user.*`` capability, the judge's measured agreement with the person
            whose goal it is — what ADR-0032 §6 requires the explanation to state in words.
    """

    capability_id: str
    source: str
    score: float
    confidence: float
    runtime_profile_hash: str | None = None
    machine_fingerprint: str | None = None
    measured_at: datetime | None = None
    sample_count: int | None = None
    match_state: str | None = None
    age_days: int | None = None
    stale: bool = False
    stale_reason: str | None = None
    calibration: CalibrationFacts | None = None


@dataclass(frozen=True, slots=True)
class ServedContext:
    """The context a candidate will actually be served, and where that number came from."""

    tokens: int
    source: ServedContextSource


@dataclass(frozen=True, slots=True)
class RuntimeOverrides:
    """A request's ``overrides`` block (routing §10), as far as P3 implements it.

    Attributes:
        model: A canonical ID that bypasses scoring but not hard constraints.
        runtime_profile: Runtime settings that win over every configured level.
        disallow_fallback: Fail instead of naming fallbacks.
        require_evidence: Refuse to route on declared or manual priors.
    """

    model: str | None = None
    runtime_profile: RuntimeProfile | None = None
    disallow_fallback: bool = False
    require_evidence: bool = False


@dataclass(frozen=True, slots=True)
class ExecutionSubject:
    """One candidate: a model, the profile it would run under, and the context it would be served.

    Attributes:
        facts: The model.
        provider: The provider that would serve it.
        runtime_profile: The resolved profile. Never ``None`` — ``RuntimeProfile()`` is itself a
            legal profile meaning "provider defaults" (ADR-0023 §1).
        served_context: The context and its source.
        signals: Every capability signal known for this model, in a stable order.
    """

    facts: ModelFacts
    provider: ProviderFacts
    runtime_profile: RuntimeProfile
    served_context: ServedContext
    signals: tuple[CapabilitySignal, ...] = field(default=())

    @property
    def runtime_profile_hash(self) -> str:
        """The resolved profile's stable hash — what evidence must match (ADR-0023 §3)."""
        return self.runtime_profile.profile_hash


def resolve_runtime_profile(
    *,
    defaults: RuntimeProfile,
    per_model: RuntimeProfile | None = None,
    min_context_tokens: int = 0,
    context_configurable: bool = False,
    override: RuntimeProfile | None = None,
) -> RuntimeProfile:
    """Resolve the runtime profile one execution runs under (ADR-0023 §1).

    The chain is ``[runtime].default -> [runtime.models."<canonical_id>"] -> task-profile runtime
    settings -> overrides.runtime_profile``. At each level, a field left ``None`` means "say
    nothing here", so a later level fills it and an earlier level survives — a per-model override
    that sets only ``context_size`` does not erase the default's ``keep_alive``.

    The task-profile level is ADR-0023 §4's rule rather than a free-form settings block: where the
    provider's context is configurable and the profile declares ``min_context_tokens``, LoadCoach
    **sets** ``context_size`` instead of hoping — but only when no earlier level already stated
    one. An operator who explicitly configured 4 096 gets 4 096, and the profile requiring 16 384
    is then rejected by ``context_too_small`` rather than silently overridden.

    Args:
        defaults: The ``[runtime]`` section as a profile.
        per_model: The ``[runtime.models."<canonical_id>"]`` override, if any.
        min_context_tokens: The task profile's context requirement; 0 means none.
        context_configurable: Whether the provider will accept a context setting at all.
        override: The request's ``overrides.runtime_profile``, if any.

    Returns:
        The resolved profile. ``RuntimeProfile()`` (everything unset) is a legal result, and its
        hash is stable — there is no unprofiled execution.
    """
    resolved = _merge(defaults, per_model)
    resolved = _apply_task_profile_context(
        resolved,
        min_context_tokens=min_context_tokens,
        context_configurable=context_configurable,
    )
    return _merge(resolved, override)


def _merge(base: RuntimeProfile, layer: RuntimeProfile | None) -> RuntimeProfile:
    """Return ``base`` with every field ``layer`` actually states overriding it."""
    if layer is None:
        return base
    return RuntimeProfile(
        context_size=_first(layer.context_size, base.context_size),
        kv_cache_precision=_first(layer.kv_cache_precision, base.kv_cache_precision),
        gpu_layers=_first(layer.gpu_layers, base.gpu_layers),
        flash_attention=_first(layer.flash_attention, base.flash_attention),
        threads=_first(layer.threads, base.threads),
        batch_size=_first(layer.batch_size, base.batch_size),
        keep_alive=_first(layer.keep_alive, base.keep_alive),
        provider_options={**dict(base.provider_options), **dict(layer.provider_options)},
    )


def _apply_task_profile_context(
    profile: RuntimeProfile, *, min_context_tokens: int, context_configurable: bool
) -> RuntimeProfile:
    if min_context_tokens <= 0 or not context_configurable or profile.context_size is not None:
        return profile
    return RuntimeProfile(
        context_size=min_context_tokens,
        kv_cache_precision=profile.kv_cache_precision,
        gpu_layers=profile.gpu_layers,
        flash_attention=profile.flash_attention,
        threads=profile.threads,
        batch_size=profile.batch_size,
        keep_alive=profile.keep_alive,
        provider_options=dict(profile.provider_options),
    )


def _first[T](preferred: T | None, fallback: T | None) -> T | None:
    return fallback if preferred is None else preferred


def resolve_served_context(
    *, profile: RuntimeProfile, provider: ProviderFacts, max_context: int | None
) -> ServedContext | None:
    """Derive the context this candidate will actually be served (ADR-0023 §4).

    ``runtime_profile.context_size`` when set **and the provider will accept it** (``configured``);
    else the provider's own reported served context (``reported``); else the descriptor's
    advertised maximum, flagged ``assumed``.

    A context set on a provider that declares ``context_configurable=False`` is deliberately not
    reported as ``configured``: the provider will ignore the setting, and recording a context that
    never happened is the fabricated-measurement failure ADR-0023 was written against. Such a
    candidate falls through to ``reported``/``assumed`` here, and the constraint filter rejects it
    with ``context_not_configurable`` — the ask exists and cannot be honoured.

    Args:
        profile: The already-resolved runtime profile.
        provider: The provider that would serve the model.
        max_context: The descriptor's advertised maximum, or ``None`` if it never reported one.

    Returns:
        The context and its source, or ``None`` when not even an advertised maximum is known —
        which is an absence, not a zero, and the caller rejects the candidate rather than
        substituting a number.
    """
    if profile.context_size is not None and provider.context_configurable:
        return ServedContext(tokens=profile.context_size, source="configured")
    if provider.reported_served_context is not None:
        return ServedContext(tokens=provider.reported_served_context, source="reported")
    if max_context is not None:
        return ServedContext(tokens=max_context, source="assumed")
    return None


def signals_by_capability(
    signals: tuple[CapabilitySignal, ...],
) -> Mapping[str, tuple[CapabilitySignal, ...]]:
    """Group ``signals`` by capability, preserving each group's order.

    Args:
        signals: Every signal known for one model.

    Returns:
        Capability ID -> its signals, in the order given.
    """
    grouped: dict[str, list[CapabilitySignal]] = {}
    for signal in signals:
        grouped.setdefault(signal.capability_id, []).append(signal)
    return {capability: tuple(group) for capability, group in grouped.items()}
