"""loadcoach.services.routing — the routing pipeline, wired to the registry and persisted.

Routing §1's six steps, in order, over the models the registry knows:

1. Candidate set — every model discovery has seen, each resolved into an execution subject.
2. Hard constraints — routing §4, first failure recorded and evaluation stopped.
3. Capability scoring — routing §5, with the absent-evidence rule.
4. Adjustment factors — routing §6.
5. Ranking — routing §7's total order, primary plus fallbacks.
6. The explanation, persisted for every decision.

The arithmetic itself lives in :mod:`loadcoach.domain.routing` and is pure; this module supplies
it with values, times it, and writes the result down. Given the same registry, evidence,
telemetry snapshot and request, it produces the same decision (routing §12) — which is why the
clock and the telemetry snapshot are parameters rather than something this module reaches for.

**No admission policy here.** The VRAM estimate is consulted as a hard constraint and nothing
more: nothing defers, waits for headroom, re-evaluates on unload, or sums concurrent jobs against
a device. That is P5's queue, and the sequencing note in the development plan puts it there
deliberately.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, ClassVar, cast

from baseaicore import RuntimeProfile, SuiteError, new_id
from sqlalchemy import select
from weightsdb import upsert

from loadcoach.domain.authorization import Principal, authorize
from loadcoach.domain.registry import (
    DECLARED_CONFIDENCE,
    DECLARED_SCORE,
    geometry_from_json,
)
from loadcoach.domain.reliability import neutral_factor
from loadcoach.domain.routing.constraints import (
    ConstraintInputs,
    Rejection,
    estimate_vram,
    evaluate_constraints,
)
from loadcoach.domain.routing.context_budget import ContextBudget, budget_context
from loadcoach.domain.routing.explanation import (
    CONFIDENCE_POLICY_VERSION,
    STRATEGY_VERSION,
    Explanation,
    RejectedCandidate,
    build_explanation,
)
from loadcoach.domain.routing.ranking import RankedCandidate, rank_candidates
from loadcoach.domain.routing.scoring import (
    ScoringInputs,
    adjustment_factors,
    parameter_band_priors,
    score_subject,
)
from loadcoach.domain.routing.subject import (
    AdapterFacts,
    CapabilitySignal,
    ExecutionSubject,
    ModelFacts,
    ProviderFacts,
    RuntimeOverrides,
    resolve_runtime_profile,
    resolve_served_context,
)
from loadcoach.domain.task_profile import TaskProfileConstraints
from loadcoach.infrastructure.db.models import (
    Model,
    ModelCapability,
    RoutingCandidate,
    RoutingDecision,
)
from loadcoach.infrastructure.db.models import RuntimeProfile as RuntimeProfileModel
from loadcoach.services.adapters import AdapterNotFound, adapter_facts_by_base_name
from loadcoach.services.evidence import bound_signals_for_routing, evidence_overview
from loadcoach.services.reliability import factors_for_task
from loadcoach.services.task_profiles import StoredTaskProfile, list_stored_task_profiles

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from sqlalchemy.orm import Session
    from sweatmeter import TelemetrySnapshot

    from loadcoach.config import (
        EvidenceSettings,
        RoutingSettings,
        RuntimeSettings,
        TelemetrySettings,
    )
    from loadcoach.services.database import Database

__all__ = [
    "ConstraintsNotTightening",
    "DecisionSummary",
    "NoEligibleModel",
    "RouteRequest",
    "RoutingPolicy",
    "RoutingResult",
    "TaskProfileNotFound",
    "load_task_profile",
    "read_decision",
    "recent_decisions",
    "route",
    "telemetry_snapshot_json",
]


class TaskProfileNotFound(SuiteError):
    """The request named a task profile that is not in the registry (spec §13)."""

    code: ClassVar[str] = "TASK_PROFILE_NOT_FOUND"


class NoEligibleModel(SuiteError):
    """No candidate survived the hard constraints.

    ``details`` always carries every candidate and the constraint that rejected it (spec §13,
    api.md §10) — "nothing was eligible" is useless without the numbers.
    """

    code: ClassVar[str] = "NO_ELIGIBLE_MODEL"


class ConstraintsNotTightening(SuiteError):
    """A request's ``constraints`` block tried to loosen the task profile's own constraints.

    Routing §2 makes a profile's constraints hard, and routing §10's override table has no entry
    for relaxing one. A request may therefore narrow what the profile already allows — add a
    required capability, raise a floor — but never widen it, or a caller could route around the
    policy the profile exists to state.
    """

    code: ClassVar[str] = "VALIDATION_ERROR"


@dataclass(frozen=True, slots=True)
class RouteRequest:
    """One ``POST /route`` (or ``loadcoach route explain``) request (api.md §3).

    Attributes:
        task: The task profile ID.
        estimated_input_tokens: The caller's own count or estimate. ``None`` means the caller did
            not say, and context budgeting is skipped rather than guessed at.
        max_output_tokens: Overrides the profile's allowance when given.
        constraints: Additional constraints, which may only tighten the profile's own.
        require_capabilities: Capabilities this *request* needs, whatever its task profile
            requires — a body carrying tools requires ``tool_use`` (ADR-0075). Unioned with the
            profile's, so it can only narrow the field; it never scores and never loosens.
        think: The request's own thinking control (``sampling.think``), or ``None`` when it set
            none and the task profile's ``execution.think`` decides. Set either way, it requires
            ``thinking_control`` of every candidate (ADR-0099 rule 4). Carried separately from
            ``require_capabilities`` because ``thinking_control`` is a provider flag and not a
            SetSpec capability, and ``requires_capabilities`` is validated against that vocabulary.
        overrides: Routing §10's overrides.
        data_classification: The caller's own declaration. **Not an override** — it selects
            nothing and relaxes nothing; it is one input to the
            ``adapter_classification_conflict`` constraint, where the effective classification is
            ``max(caller, adapter)`` (ADR-0065 rule 2). Because the join is a ``max()``, a
            declaration can only make an adapter candidate *less* eligible, never more.
    """

    task: str
    estimated_input_tokens: int | None = None
    max_output_tokens: int | None = None
    constraints: TaskProfileConstraints | None = None
    require_capabilities: tuple[str, ...] = ()
    think: bool | None = None
    overrides: RuntimeOverrides = field(default_factory=RuntimeOverrides)
    data_classification: str | None = None


@dataclass(frozen=True, slots=True)
class RoutingPolicy:
    """The configured policy routing applies, lifted out of :class:`~loadcoach.config.Settings`.

    Passed as one value so the pipeline can be driven from a test without building a whole
    settings object, and so every knob a decision depended on is visible in one place.
    """

    strategy: str = "weighted_evidence"
    min_confidence: float = 0.05
    prefer_resident_bonus: float = 0.05
    base_switch_penalty: float = 0.10
    require_adapter_evidence: bool = True
    min_present_weight: float = 0.5
    remote_cost_factor: float = 0.9
    vram_headroom_bytes: int = 512 * 1024 * 1024
    runtime_defaults: RuntimeProfile = field(default_factory=RuntimeProfile)
    runtime_per_model: Mapping[str, RuntimeProfile] = field(default_factory=dict)
    machine_fingerprint: str | None = None
    """This machine's fingerprint, from SweatMeter at startup (spec §10). ``None`` when it could
    not be produced, which admits evidence from anywhere rather than refusing all of it."""
    evidence_url: str = ""
    """``[evidence] freeweight_url``, so a decision can say whether a source is configured at all
    — which is a different state from one that is unavailable."""

    @classmethod
    def from_settings(
        cls,
        *,
        routing: RoutingSettings,
        runtime: RuntimeSettings,
        telemetry: TelemetrySettings,
        evidence: EvidenceSettings | None = None,
        machine_fingerprint: str | None = None,
    ) -> RoutingPolicy:
        """Build the policy from the three settings sections that shape a decision.

        ``[runtime]``'s TOML sentinels become the ``None`` a :class:`~baseaicore.RuntimeProfile`
        uses for "provider decides": ``context_size = 0`` and ``kv_cache_precision = ""`` are that
        sentinel in TOML's type system, and ``flash_attention = false`` is left unset rather than
        sent as an explicit ``false``, because a profile that says nothing about flash attention
        must hash the same as one written before the field existed.
        """
        return cls(
            strategy=routing.strategy,
            min_confidence=routing.min_confidence,
            prefer_resident_bonus=routing.prefer_resident_bonus,
            base_switch_penalty=routing.base_switch_penalty,
            require_adapter_evidence=routing.require_adapter_evidence,
            min_present_weight=routing.min_present_weight,
            remote_cost_factor=routing.remote_cost_factor,
            vram_headroom_bytes=telemetry.vram_headroom_bytes,
            runtime_defaults=RuntimeProfile(
                context_size=runtime.context_size or None,
                kv_cache_precision=runtime.kv_cache_precision or None,
                flash_attention=True if runtime.flash_attention else None,
                keep_alive=runtime.keep_alive or None,
            ),
            runtime_per_model={
                canonical_id: RuntimeProfile(context_size=override.context_size)
                for canonical_id, override in runtime.models.items()
            },
            machine_fingerprint=machine_fingerprint,
            evidence_url="" if evidence is None else evidence.freeweight_url.strip(),
        )


@dataclass(frozen=True, slots=True)
class RoutingResult:
    """A completed decision: the explanation, and the profile it was made against."""

    explanation: Explanation
    task_profile: StoredTaskProfile
    budget: ContextBudget | None


def load_task_profile(database: Database, task: str) -> StoredTaskProfile:
    """Return the stored task profile named ``task``.

    Args:
        database: The application's database handle.
        task: The dotted profile ID.

    Returns:
        The profile, taking the most recently updated row when several versions are stored.

    Raises:
        TaskProfileNotFound: No profile with that ID is stored.
    """
    matches = [
        profile
        for profile in list_stored_task_profiles(database)
        if profile.profile_id == task and profile.enabled
    ]
    if not matches:
        raise TaskProfileNotFound(
            f"No enabled task profile {task!r} is registered.",
            details={"task_profile_id": task},
        )
    return matches[0]


def _merged_constraints(
    profile: StoredTaskProfile, requested: TaskProfileConstraints | None
) -> TaskProfileConstraints:
    """Combine the profile's constraints with the request's, refusing any loosening."""
    base = TaskProfileConstraints.model_validate(profile.constraints)
    if requested is None:
        return base
    problems: list[str] = []
    if requested.min_context_tokens < base.min_context_tokens and requested.min_context_tokens:
        problems.append("constraints.min_context_tokens")
    if requested.allow_remote_providers and not base.allow_remote_providers:
        problems.append("constraints.allow_remote_providers")
    if (
        requested.max_latency_p95_seconds is not None
        and base.max_latency_p95_seconds is not None
        and requested.max_latency_p95_seconds > base.max_latency_p95_seconds
    ):
        problems.append("constraints.max_latency_p95_seconds")
    for capability, floor in requested.min_capability_scores.items():
        if (
            capability in base.min_capability_scores
            and floor < base.min_capability_scores[capability]
        ):
            problems.append(f"constraints.min_capability_scores.{capability}")
    if problems:
        raise ConstraintsNotTightening(
            "Request constraints may only tighten the task profile's own; these would loosen it.",
            details={"fields": sorted(problems)},
        )
    latency = base.max_latency_p95_seconds
    if requested.max_latency_p95_seconds is not None:
        latency = (
            requested.max_latency_p95_seconds
            if latency is None
            else min(latency, requested.max_latency_p95_seconds)
        )
    return TaskProfileConstraints(
        min_context_tokens=max(base.min_context_tokens, requested.min_context_tokens),
        requires_capabilities=tuple(
            sorted(set(base.requires_capabilities) | set(requested.requires_capabilities))
        ),
        max_latency_p95_seconds=latency,
        min_capability_scores={
            capability: max(
                base.min_capability_scores.get(capability, 0.0),
                requested.min_capability_scores.get(capability, 0.0),
            )
            for capability in set(base.min_capability_scores) | set(requested.min_capability_scores)
        },
        exclude_models=tuple(sorted(set(base.exclude_models) | set(requested.exclude_models))),
        allow_remote_providers=base.allow_remote_providers and requested.allow_remote_providers,
    )


def _facts_for(model: Model, *, is_remote: bool) -> ModelFacts:
    """Lift one registry row into routing's shape, with the egress class it was discovered under.

    ``is_remote`` is the caller's fallback for a row whose ``provider_name`` is empty — a model
    discovered before named registration existed, or through a bare provider handle. A row that
    names its registration carries that registration's declared flag instead (ADR-0055 rule 4),
    which is what lets one pool hold local and remote candidates at once.
    """
    geometry = model.descriptor_json
    return ModelFacts(
        model_id=model.id,
        canonical_id=model.canonical_id,
        provider_kind=model.provider_kind,
        provider_model_name=model.provider_model_name,
        artifact_digest=model.artifact_digest,
        available=model.available,
        unavailable_reason=model.unavailable_reason,
        enabled=model.enabled,
        max_context=model.max_context,
        size_bytes=model.size_bytes,
        parameter_count=model.parameter_count,
        layers=geometry_from_json(geometry, "layers"),
        kv_heads=geometry_from_json(geometry, "kv_heads"),
        head_dim=geometry_from_json(geometry, "head_dim"),
        provider_name=model.provider_name,
        is_remote=model.is_remote if model.provider_name else is_remote,
    )


def _signals_for(rows: Sequence[ModelCapability]) -> tuple[CapabilitySignal, ...]:
    return tuple(
        CapabilitySignal(
            capability_id=row.capability_id,
            source=row.source,
            score=row.score if row.score is not None else 0.0,
            confidence=row.confidence,
        )
        for row in rows
        if row.score is not None
    )


def _read_candidates(
    database: Database,
    *,
    provider: ProviderFacts,
    provider_facts_by_name: Mapping[str, ProviderFacts],
    weights: Mapping[str, float],
    now: datetime,
    machine_fingerprint: str | None,
) -> tuple[
    tuple[ModelFacts, ProviderFacts, tuple[CapabilitySignal, ...], AdapterFacts | None], ...
]:
    """Read every model the registry knows, with every capability signal that may score it.

    Two sources, one signal type: ``model_capabilities`` for declared flags, manual scores and
    production statistics, and ``capability_evidence`` for FreeWeight's measurements. P3 built
    the signal so that adding the second source would be a query rather than a reshaping, and
    that is what this is.

    The benchmark half filters on ``match_state = 'bound'`` and applies the ``user.*`` opt-in
    before a signal exists at all — see
    :func:`~loadcoach.services.evidence.bound_signals_for_routing`. It is keyed on the **subject**,
    ``(model_id, adapter_key)``, so a base candidate takes the base's measurements and an adapter
    candidate takes its own: no evidence crosses between subjects on one base (ADR-0081).

    Each candidate carries **its own** registration's provider facts (ADR-0055 rule 3: one pool,
    tagged), falling back to ``provider`` for a row whose registration is not in the map — a model
    discovered before named registration, or one whose registration has since been removed from
    the configuration. Filtering, scoring and ranking are unchanged code over a larger pool.
    """
    with database.read() as session:
        models = session.execute(select(Model).order_by(Model.canonical_id)).scalars().all()
        capability_rows = session.execute(select(ModelCapability)).scalars().all()
    by_model: dict[str, list[ModelCapability]] = {}
    for row in capability_rows:
        by_model.setdefault(row.model_id, []).append(row)
    evidence = bound_signals_for_routing(
        database,
        weights=weights,
        now=now,
        local_machine_fingerprint=machine_fingerprint,
    )
    adapters = adapter_facts_by_base_name(database)
    candidates: list[
        tuple[ModelFacts, ProviderFacts, tuple[CapabilitySignal, ...], AdapterFacts | None]
    ] = []
    for model in models:
        facts = _facts_for(model, is_remote=provider.is_remote)
        candidate_provider = provider_facts_by_name.get(model.provider_name, provider)
        signals = _signals_for(by_model.get(model.id, [])) + evidence.get((model.id, ""), ())
        candidates.append((facts, candidate_provider, signals, None))
        if not candidate_provider.adapter_hot_swap:
            # ADR-0062 decision 5: a provider that cannot hot-swap contributes no adapter
            # subjects at all, which is what keeps ADR-0065's local-only rule true by
            # construction rather than by a check somebody could forget.
            continue
        for adapter in adapters.get(facts.provider_model_name, ()):
            subject_signals = _adapter_signals(adapter) + evidence.get(
                (model.id, adapter.adapter_id), ()
            )
            candidates.append((facts, candidate_provider, subject_signals, adapter))
    return tuple(candidates)


def _adapter_signals(adapter: AdapterFacts) -> tuple[CapabilitySignal, ...]:
    """Return the **declared** half of an adapter subject's signals: its manifest's claims.

    The vocabulary terms the manifest declares (ADR-0064 rule 1), at the same declared score and
    confidence a provider flag gets — a statement, never a measurement, which is why
    ``require_adapter_evidence`` refuses to route on these alone.

    The measured half arrives separately, from
    :func:`~loadcoach.services.evidence.bound_signals_for_routing` keyed on this subject, and the
    caller adds it. It is that subject's own imported evidence and nothing else: an adapter subject
    inherits **nothing** from its base and nothing from a sibling, because a benchmark taken on the
    bare weights describes the bare weights (ADR-0081, ADR-0059, ADR-0058 §4). An adapter nobody
    has measured therefore still carries declarations only, and is still rejected
    ``adapter_unmeasured``.
    """
    return tuple(
        CapabilitySignal(
            capability_id=capability_id,
            source="declared",
            score=DECLARED_SCORE,
            confidence=DECLARED_CONFIDENCE,
        )
        for capability_id in adapter.declared_capabilities
    )


def telemetry_snapshot_json(snapshot: TelemetrySnapshot | None) -> dict[str, Any] | None:
    """Render the telemetry a decision read into the form it is stored in.

    Only the fields routing actually consulted, so a stored decision names its inputs without
    carrying a whole machine profile. An unreported figure is stored as ``null``, never as ``0``.

    Args:
        snapshot: The observation routing read, or ``None`` if it read none.

    Returns:
        The mapping, or ``None``.
    """
    if snapshot is None:
        return None
    from baseaicore import is_supported
    from baseaicore.timeutil import to_rfc3339

    return {
        "timestamp": to_rfc3339(snapshot.timestamp),
        "ram_available_bytes": (
            int(snapshot.ram_available_bytes)
            if is_supported(snapshot.ram_available_bytes)
            else None
        ),
        "gpus": [
            {
                "index": gpu.index,
                "vram_total_bytes": (
                    int(gpu.vram_total_bytes) if is_supported(gpu.vram_total_bytes) else None
                ),
                "vram_used_bytes": (
                    int(gpu.vram_used_bytes) if is_supported(gpu.vram_used_bytes) else None
                ),
            }
            for gpu in snapshot.gpus
        ],
    }


def _overrides_json(overrides: RuntimeOverrides) -> dict[str, Any] | None:
    if (
        overrides.model is None
        and overrides.adapter is None
        and overrides.runtime_profile is None
        and not overrides.disallow_fallback
        and not overrides.require_evidence
        and not overrides.ignore_residency
    ):
        return None
    return {
        "model": overrides.model,
        "adapter": overrides.adapter,
        "ignore_residency": overrides.ignore_residency,
        "runtime_profile_hash": (
            None if overrides.runtime_profile is None else overrides.runtime_profile.profile_hash
        ),
        "disallow_fallback": overrides.disallow_fallback,
        "require_evidence": overrides.require_evidence,
    }


def route(
    database: Database,
    request: RouteRequest,
    *,
    provider: ProviderFacts,
    provider_facts_by_name: Mapping[str, ProviderFacts] | None = None,
    policy: RoutingPolicy,
    snapshot: TelemetrySnapshot | None = None,
    resident_models: frozenset[str] = frozenset(),
    open_circuit_breakers: frozenset[str] | None = None,
    resident_devices: Mapping[str, frozenset[int]] | None = None,
    circuit_breaker_details: Mapping[str, Mapping[str, object]] | None = None,
    now: datetime,
    persist: bool = True,
    principal: Principal | None = None,
) -> RoutingResult:
    """Run the whole routing pipeline and persist the decision.

    Args:
        database: The application's database handle.
        request: What to route.
        provider: The provider's own capabilities, which gate the context source and the
            capability constraints. With more than one registration this is the fallback for a
            candidate whose registration is not named in ``provider_facts_by_name``.
        provider_facts_by_name: One entry per registered provider, keyed by its registration name
            (ADR-0055). A candidate is evaluated against **its own** registration's capabilities
            and egress class; ``None`` — the default — is the single-provider case, where every
            candidate uses ``provider`` and the pool is exactly what LoadCoach 1.0 produced.
        policy: The configured routing policy.
        snapshot: The telemetry the resource constraints read. ``None`` skips them, which is what
            a machine with no telemetry reader honestly supports — not a fabricated zero.
        resident_models: Canonical IDs currently loaded, for the residency tie-break.
        open_circuit_breakers: Canonical IDs the breaker currently excludes. ``None`` — the
            default — means *no breaker registry existed to consult* (a one-shot process such
            as the CLI), which raises the ``breaker_state_unavailable`` flag on the explanation
            rather than silently assuming an empty set (F3/M5C-3). A caller with a runtime
            passes its real set, even when that set is empty.
        resident_devices: Canonical ID -> devices the model is resident on; a resident model
            fits on its device whatever the estimate says (queue §5, admission).
        circuit_breaker_details: The open breakers' records, for the rejection detail.
        now: The instant the request arrived. Injected, so a decision is reproducible.
        persist: Whether to write the decision. ``False`` is for replaying a stored decision's
            inputs to prove it reproduces (acceptance criterion 3).

    Returns:
        The :class:`RoutingResult`.

    Raises:
        TaskProfileNotFound: No such enabled task profile.
        ConstraintsNotTightening: The request's constraints would loosen the profile's.
        NoEligibleModel: Every candidate was rejected. ``details`` names each one and why.
    """
    authorize(principal, "write")
    breaker_state_unavailable = open_circuit_breakers is None
    open_circuit_breakers = open_circuit_breakers or frozenset()
    started = datetime.now(tz=now.tzinfo)
    profile = load_task_profile(database, request.task)
    constraints = _merged_constraints(profile, request.constraints)
    # ADR-0075: what the request itself needs, unioned onto what the profile needs. A union can
    # only narrow, so it cannot collide with `_merged_constraints`' refusal to loosen.
    request_capabilities = frozenset(request.require_capabilities)
    if request_capabilities - set(constraints.requires_capabilities):
        constraints = constraints.model_copy(
            update={
                "requires_capabilities": tuple(
                    sorted(request_capabilities | set(constraints.requires_capabilities))
                )
            }
        )
    execution = profile.execution
    # ADR-0099 rule 4: a set thinking control — the request's over the profile's, as `sampling_for`
    # resolves it — requires `thinking_control` of every candidate, labelled with who asked.
    requires_thinking_control = (
        "request"
        if request.think is not None
        else "task_profile"
        if execution.get("think") is not None
        else None
    )
    max_output_tokens = request.max_output_tokens or int(
        cast("int", execution.get("max_output_tokens", 1024))
    )
    min_output_tokens = cast("int | None", execution.get("min_output_tokens"))
    fallback_depth = int(cast("int", execution.get("fallback_depth", 0)))

    candidates = _read_candidates(
        database,
        provider=provider,
        provider_facts_by_name=provider_facts_by_name or {},
        weights=profile.weights,
        now=now,
        machine_fingerprint=policy.machine_fingerprint,
    )
    overview = evidence_overview(database, configured_url=policy.evidence_url)
    # Production evidence enters here and only here (routing §6, §11): one lookup per decision
    # on data model §4's index, then the pure factor per candidate. A model with no rows is
    # neutral, and the neutral record still says how many attempts it has seen.
    reliability = factors_for_task(database, task_profile_id=profile.profile_id)
    priors = parameter_band_priors(
        {facts.canonical_id: facts.parameter_count for facts, _, _, _ in candidates}
    )
    scoring = ScoringInputs(
        weights=profile.weights,
        min_confidence=policy.min_confidence,
        parameter_priors=priors,
        require_evidence=request.overrides.require_evidence,
        machine_fingerprint=policy.machine_fingerprint,
    )

    ranked: list[RankedCandidate] = []
    rejected: list[RejectedCandidate] = []
    selected_budget: ContextBudget | None = None

    top_weighted = _top_weighted_capability(profile.weights)
    pinned_adapter = request.overrides.adapter
    if pinned_adapter is not None:
        _refuse_unknown_adapter(pinned_adapter, candidates)
    for facts, candidate_provider, signals, adapter in candidates:
        subject, missing_context = _build_subject(
            facts,
            signals,
            provider=candidate_provider,
            policy=policy,
            request=request,
            constraints=constraints,
            adapter=adapter,
        )
        if subject is None:
            rejected.append(
                RejectedCandidate(subject=missing_context, rejection=_no_context_rejection(facts))
            )
            continue

        if request.overrides.model is not None and facts.canonical_id != request.overrides.model:
            continue

        # An adapter pin selects, exactly as a model pin does (ADR-0064 rule 4). Every other
        # subject — including the bare base this adapter would run on — leaves the pool, so the
        # pin cannot silently fall back to serving without the adapter.
        if pinned_adapter is not None and (adapter is None or adapter.name != pinned_adapter):
            continue

        estimate = estimate_vram(
            size_bytes=facts.size_bytes,
            served_context=subject.served_context.tokens,
            layers=facts.layers,
            kv_heads=facts.kv_heads,
            head_dim=facts.head_dim,
            kv_cache_precision=subject.runtime_profile.kv_cache_precision,
        )
        fit = score_subject(subject, scoring)
        budget = (
            None
            if request.estimated_input_tokens is None
            else budget_context(
                estimated_input_tokens=request.estimated_input_tokens,
                max_output_tokens=max_output_tokens,
                served_context=subject.served_context.tokens,
                served_context_source=subject.served_context.source,
                min_output_tokens=min_output_tokens,
            )
        )
        rejection, _fits, target_gpu_index = evaluate_constraints(
            subject,
            estimate,
            ConstraintInputs(
                min_context_tokens=constraints.min_context_tokens,
                requires_capabilities=constraints.requires_capabilities,
                min_capability_scores=constraints.min_capability_scores,
                exclude_models=constraints.exclude_models,
                allow_remote_providers=constraints.allow_remote_providers,
                required_context=None if budget is None or budget.fits else budget.required_context,
                resolved_capabilities={score.capability_id: score for score in fit.capabilities},
                snapshot=snapshot,
                vram_headroom_bytes=policy.vram_headroom_bytes,
                open_circuit_breakers=open_circuit_breakers,
                resident_devices=resident_devices or {},
                circuit_breaker_details=circuit_breaker_details or {},
                request_capabilities=request_capabilities,
                requires_thinking_control=requires_thinking_control,
                # A pin is not routed selection, so the evidence gate does not apply to it: an
                # unmeasured adapter *is* pinnable, and every other hard constraint still runs
                # (routing §10).
                require_adapter_evidence=(
                    policy.require_adapter_evidence and pinned_adapter is None
                ),
                top_weighted_capability=top_weighted,
                caller_data_classification=request.data_classification,
            ),
        )
        if rejection is not None:
            detail = dict(rejection.detail)
            if budget is not None and rejection.reason == "context_limit_exceeded":
                detail["context_budget"] = budget.as_json()
            rejected.append(
                RejectedCandidate(
                    subject=subject,
                    rejection=Rejection(rejection.reason, detail),
                    estimate=estimate,
                )
            )
            continue

        ranked.append(
            RankedCandidate(
                subject=subject,
                fit=fit,
                factors=adjustment_factors(
                    subject,
                    resident_models=resident_models,
                    prefer_resident_bonus=policy.prefer_resident_bonus,
                    base_switch_penalty=policy.base_switch_penalty,
                    ignore_residency=request.overrides.ignore_residency,
                    remote_cost_factor=policy.remote_cost_factor,
                    # Keyed on the subject (ADR-0067): an adapter never borrows its base's
                    # numbers, and never lends it its own.
                    reliability=reliability.get(
                        (facts.model_id, "" if adapter is None else adapter.adapter_id),
                        neutral_factor(),
                    ),
                ),
                estimated_vram_bytes=estimate.total_bytes,
                target_gpu_index=target_gpu_index,
                resident=facts.canonical_id in resident_models,
            )
        )

    ranking = rank_candidates(
        tuple(ranked),
        fallback_depth=fallback_depth,
        disallow_fallback=request.overrides.disallow_fallback,
    )
    if ranking.primary is not None and request.estimated_input_tokens is not None:
        selected_budget = budget_context(
            estimated_input_tokens=request.estimated_input_tokens,
            max_output_tokens=max_output_tokens,
            served_context=ranking.primary.subject.served_context.tokens,
            served_context_source=ranking.primary.subject.served_context.source,
            min_output_tokens=min_output_tokens,
        )

    duration_ms = max(int((datetime.now(tz=now.tzinfo) - started).total_seconds() * 1000), 0)
    explanation = build_explanation(
        decision_id=new_id(),
        task_profile_id=profile.profile_id,
        task_profile_version=profile.version,
        strategy_name=policy.strategy,
        requested_at=now,
        duration_ms=duration_ms,
        ranking=ranking,
        rejected=tuple(rejected),
        budget=selected_budget,
        telemetry_snapshot=telemetry_snapshot_json(snapshot),
        overrides=_overrides_json(request.overrides),
        min_present_weight=policy.min_present_weight,
        evidence=overview,
        breaker_state_unavailable=breaker_state_unavailable,
    )

    if persist:
        _persist(database, explanation, profile=profile, now=now)

    if ranking.primary is None:
        raise NoEligibleModel(
            f"No model satisfied task profile {profile.profile_id!r}'s constraints.",
            details={
                "decision_id": explanation.decision_id,
                "task_profile_id": profile.profile_id,
                "candidates": explanation.payload["rejected"],
            },
        )
    return RoutingResult(explanation=explanation, task_profile=profile, budget=selected_budget)


def _no_context_rejection(facts: ModelFacts) -> Rejection:
    return Rejection(
        "context_too_small",
        {
            "served_context": None,
            "served_context_source": None,
            "problem": (
                "the provider reported no served context and the descriptor advertises no "
                "maximum, so no context can be established for this model"
            ),
            "canonical_id": facts.canonical_id,
        },
    )


def _refuse_unknown_adapter(
    pinned: str,
    candidates: tuple[
        tuple[ModelFacts, ProviderFacts, tuple[CapabilitySignal, ...], AdapterFacts | None], ...
    ],
) -> None:
    """Refuse a pin no candidate could ever honour, before any candidate is evaluated.

    A pin naming an adapter that no hot-swapping provider was offered is not "nothing was
    eligible": there is no candidate to reject and no numbers to show, so ``NO_ELIGIBLE_MODEL``
    with an empty rejection list would be the least useful possible answer. It is a 404 naming
    what does exist (api.md §10).

    Args:
        pinned: The adapter name the request asked for.
        candidates: The expanded candidate list.

    Raises:
        AdapterNotFound: No candidate carries an adapter of that name.
    """
    known = sorted({adapter.name for _, _, _, adapter in candidates if adapter is not None})
    if pinned in known:
        return
    available = ", ".join(known) or "none"
    raise AdapterNotFound(
        f"no adapter named {pinned!r} is registered on any provider that could serve this "
        f"request; adapters available here: {available}",
        details={"adapter": pinned, "known_adapters": known},
    )


def _top_weighted_capability(weights: Mapping[str, float]) -> str | None:
    """Return the profile's heaviest capability, or ``None`` for a profile that weights nothing.

    Ties break on the capability ID so that two profiles with the same weights demand evidence of
    the same capability twice running — a rejection reason is caller-visible vocabulary, and one
    that moved between two identical decisions would be unexplainable.
    """
    if not weights:
        return None
    return max(sorted(weights), key=lambda capability: weights[capability])


def _build_subject(
    facts: ModelFacts,
    signals: tuple[CapabilitySignal, ...],
    *,
    provider: ProviderFacts,
    policy: RoutingPolicy,
    request: RouteRequest,
    constraints: TaskProfileConstraints,
    adapter: AdapterFacts | None = None,
) -> tuple[ExecutionSubject | None, ExecutionSubject]:
    """Resolve one model into an execution subject.

    Returns:
        ``(subject, placeholder)``. ``subject`` is ``None`` when no served context could be
        established at all; ``placeholder`` is then a subject carrying a zero-token context, used
        only so the rejection can still name the model and its resolved profile.
    """
    profile = resolve_runtime_profile(
        defaults=policy.runtime_defaults,
        per_model=policy.runtime_per_model.get(facts.canonical_id),
        min_context_tokens=constraints.min_context_tokens,
        context_configurable=provider.context_configurable,
        override=request.overrides.runtime_profile,
        adapters_registered=provider.adapters_registered,
    )
    served = resolve_served_context(
        profile=profile, provider=provider, max_context=facts.max_context
    )
    if served is None:
        from loadcoach.domain.routing.subject import ServedContext

        placeholder = ExecutionSubject(
            facts=facts,
            provider=provider,
            runtime_profile=profile,
            served_context=ServedContext(tokens=0, source="assumed"),
            signals=signals,
            adapter=adapter,
        )
        return None, placeholder
    subject = ExecutionSubject(
        facts=facts,
        provider=provider,
        runtime_profile=profile,
        served_context=served,
        signals=signals,
        adapter=adapter,
    )
    return subject, subject


def _runtime_profile_id(session: Session, profile: RuntimeProfile, *, now: datetime) -> str:
    """Upsert one resolved runtime profile and return its row ID (ADR-0023 §2)."""
    upsert(
        session,
        RuntimeProfileModel,
        {
            "profile_hash": profile.profile_hash,
            "context_size": profile.context_size,
            "kv_cache_precision": profile.kv_cache_precision,
            "gpu_layers": profile.gpu_layers,
            "flash_attention": profile.flash_attention,
            "threads": profile.threads,
            "batch_size": profile.batch_size,
            "keep_alive": profile.keep_alive,
            "provider_options_json": dict(profile.provider_options) or None,
            "created_at": now,
        },
        index_elements=["profile_hash"],
        no_update=frozenset({"created_at"}),
    )
    row = session.execute(
        select(RuntimeProfileModel).where(RuntimeProfileModel.profile_hash == profile.profile_hash)
    ).scalar_one()
    return row.id


def _persist(
    database: Database, explanation: Explanation, *, profile: StoredTaskProfile, now: datetime
) -> None:
    """Write the decision, its candidates and every resolved runtime profile."""
    payload = explanation.payload
    primary = explanation.ranking.primary
    with database.write() as session:
        profile_ids: dict[str, str] = {}
        for candidate in explanation.ranking.ordered:
            profile_ids[candidate.subject.runtime_profile_hash] = _runtime_profile_id(
                session, candidate.subject.runtime_profile, now=now
            )
        for item in explanation.rejected:
            profile_ids[item.subject.runtime_profile_hash] = _runtime_profile_id(
                session, item.subject.runtime_profile, now=now
            )

        session.add(
            RoutingDecision(
                id=explanation.decision_id,
                task_profile_id=profile.profile_id,
                task_profile_version=profile.version,
                strategy_name=str(cast("dict[str, Any]", payload["strategy"])["name"]),
                strategy_version=STRATEGY_VERSION,
                confidence_policy_version=CONFIDENCE_POLICY_VERSION,
                requested_at=now,
                duration_ms=int(cast("int", payload["duration_ms"])),
                selected_model_id=None if primary is None else primary.subject.facts.model_id,
                selected_adapter_id=(
                    None
                    if primary is None or primary.subject.adapter is None
                    else primary.subject.adapter.adapter_id
                ),
                selected_subject_canonical_id=(
                    None if primary is None else primary.subject.subject_canonical_id
                ),
                selected_score=None if primary is None else primary.final_score,
                selected_runtime_profile_id=(
                    None if primary is None else profile_ids[primary.subject.runtime_profile_hash]
                ),
                selected_served_context=(
                    None if primary is None else primary.subject.served_context.tokens
                ),
                selected_served_context_source=(
                    None if primary is None else primary.subject.served_context.source
                ),
                selected_target_gpu_index=None if primary is None else primary.target_gpu_index,
                flags_json=list(explanation.flags),
                evidence_summary_json=payload["evidence_summary"],
                overrides_json=payload["overrides"],
                telemetry_snapshot_json=payload["telemetry_snapshot"],
                explanation_json=payload,
                created_at=now,
            )
        )
        session.flush()
        for candidate in explanation.ranking.ordered:
            session.add(
                RoutingCandidate(
                    decision_id=explanation.decision_id,
                    model_id=candidate.subject.facts.model_id,
                    adapter_id=(
                        None
                        if candidate.subject.adapter is None
                        else candidate.subject.adapter.adapter_id
                    ),
                    subject_canonical_id=candidate.subject.subject_canonical_id,
                    runtime_profile_id=profile_ids[candidate.subject.runtime_profile_hash],
                    served_context=candidate.subject.served_context.tokens,
                    served_context_source=candidate.subject.served_context.source,
                    target_gpu_index=candidate.target_gpu_index,
                    rank=candidate.rank,
                    task_fit=candidate.fit.task_fit,
                    final_score=candidate.final_score,
                    estimated_vram_bytes=candidate.estimated_vram_bytes,
                    capability_breakdown_json=[
                        score.as_json() for score in candidate.fit.capabilities
                    ],
                    factors_json=candidate.factors.as_json(),
                    residency_detail_json=(
                        None
                        if candidate.factors.residency_detail is None
                        else dict(candidate.factors.residency_detail)
                    ),
                    rejected=False,
                    created_at=now,
                )
            )
        for item in explanation.rejected:
            session.add(
                RoutingCandidate(
                    decision_id=explanation.decision_id,
                    model_id=item.subject.facts.model_id,
                    adapter_id=(
                        None if item.subject.adapter is None else item.subject.adapter.adapter_id
                    ),
                    subject_canonical_id=item.subject.subject_canonical_id,
                    runtime_profile_id=profile_ids[item.subject.runtime_profile_hash],
                    served_context=item.subject.served_context.tokens,
                    served_context_source=item.subject.served_context.source,
                    rank=None,
                    rejected=True,
                    rejection_reason=item.rejection.reason,
                    rejection_detail_json=item.rejection.detail,
                    estimated_vram_bytes=(
                        None if item.estimate is None else item.estimate.total_bytes
                    ),
                    created_at=now,
                )
            )


def read_decision(database: Database, decision_id: str) -> dict[str, Any] | None:
    """Return one stored decision's explanation, or ``None`` if there is no such decision.

    Args:
        database: The application's database handle.
        decision_id: The decision's ULID.

    Returns:
        Routing §8's document exactly as it was written (acceptance criterion 2).
    """
    with database.read() as session:
        row = session.get(RoutingDecision, decision_id)
        if row is None:
            return None
        return cast("dict[str, Any]", row.explanation_json)


@dataclass(frozen=True, slots=True)
class DecisionSummary:
    """One row of the routing history, as the page and the API list it."""

    decision_id: str
    task_profile_id: str
    task_profile_version: str
    requested_at: datetime
    duration_ms: int
    selected_canonical_id: str | None
    selected_score: float | None
    flags: tuple[str, ...]


def recent_decisions(database: Database, *, limit: int = 50) -> tuple[DecisionSummary, ...]:
    """Return the most recent routing decisions, newest first.

    Args:
        database: The application's database handle.
        limit: How many to return.

    Returns:
        One summary per decision.
    """
    with database.read() as session:
        rows = (
            session.execute(
                select(RoutingDecision)
                .order_by(RoutingDecision.requested_at.desc(), RoutingDecision.id.desc())
                .limit(limit)
            )
            .scalars()
            .all()
        )
        summaries = []
        for row in rows:
            payload = cast("dict[str, Any]", row.explanation_json)
            selected = payload.get("selected")
            summaries.append(
                DecisionSummary(
                    decision_id=row.id,
                    task_profile_id=row.task_profile_id,
                    task_profile_version=row.task_profile_version,
                    requested_at=row.requested_at,
                    duration_ms=row.duration_ms,
                    selected_canonical_id=(
                        None if selected is None else str(selected["canonical_id"])
                    ),
                    selected_score=row.selected_score,
                    flags=tuple(cast("list[str]", row.flags_json or [])),
                )
            )
        return tuple(summaries)
