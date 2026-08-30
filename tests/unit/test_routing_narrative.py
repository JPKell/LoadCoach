"""The readable explanation: which capability moved the decision, what was absent, what to do."""

from __future__ import annotations

from typing import Any

import pytest

from loadcoach.domain.routing.narrative import narrate


def _candidate(
    canonical_id: str,
    *,
    rank: int,
    task_fit: float,
    reliability: float = 1.0,
    capabilities: list[dict[str, Any]],
) -> dict[str, Any]:
    final = task_fit * reliability
    return {
        "canonical_id": canonical_id,
        "model_id": "M" + canonical_id[-2:],
        "rank": rank,
        "task_fit": task_fit,
        "final_score": final,
        "capabilities": capabilities,
        "factors": {
            "reliability": reliability,
            "availability": 1.0,
            "residency": 1.0,
            "cost": 1.0,
            "reliability_detail": {"reason": f"7d window: factor {reliability:.3f}"},
        },
    }


def _payload() -> dict[str, Any]:
    winner = _candidate(
        "fake/alpha@sha256:aa",
        rank=1,
        task_fit=0.70,
        capabilities=[
            {
                "capability": "reasoning",
                "weight": 0.5,
                "score": 0.9,
                "confidence": 0.8,
                "source": "benchmark",
                "sample_count": 40,
                "evidence_age_days": 3,
            },
            {
                "capability": "instruction_following",
                "weight": 0.3,
                "score": 0.5,
                "confidence": 0.3,
                "source": "prior",
            },
            {
                "capability": "long_context",
                "weight": 0.2,
                "score": None,
                "confidence": None,
                "source": "evidence_profile_mismatch",
                "note": "evidence measured under runtime profile 1111, executing under 2222",
                "remedy": "freeweight run start --model alpha --context-size 32768",
            },
        ],
    )
    runner_up = _candidate(
        "fake/beta@sha256:bb",
        rank=2,
        task_fit=0.55,
        capabilities=[
            {
                "capability": "reasoning",
                "weight": 0.5,
                "score": 0.6,
                "confidence": 0.8,
                "source": "benchmark",
                "sample_count": 40,
                "evidence_age_days": 3,
            },
            {
                "capability": "instruction_following",
                "weight": 0.3,
                "score": 0.5,
                "confidence": 0.3,
                "source": "prior",
            },
            {
                "capability": "long_context",
                "weight": 0.2,
                "score": None,
                "confidence": None,
                "source": "absent",
                "note": "no evidence; excluded from the weighted mean",
            },
        ],
    )
    return {
        "decision_id": "01D",
        "task_profile": {"id": "general.reasoning", "version": "1.0.0"},
        "selected": {"canonical_id": winner["canonical_id"], "final_score": 0.70},
        "candidates": [winner, runner_up],
        "rejected": [
            {
                "canonical_id": "fake/gamma@sha256:cc",
                "reason": "insufficient_vram",
                "detail": {
                    "estimated_bytes": 41_000_000_000,
                    "free_bytes_by_gpu": {"0": 9_800_000_000},
                },
            }
        ],
        "flags": ["low_evidence"],
        "evidence_summary": {"source": "freeweight", "note": "2 bound records from FreeWeight"},
    }


def test_narrative_names_the_winner_the_margin_and_the_decisive_capability() -> None:
    narrative = narrate(_payload())
    assert narrative.selected == "fake/alpha@sha256:aa"
    assert narrative.runner_up == "fake/beta@sha256:bb"
    assert narrative.margin == pytest.approx(0.15)
    assert narrative.headline.startswith("Selected fake/alpha@sha256:aa for general.reasoning")
    assert "0.150 ahead of fake/beta@sha256:bb" in narrative.headline
    assert narrative.arithmetic == (
        "task fit 0.700 × reliability 1.000 × availability 1.000 × residency 1.000 "
        "× cost 1.000 = 0.700"
    )
    assert narrative.decisive is not None
    assert "reasoning" in narrative.decisive and "0.900" in narrative.decisive
    assert "0.600" in narrative.decisive and "ahead" in narrative.decisive
    assert narrative.reliability == "7d window: factor 1.000"
    assert narrative.evidence == "2 bound records from FreeWeight"


