"""loadcoach.domain.routing.scoring — capability scoring and the adjustment factors.

Routing §5's arithmetic, with the rule that the whole phase exists to get right::

    task_fit(model) = Σ_c ( weight_c × score_c × confidence_c )
                      ─────────────────────────────────────────
                              Σ_c ( weight_c × present_c )

A capability with nothing behind it is **excluded from the numerator and the denominator**. It is
not scored zero, and it does not shrink the result: absence of evidence is not evidence of
incapacity. Every exclusion is named in the explanation, and counts toward ``low_evidence``.

Pure and total: every input is a value, there is no clock, no database and no provider here, and
the same inputs always produce the same numbers (routing §12).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

from loadcoach.domain.evidence_policy import (
    freeweight_remedy,
    is_user_capability,
    machine_admits,
    user_capability_note,
)
from loadcoach.domain.reliability import PRODUCTION_MINIMUM_SAMPLES, ReliabilityFactor
from loadcoach.domain.routing.subject import signals_by_capability

if TYPE_CHECKING:
    from collections.abc import Mapping

    from loadcoach.domain.routing.subject import CapabilitySignal, ExecutionSubject

__all__ = [
    "DECLARED_PRIOR_CONFIDENCE",
    "DECLARED_PRIOR_SCORE",
    "PARAMETER_BAND_PRIOR_CEILING",
    "PARAMETER_BAND_PRIOR_FLOOR",
    "PRODUCTION_MINIMUM_SAMPLES",
    "AdjustmentFactors",
    "CapabilityScore",
    "ScoringInputs",
    "TaskFit",
    "adjustment_factors",
    "parameter_band_priors",
    "resolve_capability",
    "score_subject",
]

DECLARED_PRIOR_SCORE: Final = 0.5
"""What a provider-declared capability flag contributes (routing §5.1: "contributes 0.5 as a
neutral prior to the matching capability").

The stored ``model_capabilities`` row for a declared flag holds ``score=1.0`` — an honest
statement that the flag *is* present, which is a binary fact about the provider's declaration.
Routing does not carry that 1.0 into the arithmetic: "the provider says it can call tools" is not
a measurement that it does so well. The flag gates the hard constraint; this number ranks."""

DECLARED_PRIOR_CONFIDENCE: Final = 0.3
"""The fixed low confidence every prior carries (routing §5.1), so a single real benchmark result
outweighs any number of them."""

PARAMETER_BAND_PRIOR_FLOOR: Final = 0.40
PARAMETER_BAND_PRIOR_CEILING: Final = 0.60
"""The capped range of the parameter-count band prior (routing §5.1: "small prior toward general
capability, capped"). Twenty percentage points of spread across the whole installed set is
deliberately narrow — the band says "this is one of the larger models here", which is weak
evidence about any particular capability and is scored as such."""

# ``PRODUCTION_MINIMUM_SAMPLES`` lives with the statistics it bounds (``domain.reliability``)
# since Phase 7 and is re-exported here unchanged: the gate on a production *signal* below and
# the gate on the reliability *factor* are one number, on purpose (LCX4).

_MEASURED_SOURCES: Final[frozenset[str]] = frozenset({"benchmark", "production", "manual"})
"""Sources that count as *evidence* for the ``low_evidence`` flag. Declared flags and the
parameter band are priors: they let routing rank without FreeWeight, and they are exactly what
that flag exists to disclose."""

_SOURCE_PRECEDENCE: Final[tuple[str, ...]] = ("benchmark", "production", "manual", "declared")


@dataclass(frozen=True, slots=True)
class CapabilityScore:
    """One capability's contribution to one candidate, present or absent.

    Attributes:
        capability_id: The SetSpec capability.
        weight: The task profile's weight for it.
        score: The resolved ability, or ``None`` when absent.
        confidence: The confidence attached to it, or ``None`` when absent.
        source: ``"benchmark"``, ``"production"``, ``"manual"``, ``"declared"``, ``"prior"``,
            ``"absent"``, ``"evidence_profile_mismatch"``, ``"evidence_foreign_machine"`` or
            ``"evidence_unbound"``.
        note: Human-readable reason. Present whenever ``score`` is ``None``, and also on a
            ``user.*`` capability that *did* score — ADR-0032 §6 requires the goal, its
            ``kappa_w`` and its ``n_holdout`` to be stated in words, not only as numbers in the
            breakdown.
        remedy: For a profile mismatch, the FreeWeight invocation that would produce matching
            evidence (ADR-0023 §3).
        evidence_age_days: Age of the evidence behind the score, from ``measured_at``.
        sample_count: How many observations stand behind it.
        measured_profile_hash: The profile a mismatched measurement was taken under.
        measured_machine_fingerprint: The machine a measurement was taken on, when it was not
            this one.
        stale: Whether the evidence behind the score carries a staleness badge.
        stale_reason: Which of ADR-0017's four reasons raised it.
    """

    capability_id: str
    weight: float
    score: float | None
    confidence: float | None
    source: str
    note: str | None = None
    remedy: str | None = None
    evidence_age_days: int | None = None
    sample_count: int | None = None
    measured_profile_hash: str | None = None
    measured_machine_fingerprint: str | None = None
    stale: bool = False
    stale_reason: str | None = None

    @property
    def present(self) -> bool:
        """Whether this capability contributes to the weighted mean at all."""
        return self.score is not None and self.confidence is not None

    @property
    def measured(self) -> bool:
        """Whether a measurement — not a prior — stands behind this score."""
        return self.present and self.source in _MEASURED_SOURCES

    def as_json(self) -> dict[str, object]:
        """Return the per-capability object routing §8's explanation carries."""
        payload: dict[str, object] = {
            "capability": self.capability_id,
            "weight": self.weight,
            "score": self.score,
            "confidence": self.confidence,
            "source": self.source,
        }
        for key, value in (
            ("note", self.note),
            ("remedy", self.remedy),
            ("evidence_age_days", self.evidence_age_days),
            ("sample_count", self.sample_count),
            ("measured_profile_hash", self.measured_profile_hash),
            ("measured_machine_fingerprint", self.measured_machine_fingerprint),
            ("stale_reason", self.stale_reason),
        ):
            if value is not None:
                payload[key] = value
        if self.stale:
            payload["stale"] = True
        return payload


@dataclass(frozen=True, slots=True)
class TaskFit:
    """One candidate's weighted capability score and the breakdown behind it."""

    task_fit: float
    capabilities: tuple[CapabilityScore, ...]
    present_weight: float
    measured_weight: float

    @property
    def mean_confidence(self) -> float:
        """Mean confidence across present capabilities; 0.0 when none is present.

        A tie-break input (routing §7), not a score: two candidates with the same ``final_score``
        are ordered by how much is actually known about them.
        """
        confidences = [c.confidence for c in self.capabilities if c.confidence is not None]
        return sum(confidences) / len(confidences) if confidences else 0.0


@dataclass(frozen=True, slots=True)
class ScoringInputs:
    """Everything scoring reads beyond the subject itself.

    Attributes:
        weights: The task profile's capability weights.
        min_confidence: Signals below this confidence are discarded as absent.
        parameter_priors: Canonical ID -> the band prior for that model, from
            :func:`parameter_band_priors`. A model whose parameter count was never reported has
            no entry, and therefore no prior — not a zero one.
        require_evidence: When true, priors do not stand in for evidence: a capability with only
            a declared flag, a manual score or a band prior behind it is absent (routing §10).
        machine_fingerprint: This machine's fingerprint, from SweatMeter at startup (spec §10).
            ``None`` when it could not be produced, which admits every measurement rather than
            refusing them all.
    """

    weights: Mapping[str, float]
    min_confidence: float = 0.05
    parameter_priors: Mapping[str, float] | None = None
    require_evidence: bool = False
    machine_fingerprint: str | None = None


def parameter_band_priors(
    parameter_counts: Mapping[str, int | None],
) -> dict[str, float]:
    """Rank the installed models by parameter count and return each one's band prior.

    Routing §5.1's "parameter count band (relative to other installed models)". The prior is the
    model's rank within the installed set, linearly mapped onto
    ``[PARAMETER_BAND_PRIOR_FLOOR, PARAMETER_BAND_PRIOR_CEILING]`` — relative, as the doc says, so
    the same model gets a different prior on a machine that also holds a 70B and on one where it
    is the largest thing present. That is the honest reading: the signal is "large for this
    machine", not "large in absolute terms".

    A model whose parameter count was never reported gets **no entry**, so no prior is invented
    for it. Every model reporting the same count gets the midpoint rather than an arbitrary order.

    Args:
        parameter_counts: Canonical ID -> parameter count, or ``None`` where unreported.

    Returns:
        Canonical ID -> prior, containing only the models that reported a count.
    """
    known = {name: count for name, count in parameter_counts.items() if count is not None}
    if not known:
        return {}
    distinct = sorted({count for count in known.values()})
    midpoint = (PARAMETER_BAND_PRIOR_FLOOR + PARAMETER_BAND_PRIOR_CEILING) / 2
    if len(distinct) == 1:
        return dict.fromkeys(known, midpoint)
    span = PARAMETER_BAND_PRIOR_CEILING - PARAMETER_BAND_PRIOR_FLOOR
    position = {count: index / (len(distinct) - 1) for index, count in enumerate(distinct)}
    return {
        name: PARAMETER_BAND_PRIOR_FLOOR + span * position[count] for name, count in known.items()
    }


def resolve_capability(  # noqa: PLR0913 — every argument is one documented scoring input
    capability_id: str,
    weight: float,
    signals: tuple[CapabilitySignal, ...],
    *,
    runtime_profile_hash: str,
    profile_fields: Mapping[str, object],
    min_confidence: float,
    band_prior: float | None,
    require_evidence: bool,
    machine_fingerprint: str | None = None,
) -> CapabilityScore:
    """Choose the one signal that scores this capability, or record why none does.

    Precedence is benchmark, production, manual, declared, then the parameter band prior. Two
    exclusions come first and are the point of the function:

    * A benchmark measurement whose ``runtime_profile_hash`` differs from the candidate's resolved
      hash does not describe this execution (ADR-0017's hard separation, ADR-0023 §3). It is not
      reused and not scored zero: the capability is **absent**, named
      ``evidence_profile_mismatch`` with both hashes and the FreeWeight invocation that would
      produce matching evidence.
    * A performance, memory or energy measurement taken on **another machine** does not
      describe this one (ADR-0017's last hard separation). It is absent, named
      ``evidence_foreign_machine``, and counted toward ``low_evidence``. A *quality* measurement
      from another machine is used, and its badge is carried into the explanation.
    * Evidence whose ``match_state`` is not ``"bound"`` never contributes (ADR-0022 §4).

    Where an excluded measurement exists, no prior stands in for it. A model that *was* measured,
    under settings that do not apply here, is not in the same position as one nobody has ever
    measured — substituting a guess would bury the remedy, which is the one thing a user can act
    on.

    Args:
        capability_id: The capability being scored.
        weight: Its weight in the task profile.
        signals: Every signal known for this model and this capability.
        runtime_profile_hash: The candidate's resolved profile hash.
        profile_fields: The resolved profile's set fields, for the remedy string. Every one of
            them is part of the subject hash, so every one of them has to be named — the context
            alone is not enough (ADR-0023 §2, and the I4 demonstration).
        min_confidence: Signals below this are discarded.
        band_prior: This model's parameter band prior, or ``None`` if it has none.
        require_evidence: When true, only benchmark and production evidence counts — declared
            flags, manual scores and band priors are all priors for this purpose (routing §10).
        machine_fingerprint: This machine's fingerprint, or ``None`` when SweatMeter could not
            produce one — in which case nothing is excluded for being measured elsewhere,
            because not knowing which machine this is has not established that it is a different
            one.

    Returns:
        The resolved :class:`CapabilityScore`, present or absent with a named reason.
    """
    usable: dict[str, CapabilitySignal] = {}
    excluded: CapabilityScore | None = None

    for signal in signals:
        if signal.confidence < min_confidence:
            continue
        if signal.source == "benchmark":
            if signal.match_state is not None and signal.match_state != "bound":
                excluded = excluded or CapabilityScore(
                    capability_id=capability_id,
                    weight=weight,
                    score=None,
                    confidence=None,
                    source="evidence_unbound",
                    note=(
                        f"evidence match_state is {signal.match_state!r}, not 'bound'; "
                        "it does not describe a model this registry has identified"
                    ),
                )
                continue
            if signal.machine_fingerprint is not None and not machine_admits(
                signal.machine_fingerprint, machine_fingerprint, capability_id
            ):
                excluded = excluded or CapabilityScore(
                    capability_id=capability_id,
                    weight=weight,
                    score=None,
                    confidence=None,
                    source="evidence_foreign_machine",
                    note=(
                        f"this is a performance measurement taken on machine "
                        f"{signal.machine_fingerprint}, not on this one; throughput, memory and "
                        "energy describe the card they were measured on (ADR-0017)"
                    ),
                    remedy=f"{freeweight_remedy(profile_fields)}   # on this machine",
                    measured_machine_fingerprint=signal.machine_fingerprint,
                )
                continue
            if signal.runtime_profile_hash != runtime_profile_hash:
                excluded = excluded or CapabilityScore(
                    capability_id=capability_id,
                    weight=weight,
                    score=None,
                    confidence=None,
                    source="evidence_profile_mismatch",
                    note=(
                        f"evidence measured under runtime profile "
                        f"{signal.runtime_profile_hash}, executing under {runtime_profile_hash}"
                    ),
                    remedy=freeweight_remedy(profile_fields),
                    measured_profile_hash=signal.runtime_profile_hash,
                )
                continue
        if signal.source == "production" and (
            signal.sample_count is None or signal.sample_count < PRODUCTION_MINIMUM_SAMPLES
        ):
            continue
        usable.setdefault(signal.source, signal)

    for source in _SOURCE_PRECEDENCE:
        chosen = usable.get(source)
        if chosen is None:
            continue
        if source in ("declared", "manual") and require_evidence:
            break
        if source == "declared":
            return CapabilityScore(
                capability_id=capability_id,
                weight=weight,
                score=DECLARED_PRIOR_SCORE,
                confidence=DECLARED_PRIOR_CONFIDENCE,
                source="declared",
                note="provider-declared flag, scored as a neutral prior (routing §5.1)",
            )
        foreign = (
            chosen.machine_fingerprint is not None
            and machine_fingerprint is not None
            and chosen.machine_fingerprint != machine_fingerprint
        )
        note: str | None = None
        if is_user_capability(capability_id):
            note = user_capability_note(capability_id, chosen.calibration)
        elif foreign:
            note = (
                f"quality measured on machine {chosen.machine_fingerprint}, not on this one; "
                "retained with a machine badge (ADR-0017)"
            )
        return CapabilityScore(
            capability_id=capability_id,
            weight=weight,
            score=chosen.score,
            confidence=chosen.confidence,
            source=source,
            note=note,
            evidence_age_days=chosen.age_days,
            sample_count=chosen.sample_count,
            measured_machine_fingerprint=chosen.machine_fingerprint if foreign else None,
            stale=chosen.stale,
            stale_reason=chosen.stale_reason,
        )

    if excluded is not None:
        return excluded
    if band_prior is not None and not require_evidence:
        return CapabilityScore(
            capability_id=capability_id,
            weight=weight,
            score=band_prior,
            confidence=DECLARED_PRIOR_CONFIDENCE,
            source="prior",
            note="parameter-count band relative to the installed set (routing §5.1)",
        )
    return CapabilityScore(
        capability_id=capability_id,
        weight=weight,
        score=None,
        confidence=None,
        source="absent",
        note="no evidence; excluded from the weighted mean",
    )


def score_subject(subject: ExecutionSubject, inputs: ScoringInputs) -> TaskFit:
    """Compute one candidate's ``task_fit`` and its full per-capability breakdown.

    Args:
        subject: The candidate.
        inputs: The task profile's weights and the scoring policy.

    Returns:
        The :class:`TaskFit`. ``task_fit`` is ``0.0`` when **no** capability is present — the one
        case where zero is the honest answer, because there is nothing at all to weigh, and the
        candidate's own breakdown says so capability by capability.
    """
    grouped = signals_by_capability(subject.signals)
    profile = subject.runtime_profile
    profile_fields = {
        name: getattr(profile, name)
        for name in (
            "context_size",
            "kv_cache_precision",
            "gpu_layers",
            "flash_attention",
            "threads",
            "batch_size",
            "keep_alive",
        )
        if getattr(profile, name) is not None
    }
    priors = inputs.parameter_priors or {}
    band_prior = priors.get(subject.facts.canonical_id)

    scores = tuple(
        resolve_capability(
            capability_id,
            weight,
            grouped.get(capability_id, ()),
            runtime_profile_hash=subject.runtime_profile_hash,
            profile_fields=profile_fields,
            min_confidence=inputs.min_confidence,
            band_prior=band_prior,
            require_evidence=inputs.require_evidence,
            machine_fingerprint=inputs.machine_fingerprint,
        )
        for capability_id, weight in sorted(inputs.weights.items())
    )

    numerator = sum(
        s.weight * s.score * s.confidence
        for s in scores
        if s.score is not None and s.confidence is not None
    )
    present_weight = sum(s.weight for s in scores if s.present)
    measured_weight = sum(s.weight for s in scores if s.measured)
    task_fit = numerator / present_weight if present_weight > 0 else 0.0
    return TaskFit(
        task_fit=task_fit,
        capabilities=scores,
        present_weight=present_weight,
        measured_weight=measured_weight,
    )


@dataclass(frozen=True, slots=True)
class AdjustmentFactors:
    """The four multipliers applied to ``task_fit`` (routing §6), and the reliability inputs.

    ``reliability_detail`` is the :class:`~loadcoach.domain.reliability.ReliabilityFactor`
    record behind ``reliability``: which window, how many attempts, the rates, the acceptance,
    and one line saying why — present whether the factor is live or neutral, because routing §6
    says each factor's value *and inputs* are recorded, and a neutral factor's input is the
    sample count that kept it neutral.
    """

    reliability: float = 1.0
    availability: float = 1.0
    residency: float = 1.0
    cost: float = 1.0
    reliability_detail: Mapping[str, Any] | None = None

    @property
    def product(self) -> float:
        """The combined multiplier."""
        return self.reliability * self.availability * self.residency * self.cost

    def as_json(self) -> dict[str, Any]:
        """Return the ``factors`` object routing §8's explanation carries."""
        document: dict[str, Any] = {
            "reliability": self.reliability,
            "availability": self.availability,
            "residency": self.residency,
            "cost": self.cost,
        }
        if self.reliability_detail is not None:
            document["reliability_detail"] = dict(self.reliability_detail)
        return document


def adjustment_factors(
    subject: ExecutionSubject,
    *,
    resident_models: frozenset[str] = frozenset(),
    prefer_resident_bonus: float = 0.05,
    remote_cost_factor: float = 0.9,
    reliability: float | ReliabilityFactor = 1.0,
    availability: float = 1.0,
) -> AdjustmentFactors:
    """Build one candidate's adjustment factors (routing §6).

    ``reliability`` is a :class:`~loadcoach.domain.reliability.ReliabilityFactor` when production
    statistics exist for the candidate and this task profile (P7), and the documented neutral
    ``1.0`` otherwise; ``availability`` stays a plain parameter until queue load feeds it. Either
    way the arithmetic is the same, which is why P3 shaped it this way.

    Args:
        subject: The candidate.
        resident_models: Canonical IDs currently loaded. Empty until residency tracking exists.
        prefer_resident_bonus: The residency bonus, deliberately small — it breaks ties, it does
            not override capability.
        remote_cost_factor: The multiplier for a remote provider. 1.0 for local, always.
        reliability: From production evidence; neutral until the minimum sample count.
        availability: From queue load; neutral until there is a queue.

    Returns:
        The four factors, each within routing §6's documented range.
    """
    resident = subject.facts.canonical_id in resident_models
    if isinstance(reliability, ReliabilityFactor):
        value, detail = reliability.value, reliability.as_json()
    else:
        value, detail = reliability, None
    return AdjustmentFactors(
        reliability=value,
        availability=availability,
        residency=1.0 + prefer_resident_bonus if resident else 1.0,
        cost=remote_cost_factor if subject.facts.is_remote else 1.0,
        reliability_detail=detail,
    )
