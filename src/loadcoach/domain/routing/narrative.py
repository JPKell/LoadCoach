"""loadcoach.domain.routing.narrative — the explanation, made readable (dev-plan P8).

Routing §8's document has every number; a page that dumps it is not an explanation. This module
turns the stored payload into what a person asks first — *why this model?* — as a structure a
template renders in order: the headline arithmetic, what separated the winner from the runner-up,
which capabilities carried the decision and where their evidence came from, what was absent and
what would fix it, what each flag means, and why every other candidate was set aside.

Pure: takes the persisted mapping, returns dataclasses. The numbers are the payload's own,
re-derived from it and never recomputed from other inputs, so the narrative cannot disagree with
the JSON viewer below it.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "Absence",
    "Driver",
    "Narrative",
    "RejectionNote",
    "narrate",
]

_FLAG_MEANINGS: dict[str, str] = {
    "low_evidence": (
        "Less than the configured share of the task profile's weight was measured — the decision "
        "leans on declared flags and priors. Importing FreeWeight evidence for these models "
        "would replace guesses with measurements."
    ),
    "assumed_context": (
        "The selected model's served context could not be established and was taken from its "
        "advertised maximum; a context-limit failure at execution is possible."
    ),
    "output_tokens_reduced": (
        "The requested output length did not fit beside the estimated input, so the output "
        "budget was reduced to what the served context allows."
    ),
    "breaker_state_unavailable": (
        "This decision was made without the serving process's circuit-breaker state — a "
        "one-shot process has none — so it may name a model the running queue is currently "
        "excluding as recently failing."
    ),
}

_REJECTION_MEANINGS: dict[str, str] = {
    "model_unavailable": "the provider currently reports the model as unavailable",
    "insufficient_vram": "its estimated footprint does not fit in the free VRAM of any device",
    "insufficient_ram": "its estimated footprint does not fit in free host RAM",
    "context_limit_exceeded": "the request does not fit in the context it can serve",
    "context_too_small": "its served context is below the task profile's minimum",
    "context_not_configurable": "no served context could be resolved for it",
    "capability_unsupported": "it lacks a capability the task profile requires",
    "below_minimum_score": "a capability scores below the task profile's floor",
    "excluded_by_policy": "the task profile or the request excludes it by name",
    "recently_failing": "its circuit breaker is open after recent failures",
}


@dataclass(frozen=True, slots=True)
class Driver:
    """One capability's contribution to the selected candidate's task fit.

    Attributes:
        capability: The capability.
        weight: Its weight in the profile.
        score: The resolved score.
        confidence: The confidence behind it.
        contribution: ``weight × score × confidence`` — the numerator term.
        source: Where the score came from.
        evidence_age_days: Age of the measurement, when it was one.
        sample_count: Observations behind it, when known.
        stale: Whether the evidence carries a staleness badge.
        runner_up_score: The runner-up's score for the same capability, or ``None``.
        advantage: This candidate's contribution minus the runner-up's, when both scored it.
    """

    capability: str
    weight: float
    score: float
    confidence: float
    contribution: float
    source: str
    evidence_age_days: int | None = None
    sample_count: int | None = None
    stale: bool = False
    runner_up_score: float | None = None
    advantage: float | None = None


@dataclass(frozen=True, slots=True)
class Absence:
    """A capability the selected candidate could not be scored on, and what would fix it."""

    capability: str
    weight: float
    source: str
    note: str
    remedy: str | None = None


@dataclass(frozen=True, slots=True)
class RejectionNote:
    """One rejected candidate, its reason in words, and the numbers behind it."""

    canonical_id: str
    reason: str
    meaning: str
    numbers: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Narrative:
    """The readable explanation.

    Attributes:
        selected: The selected candidate's canonical ID, or ``None`` when nothing was eligible.
        headline: One sentence: what was chosen and the score arithmetic.
        arithmetic: ``task_fit × factors`` spelled out, or ``None``.
        runner_up: The rank-2 candidate's canonical ID, or ``None``.
        margin: Selected final score minus the runner-up's, or ``None``.
        decisive: One sentence naming what separated them, or ``None``.
        drivers: The selected candidate's scored capabilities, largest contribution first.
        absences: Its unscored capabilities, heaviest first, each with its reason and remedy.
        flags: ``(flag, meaning)`` pairs.
        rejections: Every rejected candidate with its reason in words.
        reliability: The selected candidate's reliability line, or ``None``.
        evidence: The evidence summary's own sentence, or ``None``.
        remedies: Every distinct remedy the explanation offers, in order.
    """

    selected: str | None
    headline: str
    arithmetic: str | None
    runner_up: str | None
    margin: float | None
    decisive: str | None
    drivers: tuple[Driver, ...]
    absences: tuple[Absence, ...]
    flags: tuple[tuple[str, str], ...]
    rejections: tuple[RejectionNote, ...]
    reliability: str | None
    evidence: str | None
    remedies: tuple[str, ...]


def _candidates(payload: Mapping[str, Any]) -> Sequence[Mapping[str, Any]]:
    candidates = payload.get("candidates") or ()
    return sorted(candidates, key=lambda c: c.get("rank") or 0)


def _scores_by_capability(candidate: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    return {
        str(entry["capability"]): entry
        for entry in candidate.get("capabilities") or ()
        if isinstance(entry, Mapping)
    }


def _contribution(entry: Mapping[str, Any]) -> float | None:
    score, confidence = entry.get("score"), entry.get("confidence")
    if score is None or confidence is None:
        return None
    return float(entry.get("weight") or 0.0) * float(score) * float(confidence)


def _drivers(
    selected: Mapping[str, Any], runner_up: Mapping[str, Any] | None
) -> tuple[Driver, ...]:
    other = {} if runner_up is None else _scores_by_capability(runner_up)
    drivers: list[Driver] = []
    for entry in selected.get("capabilities") or ():
        contribution = _contribution(entry)
        if contribution is None:
            continue
        rival = other.get(str(entry["capability"]))
        rival_contribution = None if rival is None else _contribution(rival)
        drivers.append(
            Driver(
                capability=str(entry["capability"]),
                weight=float(entry.get("weight") or 0.0),
                score=float(entry["score"]),
                confidence=float(entry["confidence"]),
                contribution=contribution,
                source=str(entry.get("source")),
                evidence_age_days=entry.get("evidence_age_days"),
                sample_count=entry.get("sample_count"),
                stale=bool(entry.get("stale", False)),
                runner_up_score=None if rival is None else rival.get("score"),
                advantage=(
                    None if rival_contribution is None else contribution - rival_contribution
                ),
            )
        )
    return tuple(sorted(drivers, key=lambda d: -d.contribution))


def _absences(selected: Mapping[str, Any]) -> tuple[Absence, ...]:
    absences = [
        Absence(
            capability=str(entry["capability"]),
            weight=float(entry.get("weight") or 0.0),
            source=str(entry.get("source")),
            note=str(entry.get("note") or "no evidence; excluded from the weighted mean"),
            remedy=entry.get("remedy"),
        )
        for entry in selected.get("capabilities") or ()
        if entry.get("score") is None
    ]
    return tuple(sorted(absences, key=lambda a: -a.weight))


def _factors_text(candidate: Mapping[str, Any]) -> str:
    factors = candidate.get("factors") or {}
    return (
        f"task fit {float(candidate.get('task_fit') or 0.0):.3f}"
        f" × reliability {float(factors.get('reliability', 1.0)):.3f}"
        f" × availability {float(factors.get('availability', 1.0)):.3f}"
        f" × residency {float(factors.get('residency', 1.0)):.3f}"
        f" × cost {float(factors.get('cost', 1.0)):.3f}"
        f" = {float(candidate.get('final_score') or 0.0):.3f}"
    )


def _decisive(
    selected: Mapping[str, Any], runner_up: Mapping[str, Any], drivers: Sequence[Driver]
) -> str:
    """Name what separated the two: a factor, or the capability with the largest advantage."""
    own = selected.get("factors") or {}
    other = runner_up.get("factors") or {}
    factor_gaps = {
        name: float(own.get(name, 1.0)) - float(other.get(name, 1.0))
        for name in ("reliability", "availability", "residency", "cost")
    }
    fit_gap = float(selected.get("task_fit") or 0.0) - float(runner_up.get("task_fit") or 0.0)
    widest_factor = max(factor_gaps, key=lambda name: abs(factor_gaps[name]))
    if abs(factor_gaps[widest_factor]) > abs(fit_gap) and factor_gaps[widest_factor] != 0.0:
        direction = "higher" if factor_gaps[widest_factor] > 0 else "lower"
        return (
            f"The {widest_factor} factor decided it: {own.get(widest_factor, 1.0):.3f} against "
            f"{other.get(widest_factor, 1.0):.3f} for {runner_up.get('canonical_id')} — task fit "
            f"was {'higher' if fit_gap > 0 else 'lower' if fit_gap < 0 else 'equal'} by "
            f"{abs(fit_gap):.3f}, the factor {direction} by {abs(factor_gaps[widest_factor]):.3f}."
        )
    scored = [d for d in drivers if d.advantage is not None]
    if scored:
        best = max(scored, key=lambda d: abs(d.advantage or 0.0))
        if best.advantage:
            verb = "ahead" if best.advantage > 0 else "behind"
            return (
                f"Task fit decided it ({float(selected.get('task_fit') or 0):.3f} against "
                f"{float(runner_up.get('task_fit') or 0):.3f}); the capability that moved it most "
                f"was {best.capability}, where the winner scored {best.score:.3f} "
                f"({best.source}) against {best.runner_up_score:.3f} — {abs(best.advantage):.4f} "
                f"of weighted contribution {verb}."
            )
    if fit_gap == 0.0 and all(gap == 0.0 for gap in factor_gaps.values()):
        return (
            "The two scored identically on every term; the tie was broken by the documented "
            "order (confidence, residency, estimated VRAM, then canonical ID)."
        )
    return (
        f"Task fit decided it: {float(selected.get('task_fit') or 0):.3f} against "
        f"{float(runner_up.get('task_fit') or 0):.3f}."
    )


def narrate(payload: Mapping[str, Any]) -> Narrative:
    """Build the readable explanation from routing §8's persisted document.

    Args:
        payload: The stored explanation, exactly as ``GET /jobs/{id}/explanation`` returns it.

    Returns:
        The :class:`Narrative`. With no eligible candidate the headline says so and the
        rejections carry the whole story.
    """
    candidates = _candidates(payload)
    selected_payload = payload.get("selected")
    flags = tuple(
        (str(flag), _FLAG_MEANINGS.get(str(flag), "no documented meaning for this flag"))
        for flag in payload.get("flags") or ()
    )
    rejections = tuple(
        RejectionNote(
            canonical_id=str(item.get("canonical_id")),
            reason=str(item.get("reason")),
            meaning=_REJECTION_MEANINGS.get(
                str(item.get("reason")), str(item.get("reason")).replace("_", " ")
            ),
            numbers=dict(item.get("detail") or {}),
        )
        for item in payload.get("rejected") or ()
    )
    evidence_summary = payload.get("evidence_summary") or {}
    evidence = evidence_summary.get("note")

    if not candidates or not isinstance(selected_payload, Mapping):
        return Narrative(
            selected=None,
            headline=(
                "No candidate satisfied the task profile's constraints. Every model was set "
                "aside for the reason listed beside it."
            ),
            arithmetic=None,
            runner_up=None,
            margin=None,
            decisive=None,
            drivers=(),
            absences=(),
            flags=flags,
            rejections=rejections,
            reliability=None,
            evidence=None if evidence is None else str(evidence),
            remedies=tuple(
                dict.fromkeys(
                    str(r.numbers["remedy"]) for r in rejections if r.numbers.get("remedy")
                )
            ),
        )

    selected = candidates[0]
    runner_up = candidates[1] if len(candidates) > 1 else None
    drivers = _drivers(selected, runner_up)
    absences = _absences(selected)
    margin = (
        None
        if runner_up is None
        else float(selected.get("final_score") or 0.0) - float(runner_up.get("final_score") or 0.0)
    )
    task = payload.get("task_profile") or {}
    headline = (
        f"Selected {selected.get('canonical_id')} for {task.get('id')} with final score "
        f"{float(selected.get('final_score') or 0.0):.3f}"
        + (
            f", {margin:.3f} ahead of {runner_up.get('canonical_id')}."
            if runner_up is not None and margin is not None
            else ", the only eligible candidate."
        )
    )
    reliability_detail = (selected.get("factors") or {}).get("reliability_detail") or {}
    remedies = tuple(dict.fromkeys(a.remedy for a in absences if a.remedy))
    return Narrative(
        selected=str(selected.get("canonical_id")),
        headline=headline,
        arithmetic=_factors_text(selected),
        runner_up=None if runner_up is None else str(runner_up.get("canonical_id")),
        margin=margin,
        decisive=None if runner_up is None else _decisive(selected, runner_up, drivers),
        drivers=drivers,
        absences=absences,
        flags=flags,
        rejections=rejections,
        reliability=reliability_detail.get("reason"),
        evidence=None if evidence is None else str(evidence),
        remedies=remedies,
    )