def test_drivers_are_largest_contribution_first_with_their_source_and_advantage() -> None:
    narrative = narrate(_payload())
    assert [d.capability for d in narrative.drivers] == ["reasoning", "instruction_following"]
    reasoning = narrative.drivers[0]
    assert reasoning.contribution == pytest.approx(0.5 * 0.9 * 0.8)
    assert reasoning.source == "benchmark" and reasoning.sample_count == 40
    assert reasoning.evidence_age_days == 3
    assert reasoning.runner_up_score == 0.6
    assert reasoning.advantage == pytest.approx(0.5 * 0.8 * (0.9 - 0.6))
    prior = narrative.drivers[1]
    assert prior.source == "prior" and prior.advantage == pytest.approx(0.0)


def test_absences_carry_the_reason_and_the_remedy_a_person_can_act_on() -> None:
    narrative = narrate(_payload())
    assert len(narrative.absences) == 1
    absence = narrative.absences[0]
    assert absence.capability == "long_context" and absence.weight == 0.2
    assert absence.source == "evidence_profile_mismatch"
    assert "executing under 2222" in absence.note
    assert absence.remedy == "freeweight run start --model alpha --context-size 32768"
    assert narrative.remedies == (absence.remedy,)


def test_flags_and_rejections_are_explained_in_words_with_their_numbers() -> None:
    narrative = narrate(_payload())
    assert narrative.flags[0][0] == "low_evidence"
    assert "declared flags and priors" in narrative.flags[0][1]
    rejection = narrative.rejections[0]
    assert rejection.canonical_id == "fake/gamma@sha256:cc"
    assert rejection.reason == "insufficient_vram"
    assert "does not fit in the free VRAM" in rejection.meaning
    assert rejection.numbers["estimated_bytes"] == 41_000_000_000


def test_a_reliability_factor_that_decided_it_is_named_as_such() -> None:
    payload = _payload()
    payload["candidates"][0]["factors"]["reliability"] = 0.5
    payload["candidates"][0]["final_score"] = 0.35
    payload["candidates"][1]["final_score"] = 0.55
    # The runner-up now wins on final score; swap ranks to mirror what ranking would do.
    payload["candidates"][0]["rank"], payload["candidates"][1]["rank"] = 2, 1
    payload["selected"] = {"canonical_id": "fake/beta@sha256:bb", "final_score": 0.55}
    narrative = narrate(payload)
    assert narrative.selected == "fake/beta@sha256:bb"
    assert narrative.decisive is not None
    assert narrative.decisive.startswith("The reliability factor decided it: 1.000 against 0.500")
    assert "task fit was lower by 0.150" in narrative.decisive


def test_no_eligible_candidate_reads_as_such_and_keeps_every_rejection() -> None:
    payload = _payload()
    payload["candidates"] = []
    payload["selected"] = None
    payload["rejected"].append(
        {
            "canonical_id": "fake/alpha@sha256:aa",
            "reason": "recently_failing",
            "detail": {"state": "open", "reason": "5 of 5 attempts failed"},
        }
    )
    narrative = narrate(payload)
    assert narrative.selected is None and narrative.drivers == ()
    assert narrative.headline.startswith("No candidate satisfied")
    assert [r.reason for r in narrative.rejections] == ["insufficient_vram", "recently_failing"]
    assert "circuit breaker is open" in narrative.rejections[1].meaning


def test_an_unknown_rejection_reason_is_still_rendered_in_words() -> None:
    payload = _payload()
    payload["rejected"] = [{"canonical_id": "x", "reason": "some_new_reason", "detail": {}}]
    assert narrate(payload).rejections[0].meaning == "some new reason"


def test_identical_candidates_name_the_tie_break() -> None:
    payload = _payload()
    payload["candidates"][1] = dict(
        payload["candidates"][0], canonical_id="fake/twin@sha256:dd", rank=2
    )
    payload["selected"]["final_score"] = 0.70
    narrative = narrate(payload)
    assert narrative.margin == pytest.approx(0.0)
    assert narrative.decisive is not None and "tie was broken" in narrative.decisive
