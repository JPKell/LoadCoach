"""loadcoach.domain.routing.ranking — the total order over candidates, and fallback selection.

Routing §7: ordered by ``final_score`` descending, ties broken by higher confidence, then
resident, then lower estimated VRAM, then canonical ID. This module adds one more tie-break the
doc does not name — the model's local ULID — because two rows *can* share a canonical ID (the ID
truncates the digest to twelve hex characters, and it is documented as lossy), and an order that
is total for every input is what "no tie is resolved nondeterministically" actually requires. The
ULID is unique by construction, so the comparison never reaches a genuine tie.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from loadcoach.domain.routing.scoring import AdjustmentFactors, TaskFit
    from loadcoach.domain.routing.subject import ExecutionSubject

__all__ = ["RankedCandidate", "Ranking", "rank_candidates"]


@dataclass(frozen=True, slots=True)
class RankedCandidate:
    """One surviving candidate with everything the ordering and the explanation need.

    Attributes:
        subject: The execution subject.
        fit: Its weighted capability score and breakdown.
        factors: The four adjustment multipliers.
        estimated_vram_bytes: The VRAM estimate, or ``None`` when it could not be produced.
        target_gpu_index: The device that satisfied admission, or ``None`` on a CPU-only machine.
        resident: Whether the model is currently loaded.
        rank: 1-based position, assigned by :func:`rank_candidates`.
    """

    subject: ExecutionSubject
    fit: TaskFit
    factors: AdjustmentFactors
    estimated_vram_bytes: int | None = None
    target_gpu_index: int | None = None
    resident: bool = False
    rank: int = 0

    @property
    def final_score(self) -> float:
        """``task_fit × reliability × availability × residency × cost`` (routing §6)."""
        return self.fit.task_fit * self.factors.product

    @property
    def order_key(self) -> tuple[float, float, int, float, str, str]:
        """The total order's sort key. Every component is deterministic for fixed inputs.

        Negated where the ordering is descending, so a single ascending sort expresses the whole
        rule. ``estimated_vram_bytes`` of ``None`` sorts last rather than first: a candidate whose
        footprint could not be estimated must not win a tie against one whose could.
        """
        vram = (
            float("inf") if self.estimated_vram_bytes is None else float(self.estimated_vram_bytes)
        )
        return (
            -self.final_score,
            -self.fit.mean_confidence,
            0 if self.resident else 1,
            vram,
            self.subject.facts.canonical_id,
            self.subject.facts.model_id,
        )


@dataclass(frozen=True, slots=True)
class Ranking:
    """The ordered candidates, split into the primary and its fallbacks.

    Attributes:
        ordered: Every eligible candidate, best first, each carrying its assigned ``rank``.
        primary: Rank 1, or ``None`` when nothing was eligible.
        fallbacks: Ranks 2 through ``1 + fallback_depth``.
    """

    ordered: tuple[RankedCandidate, ...]
    primary: RankedCandidate | None
    fallbacks: tuple[RankedCandidate, ...]


def rank_candidates(
    candidates: tuple[RankedCandidate, ...],
    *,
    fallback_depth: int = 0,
    disallow_fallback: bool = False,
) -> Ranking:
    """Order the eligible candidates and pick the primary and its fallbacks.

    Args:
        candidates: Every candidate that survived the hard constraints.
        fallback_depth: How many fallbacks the task profile allows.
        disallow_fallback: The request's override — fail rather than fall back (routing §10).

    Returns:
        The :class:`Ranking`. Empty in, empty out: ``primary`` is ``None`` and the caller raises
        ``NO_ELIGIBLE_MODEL`` with every rejection.
    """
    ordered = tuple(
        RankedCandidate(
            subject=candidate.subject,
            fit=candidate.fit,
            factors=candidate.factors,
            estimated_vram_bytes=candidate.estimated_vram_bytes,
            target_gpu_index=candidate.target_gpu_index,
            resident=candidate.resident,
            rank=position,
        )
        for position, candidate in enumerate(sorted(candidates, key=lambda c: c.order_key), start=1)
    )
    if not ordered:
        return Ranking(ordered=(), primary=None, fallbacks=())
    depth = 0 if disallow_fallback else max(fallback_depth, 0)
    return Ranking(ordered=ordered, primary=ordered[0], fallbacks=ordered[1 : 1 + depth])
