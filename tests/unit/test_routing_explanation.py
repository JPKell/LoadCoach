"""Explanation assembly (routing §8): the flags, and what the document must always name."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from baseaicore import RuntimeProfile

from loadcoach.domain.routing.constraints import Rejection
from loadcoach.domain.routing.explanation import (
    CONFIDENCE_POLICY_VERSION,
    STRATEGY_VERSION,
    RejectedCandidate,
    build_explanation,
)
from loadcoach.domain.routing.ranking import RankedCandidate, rank_candidates
from loadcoach.domain.routing.scoring import AdjustmentFactors, CapabilityScore, TaskFit
from loadcoach.domain.routing.subject import (
    ExecutionSubject,
    ModelFacts,
    ProviderFacts,
    ServedContext,
    ServedContextSource,
)

NOW = datetime(2026, 8, 29, 9, 14, 2, 318000, tzinfo=UTC)


def _subject(source: ServedContextSource = "configured") -> ExecutionSubject:
    return ExecutionSubject(
        facts=ModelFacts(
            model_id="01ABCDEFGHJKMNPQRSTVWXYZ00",
            canonical_id="fake/m@sha256:aaaa",
            provider_kind="fake",
            provider_model_name="m",
        ),
        provider=ProviderFacts(),
        runtime_profile=RuntimeProfile(context_size=32768),
        served_context=ServedContext(tokens=32768, source=source),
    )


def _candidate(
    *,
    measured_weight: float,
    source: ServedContextSource = "configured",
    capability_source: str = "benchmark",
) -> RankedCandidate:
    return RankedCandidate(
        subject=_subject(source),
        fit=TaskFit(
            task_fit=0.74,
            capabilities=(
                CapabilityScore(
                    capability_id="reasoning",
                    weight=1.0,
                    score=0.74,
                    confidence=0.62,
                    source=capability_source,
                ),
            ),
            present_weight=1.0,
            measured_weight=measured_weight,
        ),
        factors=AdjustmentFactors(),
        estimated_vram_bytes=9_000_000_000,
        target_gpu_index=0,
    )


def _build(
    candidate: RankedCandidate | None,
    *,
    min_present_weight: float = 0.5,
    rejected: tuple[RejectedCandidate, ...] = (),
) -> dict[str, Any]:
    ranking = rank_candidates(() if candidate is None else (candidate,))
    return build_explanation(
        decision_id="01ABCDEFGHJKMNPQRSTVWXYZ01",
        task_profile_id="code.review",
        task_profile_version="1.2.0",
        strategy_name="weighted_evidence",
        requested_at=NOW,
        duration_ms=18,
        ranking=ranking,
        rejected=rejected,
        budget=None,
        telemetry_snapshot=None,
        overrides=None,
        min_present_weight=min_present_weight,
    ).payload


def test_low_evidence_is_raised_below_the_floor_and_not_above_it() -> None:
    below = _build(_candidate(measured_weight=0.3), min_present_weight=0.5)
    assert "low_evidence" in below["flags"]

    above = _build(_candidate(measured_weight=0.8), min_present_weight=0.5)
    assert "low_evidence" not in above["flags"]


def test_priors_alone_always_read_as_low_evidence() -> None:
    """routing §5.1: routing without measurement is reasonable, and clearly labelled."""
    payload = _build(_candidate(measured_weight=0.0, capability_source="declared"))
    assert "low_evidence" in payload["flags"]
    assert payload["evidence_summary"]["source"] == "none"
    assert payload["evidence_summary"]["contributing_sources"] == ["declared"]


def test_assumed_context_is_flagged_only_when_the_context_was_assumed() -> None:
    assert "assumed_context" in _build(_candidate(measured_weight=1.0, source="assumed"))["flags"]
    assert "assumed_context" not in _build(_candidate(measured_weight=1.0))["flags"]


def test_every_decision_names_its_versions_and_the_selected_subject() -> None:
    """Acceptance criterion 1a."""
    payload = _build(_candidate(measured_weight=1.0))
    assert payload["strategy"] == {"name": "weighted_evidence", "version": STRATEGY_VERSION}
    assert payload["confidence_policy_version"] == CONFIDENCE_POLICY_VERSION
    assert payload["task_profile"] == {"id": "code.review", "version": "1.2.0"}
    assert payload["requested_at"] == "2026-08-29T09:14:02.318Z"
    selected = payload["selected"]
    assert selected["runtime_profile_hash"] == RuntimeProfile(context_size=32768).profile_hash
    assert selected["served_context"] == 32768
    assert selected["served_context_source"] == "configured"
    assert selected["target_gpu_index"] == 0


def test_a_rejection_keeps_its_numbers_and_its_subject() -> None:
    rejected = (
        RejectedCandidate(
            subject=_subject(),
            rejection=Rejection(
                "insufficient_vram",
                {"estimated_bytes": 41_000_000_000, "free_bytes_by_gpu": {"0": 9_800_000_000}},
            ),
        ),
    )
    payload = _build(None, rejected=rejected)
    assert payload["selected"] is None
    entry = payload["rejected"][0]
    assert entry["reason"] == "insufficient_vram"
    assert entry["detail"]["free_bytes_by_gpu"] == {"0": 9_800_000_000}
    assert entry["runtime_profile_hash"]
    assert entry["served_context_source"] == "configured"


def test_a_profile_mismatch_is_counted_in_the_evidence_summary() -> None:
    candidate = RankedCandidate(
        subject=_subject(),
        fit=TaskFit(
            task_fit=0.0,
            capabilities=(
                CapabilityScore(
                    capability_id="reasoning",
                    weight=1.0,
                    score=None,
                    confidence=None,
                    source="evidence_profile_mismatch",
                ),
            ),
            present_weight=0.0,
            measured_weight=0.0,
        ),
        factors=AdjustmentFactors(),
    )
    payload = _build(candidate)
    assert payload["evidence_summary"]["profile_mismatched_capabilities"] == 1
    assert payload["evidence_summary"]["source"] == "none"
