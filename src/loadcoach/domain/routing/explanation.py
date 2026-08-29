"""loadcoach.domain.routing.explanation — assembling routing §8's persisted explanation.

Every decision is explained, and the explanation is persisted for every job — not sampled. This
module builds it as a plain JSON-shaped mapping from the pipeline's own outputs, so the same
structure serves the API response, the stored row and the rendered page without a second,
drifting representation of the same numbers.

Pure: it takes values and returns a mapping. Nothing here reads a clock or a database — the
``requested_at`` and ``duration_ms`` figures are measured by the service layer and passed in.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

from baseaicore.timeutil import to_rfc3339

if TYPE_CHECKING:
    from datetime import datetime

    from loadcoach.domain.routing.constraints import Rejection, VramEstimate
    from loadcoach.domain.routing.context_budget import ContextBudget
    from loadcoach.domain.routing.ranking import RankedCandidate, Ranking
    from loadcoach.domain.routing.subject import ExecutionSubject

__all__ = [
    "CONFIDENCE_POLICY_VERSION",
    "STRATEGY_VERSION",
    "Explanation",
    "RejectedCandidate",
    "build_explanation",
    "evidence_summary_of",
]

STRATEGY_VERSION: Final = "1.0.0"
"""The version of the scoring strategy this build implements. Recorded on every decision so a
decision made under one revision of the arithmetic is never silently compared with another."""

CONFIDENCE_POLICY_VERSION: Final = "1.0.0"
"""The version of the confidence policy applied. LoadCoach applies FreeWeight's confidence; this
records which interpretation of it was in force."""


@dataclass(frozen=True, slots=True)
class RejectedCandidate:
    """One candidate that did not survive the hard constraints, with its numbers."""

    subject: ExecutionSubject
    rejection: Rejection
    estimate: VramEstimate | None = None


@dataclass(frozen=True, slots=True)
class Explanation:
    """A complete routing decision, ready to persist, return and render.

    Attributes:
        decision_id: The decision's ULID.
        payload: The routing §8 mapping, JSON-serializable throughout.
        ranking: The ordered eligible candidates.
        rejected: Every candidate that was filtered out, with its reason.
        flags: ``low_evidence``, ``assumed_context``, and anything else the decision raised.
    """

    decision_id: str
    payload: dict[str, Any]
    ranking: Ranking
    rejected: tuple[RejectedCandidate, ...]
    flags: tuple[str, ...]


def _selected_payload(candidate: RankedCandidate) -> dict[str, Any]:
    """Build the ``selected`` object, which names the resolved subject in full (AC 1a)."""
    subject = candidate.subject
    return {
        "canonical_id": subject.facts.canonical_id,
        "model_id": subject.facts.model_id,
        "runtime_profile_hash": subject.runtime_profile_hash,
        "final_score": candidate.final_score,
        "rank": candidate.rank,
        "served_context": subject.served_context.tokens,
        "served_context_source": subject.served_context.source,
        "target_gpu_index": candidate.target_gpu_index,
    }


def _candidate_payload(candidate: RankedCandidate) -> dict[str, Any]:
    subject = candidate.subject
    return {
        "canonical_id": subject.facts.canonical_id,
        "model_id": subject.facts.model_id,
        "runtime_profile_hash": subject.runtime_profile_hash,
        "served_context": subject.served_context.tokens,
        "served_context_source": subject.served_context.source,
        "rank": candidate.rank,
        "task_fit": candidate.fit.task_fit,
        "final_score": candidate.final_score,
        "present_weight": candidate.fit.present_weight,
        "measured_weight": candidate.fit.measured_weight,
        "estimated_vram_bytes": candidate.estimated_vram_bytes,
        "target_gpu_index": candidate.target_gpu_index,
        "capabilities": [score.as_json() for score in candidate.fit.capabilities],
        "factors": candidate.factors.as_json(),
    }


def _rejected_payload(rejected: RejectedCandidate) -> dict[str, Any]:
    subject = rejected.subject
    return {
        "canonical_id": subject.facts.canonical_id,
        "model_id": subject.facts.model_id,
        "runtime_profile_hash": subject.runtime_profile_hash,
        "served_context": subject.served_context.tokens,
        "served_context_source": subject.served_context.source,
        "reason": rejected.rejection.reason,
        "detail": rejected.rejection.detail,
    }


def evidence_summary_of(
    ranking: Ranking, rejected: tuple[RejectedCandidate, ...]
) -> dict[str, Any]:
    """Summarize what evidence stood behind this decision.

    With no benchmark evidence imported — P3's whole premise, "no FreeWeight in the picture at
    all" — this reports ``source: "none"``, which is what acceptance criterion 1 asks the
    explanation to say. It is derived from the candidates themselves rather than from an import
    record, so it stays true the moment evidence does arrive.

    Args:
        ranking: The ordered eligible candidates.
        rejected: The filtered-out candidates.

    Returns:
        The ``evidence_summary`` object.
    """
    sources: set[str] = set()
    mismatched = 0
    unbound = 0
    for candidate in ranking.ordered:
        for score in candidate.fit.capabilities:
            if score.source == "evidence_profile_mismatch":
                mismatched += 1
            elif score.source == "evidence_unbound":
                unbound += 1
            elif score.present:
                sources.add(score.source)
    has_benchmark = "benchmark" in sources
    return {
        "source": "freeweight" if has_benchmark else "none",
        "contributing_sources": sorted(sources),
        "profile_mismatched_capabilities": mismatched,
        "unbound_capabilities": unbound,
        "candidates_considered": len(ranking.ordered) + len(rejected),
    }


def build_explanation(
    *,
    decision_id: str,
    task_profile_id: str,
    task_profile_version: str,
    strategy_name: str,
    requested_at: datetime,
    duration_ms: int,
    ranking: Ranking,
    rejected: tuple[RejectedCandidate, ...],
    budget: ContextBudget | None,
    telemetry_snapshot: dict[str, Any] | None,
    overrides: dict[str, Any] | None,
    min_present_weight: float,
) -> Explanation:
    """Assemble routing §8's explanation from the pipeline's outputs.

    Two flags are raised here, both about honesty rather than failure:

    * ``low_evidence`` when the selected candidate's **measured** weight — benchmark, production
      or manual, never a declared flag or a parameter band — falls below the configured floor.
      With no FreeWeight in the picture this is always raised, which is precisely the disclosure
      routing §5.1 promises: routing without measurement is reasonable, and clearly labelled.
    * ``assumed_context`` when the selected candidate's served context could only be assumed
      (ADR-0023 §4).

    Args:
        decision_id: The decision's ULID.
        task_profile_id: Which profile was routed for.
        task_profile_version: Its version, recorded on every decision.
        strategy_name: The configured strategy, e.g. ``"weighted_evidence"``.
        requested_at: When the request arrived.
        duration_ms: How long routing took.
        ranking: The ordered eligible candidates.
        rejected: Every filtered-out candidate with its reason.
        budget: The selected candidate's context budget, when one was computed.
        telemetry_snapshot: The machine state routing read, stored so the decision is reproducible
            from its inputs.
        overrides: The request's overrides, or ``None``.
        min_present_weight: The floor below which ``low_evidence`` is raised.

    Returns:
        The :class:`Explanation`.
    """
    flags: list[str] = []
    primary = ranking.primary
    if primary is not None:
        if primary.fit.measured_weight < min_present_weight:
            flags.append("low_evidence")
        if primary.subject.served_context.source == "assumed":
            flags.append("assumed_context")
    if budget is not None and budget.reduced:
        flags.append("output_tokens_reduced")

    payload: dict[str, Any] = {
        "decision_id": decision_id,
        "task_profile": {"id": task_profile_id, "version": task_profile_version},
        "strategy": {"name": strategy_name, "version": STRATEGY_VERSION},
        "confidence_policy_version": CONFIDENCE_POLICY_VERSION,
        "requested_at": to_rfc3339(requested_at),
        "duration_ms": duration_ms,
        "selected": None if primary is None else _selected_payload(primary),
        "fallbacks": [_selected_payload(candidate) for candidate in ranking.fallbacks],
        "candidates": [_candidate_payload(candidate) for candidate in ranking.ordered],
        "rejected": [_rejected_payload(item) for item in rejected],
        "flags": flags,
        "context_budget": None if budget is None else budget.as_json(),
        "evidence_summary": evidence_summary_of(ranking, rejected),
        "telemetry_snapshot": telemetry_snapshot,
        "overrides": overrides,
    }
    return Explanation(
        decision_id=decision_id,
        payload=payload,
        ranking=ranking,
        rejected=rejected,
        flags=tuple(flags),
    )
