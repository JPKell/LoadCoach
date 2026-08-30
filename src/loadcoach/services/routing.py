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

from loadcoach.domain.registry import geometry_from_json
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
from loadcoach.services.task_profiles import StoredTaskProfile, list_stored_task_profiles

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from sqlalchemy.orm import Session
    from sweatmeter import TelemetrySnapshot

    from loadcoach.config import RoutingSettings, RuntimeSettings, TelemetrySettings
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
        overrides: Routing §10's overrides.
    """

    task: str
    estimated_input_tokens: int | None = None
    max_output_tokens: int | None = None
    constraints: TaskProfileConstraints | None = None
    overrides: RuntimeOverrides = field(default_factory=RuntimeOverrides)


@dataclass(frozen=True, slots=True)
class RoutingPolicy:
    """The configured policy routing applies, lifted out of :class:`~loadcoach.config.Settings`.

    Passed as one value so the pipeline can be driven from a test without building a whole
    settings object, and so every knob a decision depended on is visible in one place.
    """

    strategy: str = "weighted_evidence"
    min_confidence: float = 0.05
    prefer_resident_bonus: float = 0.05
    min_present_weight: float = 0.5
    remote_cost_factor: float = 0.9
    vram_headroom_bytes: int = 512 * 1024 * 1024
    runtime_defaults: RuntimeProfile = field(default_factory=RuntimeProfile)
    runtime_per_model: Mapping[str, RuntimeProfile] = field(default_factory=dict)

    @classmethod
    def from_settings(
        cls,
        *,
        routing: RoutingSettings,
        runtime: RuntimeSettings,
        telemetry: TelemetrySettings,
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
    geometry = model.descriptor_json
    return ModelFacts(
        model_id=model.id,
        canonical_id=model.canonical_id,
        provider_kind=model.provider_kind,
        provider_model_name=model.provider_model_name,
        artifact_digest=model.artifact_digest,
        available=model.available,
        unavailable_reason=model.unavailable_reason,
        max_context=model.max_context,
        size_bytes=model.size_bytes,
        parameter_count=model.parameter_count,
        layers=geometry_from_json(geometry, "layers"),
        kv_heads=geometry_from_json(geometry, "kv_heads"),
        head_dim=geometry_from_json(geometry, "head_dim"),
        is_remote=is_remote,
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
    database: Database, *, is_remote: bool
) -> tuple[tuple[ModelFacts, tuple[CapabilitySignal, ...]], ...]:
    """Read every model the registry knows, with its non-benchmark capability signals.

    Benchmark evidence is not read here: the ``capability_evidence`` table arrives with P6's
    import, and this phase's whole premise is routing with no FreeWeight in the picture. The
    signal type it will produce is already the one scoring consumes, so P6 adds a query, not a
    reshaping.
    """
    with database.read() as session:
        models = session.execute(select(Model).order_by(Model.canonical_id)).scalars().all()
        capability_rows = session.execute(select(ModelCapability)).scalars().all()
    by_model: dict[str, list[ModelCapability]] = {}
    for row in capability_rows:
        by_model.setdefault(row.model_id, []).append(row)
    return tuple(
        (_facts_for(model, is_remote=is_remote), _signals_for(by_model.get(model.id, [])))
        for model in models
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
        and overrides.runtime_profile is None
        and not overrides.disallow_fallback
        and not overrides.require_evidence
    ):
        return None
    return {
        "model": overrides.model,
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
    policy: RoutingPolicy,
    snapshot: TelemetrySnapshot | None = None,
    resident_models: frozenset[str] = frozenset(),
    open_circuit_breakers: frozenset[str] = frozenset(),
    resident_devices: Mapping[str, frozenset[int]] | None = None,
    now: datetime,
    persist: bool = True,
) -> RoutingResult:
    """Run the whole routing pipeline and persist the decision.

    Args:
        database: The application's database handle.
        request: What to route.
        provider: The provider's own capabilities, which gate the context source and the
            capability constraints.
        policy: The configured routing policy.
        snapshot: The telemetry the resource constraints read. ``None`` skips them, which is what
            a machine with no telemetry reader honestly supports — not a fabricated zero.
        resident_models: Canonical IDs currently loaded, for the residency tie-break.
        open_circuit_breakers: Canonical IDs the breaker currently excludes.
        resident_devices: Canonical ID -> devices the model is resident on; a resident model
            fits on its device whatever the estimate says (queue §5, admission).
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
    started = datetime.now(tz=now.tzinfo)
    profile = load_task_profile(database, request.task)
    constraints = _merged_constraints(profile, request.constraints)
    execution = profile.execution
    max_output_tokens = request.max_output_tokens or int(
        cast("int", execution.get("max_output_tokens", 1024))
    )
    min_output_tokens = cast("int | None", execution.get("min_output_tokens"))
    fallback_depth = int(cast("int", execution.get("fallback_depth", 0)))

    candidates = _read_candidates(database, is_remote=provider.is_remote)
    priors = parameter_band_priors(
        {facts.canonical_id: facts.parameter_count for facts, _ in candidates}
    )
    scoring = ScoringInputs(
        weights=profile.weights,
        min_confidence=policy.min_confidence,
        parameter_priors=priors,
        require_evidence=request.overrides.require_evidence,
    )

    ranked: list[RankedCandidate] = []
    rejected: list[RejectedCandidate] = []
    selected_budget: ContextBudget | None = None

    for facts, signals in candidates:
        subject, missing_context = _build_subject(
            facts,
            signals,
            provider=provider,
            policy=policy,
            request=request,
            constraints=constraints,
        )
        if subject is None:
            rejected.append(
                RejectedCandidate(subject=missing_context, rejection=_no_context_rejection(facts))
            )
            continue

        if request.overrides.model is not None and facts.canonical_id != request.overrides.model:
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
                resolved_scores={score.capability_id: score.score for score in fit.capabilities},
                snapshot=snapshot,
                vram_headroom_bytes=policy.vram_headroom_bytes,
                open_circuit_breakers=open_circuit_breakers,
                resident_devices=resident_devices or {},
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
                    remote_cost_factor=policy.remote_cost_factor,
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


def _build_subject(
    facts: ModelFacts,
    signals: tuple[CapabilitySignal, ...],
    *,
    provider: ProviderFacts,
    policy: RoutingPolicy,
    request: RouteRequest,
    constraints: TaskProfileConstraints,
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
        )
        return None, placeholder
    subject = ExecutionSubject(
        facts=facts,
        provider=provider,
        runtime_profile=profile,
        served_context=served,
        signals=signals,
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
                    rejected=False,
                    created_at=now,
                )
            )
        for item in explanation.rejected:
            session.add(
                RoutingCandidate(
                    decision_id=explanation.decision_id,
                    model_id=item.subject.facts.model_id,
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
