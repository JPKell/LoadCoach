"""The total order (routing §7): no tie is ever resolved nondeterministically."""

from __future__ import annotations

import random

import pytest
from baseaicore import RuntimeProfile

from loadcoach.domain.routing.ranking import RankedCandidate, rank_candidates
from loadcoach.domain.routing.scoring import AdjustmentFactors, CapabilityScore, TaskFit
from loadcoach.domain.routing.subject import (
    ExecutionSubject,
    ModelFacts,
    ProviderFacts,
    ServedContext,
)


def _fit(task_fit: float, *, confidence: float = 0.5) -> TaskFit:
    return TaskFit(
        task_fit=task_fit,
        capabilities=(
            CapabilityScore(
                capability_id="reasoning",
                weight=1.0,
                score=task_fit,
                confidence=confidence,
                source="manual",
            ),
        ),
        present_weight=1.0,
        measured_weight=1.0,
    )


def _candidate(
    model_id: str,
    canonical_id: str,
    task_fit: float,
    *,
    confidence: float = 0.5,
    resident: bool = False,
    vram: int | None = 1000,
    factors: AdjustmentFactors | None = None,
) -> RankedCandidate:
    return RankedCandidate(
        subject=ExecutionSubject(
            facts=ModelFacts(
                model_id=model_id,
                canonical_id=canonical_id,
                provider_kind="fake",
                provider_model_name=canonical_id,
            ),
            provider=ProviderFacts(),
            runtime_profile=RuntimeProfile(),
            served_context=ServedContext(tokens=8192, source="reported"),
        ),
        fit=_fit(task_fit, confidence=confidence),
        factors=factors or AdjustmentFactors(),
        estimated_vram_bytes=vram,
        resident=resident,
    )


def test_ordered_by_final_score_descending_with_ranks_assigned() -> None:
    ranking = rank_candidates(
        (
            _candidate("01B", "b", 0.4),
            _candidate("01A", "a", 0.9),
            _candidate("01C", "c", 0.6),
        ),
        fallback_depth=2,
    )
    assert [c.subject.facts.canonical_id for c in ranking.ordered] == ["a", "c", "b"]
    assert [c.rank for c in ranking.ordered] == [1, 2, 3]
    assert ranking.primary is not None
    assert ranking.primary.subject.facts.canonical_id == "a"
    assert [c.subject.facts.canonical_id for c in ranking.fallbacks] == ["c", "b"]


def test_ties_break_by_confidence_then_residency_then_vram_then_canonical_id() -> None:
    by_confidence = rank_candidates(
        (_candidate("01A", "a", 0.5, confidence=0.2), _candidate("01B", "b", 0.5, confidence=0.9))
    )
    assert by_confidence.ordered[0].subject.facts.canonical_id == "b"

    by_residency = rank_candidates(
        (
            _candidate("01A", "a", 0.5, confidence=0.5),
            _candidate("01B", "b", 0.5, confidence=0.5, resident=True),
        )
    )
    assert by_residency.ordered[0].subject.facts.canonical_id == "b"

    by_vram = rank_candidates(
        (
            _candidate("01A", "a", 0.5, vram=9_000),
            _candidate("01B", "b", 0.5, vram=1_000),
        )
    )
    assert by_vram.ordered[0].subject.facts.canonical_id == "b"

    by_canonical_id = rank_candidates(
        (_candidate("01B", "zeta", 0.5), _candidate("01A", "alpha", 0.5))
    )
    assert by_canonical_id.ordered[0].subject.facts.canonical_id == "alpha"


def test_an_unknown_vram_estimate_never_wins_a_tie() -> None:
    ranking = rank_candidates(
        (_candidate("01A", "a", 0.5, vram=None), _candidate("01B", "b", 0.5, vram=40_000_000_000))
    )
    assert ranking.ordered[0].subject.facts.canonical_id == "b"


def test_two_rows_sharing_a_canonical_id_still_have_a_total_order() -> None:
    """canonical_id truncates the digest, so it is not unique by construction; the ULID is."""
    ranking = rank_candidates((_candidate("01Z", "same", 0.5), _candidate("01A", "same", 0.5)))
    assert [c.subject.facts.model_id for c in ranking.ordered] == ["01A", "01Z"]


def test_ordering_is_independent_of_input_order() -> None:
    candidates = [
        _candidate(f"01{index:02d}", f"model-{index}", 0.5, confidence=0.5, vram=1000)
        for index in range(12)
    ]
    baseline = [c.subject.facts.model_id for c in rank_candidates(tuple(candidates)).ordered]
    generator = random.Random(20260829)  # noqa: S311 — shuffling test inputs, not cryptography
    for _ in range(20):
        shuffled = candidates[:]
        generator.shuffle(shuffled)
        assert [
            c.subject.facts.model_id for c in rank_candidates(tuple(shuffled)).ordered
        ] == baseline


def test_disallow_fallback_leaves_the_primary_with_no_fallbacks() -> None:
    ranking = rank_candidates(
        (_candidate("01A", "a", 0.9), _candidate("01B", "b", 0.5)),
        fallback_depth=2,
        disallow_fallback=True,
    )
    assert ranking.primary is not None
    assert ranking.fallbacks == ()


def test_empty_in_empty_out() -> None:
    ranking = rank_candidates(())
    assert ranking.primary is None
    assert ranking.ordered == ()


def test_final_score_is_task_fit_times_every_factor() -> None:
    candidate = _candidate(
        "01A",
        "a",
        0.8,
        factors=AdjustmentFactors(reliability=0.98, availability=1.0, residency=1.05, cost=0.9),
    )
    assert candidate.final_score == pytest.approx(0.8 * 0.98 * 1.0 * 1.05 * 0.9)
