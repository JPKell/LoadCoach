"""Tests for loadcoach.domain.registry — declared-capability extraction, honestly reported."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from baseaicore import (
    ModelCapabilityFlag,
    ModelDescriptor,
    ModelIdentity,
    ProviderKind,
)

from loadcoach.domain.registry import (
    ManualScoreInvalid,
    declared_capabilities_for,
    validate_manual_score,
)


def _descriptor(
    *flags: ModelCapabilityFlag, provider_kind: ProviderKind = ProviderKind.OLLAMA
) -> ModelDescriptor:
    identity = ModelIdentity(
        provider_kind=provider_kind,
        provider_model_name="some-model:8b",
        artifact_digest="sha256:" + "a" * 64,
    )
    return ModelDescriptor(
        identity=identity,
        observed_at=datetime.now(UTC),
        declared_capabilities=frozenset(flags),
    )


def test_ollama_style_descriptor_with_tools_and_structured_output() -> None:
    """Declared capabilities extracted correctly from an Ollama-shaped fixture."""
    descriptor = _descriptor(
        ModelCapabilityFlag.TOOLS,
        ModelCapabilityFlag.STRUCTURED_OUTPUT,
        provider_kind=ProviderKind.OLLAMA,
    )
    result = {item.capability_id: item.score for item in declared_capabilities_for(descriptor)}
    assert result == {"tool_use": 1.0, "structured_output": 1.0}


def test_openai_compatible_style_descriptor_with_fewer_flags() -> None:
    """An OpenAI-compatible provider reporting fewer flags produces fewer rows, honestly."""
    descriptor = _descriptor(
        ModelCapabilityFlag.STRUCTURED_OUTPUT, provider_kind=ProviderKind.OPENAI_COMPATIBLE
    )
    result = declared_capabilities_for(descriptor)
    assert len(result) == 1
    assert result[0].capability_id == "structured_output"


def test_no_declared_flags_produces_no_rows() -> None:
    descriptor = _descriptor()
    assert declared_capabilities_for(descriptor) == ()


def test_flags_with_no_setspec_counterpart_are_not_fabricated() -> None:
    """VISION, THINKING, EMBEDDING have no SetSpec vocabulary (1.1) counterpart — never padded."""
    descriptor = _descriptor(
        ModelCapabilityFlag.VISION, ModelCapabilityFlag.THINKING, ModelCapabilityFlag.EMBEDDING
    )
    assert declared_capabilities_for(descriptor) == ()


def test_declared_confidence_is_lower_than_a_measured_score() -> None:
    """A declared flag is a binary claim, not a benchmark — its confidence reflects that."""
    descriptor = _descriptor(ModelCapabilityFlag.TOOLS)
    (result,) = declared_capabilities_for(descriptor)
    assert result.score == 1.0
    assert 0.0 < result.confidence < 1.0


def test_result_order_is_deterministic() -> None:
    descriptor = _descriptor(ModelCapabilityFlag.STRUCTURED_OUTPUT, ModelCapabilityFlag.TOOLS)
    first = declared_capabilities_for(descriptor)
    second = declared_capabilities_for(descriptor)
    assert first == second


def test_valid_manual_score() -> None:
    result = validate_manual_score(
        "manual_capability_scores.toml",
        0,
        {
            "canonical_id": "ollama/x@sha256:" + "a" * 64,
            "capability_id": "coding",
            "score": 0.6,
            "confidence": 0.3,
        },
    )
    assert result.capability_id == "coding"
    assert result.score == 0.6
    assert result.confidence == 0.3


def test_manual_score_unknown_capability_rejected() -> None:
    with pytest.raises(ManualScoreInvalid) as excinfo:
        validate_manual_score(
            "f.toml",
            2,
            {"canonical_id": "x", "capability_id": "telepathy", "score": 0.5, "confidence": 0.5},
        )
    assert excinfo.value.details["index"] == 2
    assert "telepathy" in excinfo.value.details["problem"]


@pytest.mark.parametrize("bad_score", [-0.1, 1.1, "high", None])
def test_manual_score_out_of_range_rejected(bad_score: object) -> None:
    with pytest.raises(ManualScoreInvalid):
        validate_manual_score(
            "f.toml",
            0,
            {"canonical_id": "x", "capability_id": "coding", "score": bad_score, "confidence": 0.5},
        )


def test_manual_score_missing_canonical_id_rejected() -> None:
    with pytest.raises(ManualScoreInvalid):
        validate_manual_score(
            "f.toml", 0, {"capability_id": "coding", "score": 0.5, "confidence": 0.5}
        )
