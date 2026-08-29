"""loadcoach.domain.registry — turning a discovered model into declared-capability signals.

Framework-free per `.importlinter`'s domain-purity contract: this module knows about
:class:`~baseaicore.ModelDescriptor` (a domain type, not a framework) and produces plain data the
service layer persists — it never touches a database or an HTTP client itself.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

import setspec
from baseaicore import ModelCapabilityFlag, ModelDescriptor, SuiteError, is_supported

__all__ = [
    "DESCRIPTOR_GEOMETRY_FIELDS",
    "DeclaredCapability",
    "ManualCapabilityScore",
    "ManualScoreInvalid",
    "declared_capabilities_for",
    "descriptor_geometry",
    "geometry_from_json",
    "validate_manual_score",
]

DESCRIPTOR_GEOMETRY_FIELDS: tuple[str, ...] = (
    "layers",
    "kv_heads",
    "head_dim",
    "attention_heads",
    "embedding_dim",
    "vocab_size",
    "sliding_window",
)
"""The descriptor fields the VRAM/KV estimator needs that the ``models`` table has no column for.

``layers``, ``kv_heads`` and ``head_dim`` are the three the theoretical KV figure is computed
from; the rest are stored alongside them because they cost nothing and a later phase that wants
one should not need a migration to get it. Everything here is a
:class:`~baseaicore.Measurement`, so a field the provider never reported is **omitted** rather
than stored as zero — reading it back gives ``None``, which the estimator treats as "cannot
compute", not as "needs none" (ADR-0016).
"""

# Only flags with an honest, unambiguous SetSpec capability counterpart are translated. VISION,
# THINKING and EMBEDDING have no corresponding entry in the vocabulary (1.1) — inventing one would
# be exactly the dishonest "fewer flags, less honestly reported" failure mode dev-plan P2 names,
# so a provider that declares those flags simply contributes no row for them, and a provider that
# declares fewer flags than another produces fewer rows, not a padded-out equal set.
_FLAG_TO_CAPABILITY: dict[ModelCapabilityFlag, str] = {
    ModelCapabilityFlag.TOOLS: "tool_use",
    ModelCapabilityFlag.STRUCTURED_OUTPUT: "structured_output",
}

# A declared flag is a binary "the provider says so," not a measured score — scored at 1.0 (the
# flag is present) with a confidence well below what a benchmark run earns, so routing can prefer
# real evidence over a declaration the moment any exists (spec §11 evidence contract).
_DECLARED_SCORE = 1.0
_DECLARED_CONFIDENCE = 0.5


@dataclass(frozen=True, slots=True)
class DeclaredCapability:
    """One ``model_capabilities`` row with ``source="declared"``."""

    capability_id: str
    score: float
    confidence: float


def declared_capabilities_for(descriptor: ModelDescriptor) -> tuple[DeclaredCapability, ...]:
    """Return the declared-capability rows a discovered model's flags translate to.

    Args:
        descriptor: One model as reported by a provider's ``list_models()``.

    Returns:
        One :class:`DeclaredCapability` per flag that has a SetSpec counterpart, honestly limited
        to what the provider actually declared — never padded to a fixed set.
    """
    return tuple(
        DeclaredCapability(
            capability_id=capability_id, score=_DECLARED_SCORE, confidence=_DECLARED_CONFIDENCE
        )
        for flag in sorted(descriptor.declared_capabilities, key=lambda item: item.value)
        if (capability_id := _FLAG_TO_CAPABILITY.get(flag)) is not None
    )


class ManualScoreInvalid(SuiteError):
    """One entry in ``manual_capability_scores.toml`` failed validation.

    ``details`` carries ``file``, ``index`` (position in the ``[[scores]]`` array) and ``problem``
    — the same file/key/problem naming discipline as :class:`~loadcoach.domain.task_profile
    .TaskProfileInvalid`.
    """

    code: ClassVar[str] = "MANUAL_SCORE_INVALID"


@dataclass(frozen=True, slots=True)
class ManualCapabilityScore:
    """One operator-entered ``model_capabilities`` row, validated, with ``source="manual"``."""

    canonical_id: str
    capability_id: str
    score: float
    confidence: float


def validate_manual_score(file: str, index: int, raw: dict[str, object]) -> ManualCapabilityScore:
    """Validate one ``[[scores]]`` entry from ``manual_capability_scores.toml``.

    Args:
        file: The source file, named in any :class:`ManualScoreInvalid` raised.
        index: This entry's position in the array, named in the same error.
        raw: The entry as parsed from TOML.

    Returns:
        The validated :class:`ManualCapabilityScore`.

    Raises:
        ManualScoreInvalid: A required key is missing or the wrong type, ``score``/``confidence``
            is outside ``[0.0, 1.0]``, or ``capability_id`` is not in the SetSpec vocabulary.
    """

    def fail(problem: str) -> ManualScoreInvalid:
        return ManualScoreInvalid(
            f"manual_capability_scores.toml[{index}] is invalid: {problem}",
            details={"file": file, "index": index, "problem": problem},
        )

    canonical_id = raw.get("canonical_id")
    capability_id = raw.get("capability_id")
    score = raw.get("score")
    confidence = raw.get("confidence")

    if not isinstance(canonical_id, str) or not canonical_id:
        raise fail("canonical_id must be a non-empty string")
    if not isinstance(capability_id, str) or not setspec.is_known_capability(capability_id):
        raise fail(
            f"capability_id {capability_id!r} is not in the SetSpec vocabulary "
            f"(version {setspec.CAPABILITY_VOCABULARY_VERSION})"
        )
    if not isinstance(score, (int, float)) or isinstance(score, bool) or not 0.0 <= score <= 1.0:
        raise fail(f"score must be a number in [0.0, 1.0]; got {score!r}")
    if (
        not isinstance(confidence, (int, float))
        or isinstance(confidence, bool)
        or not 0.0 <= confidence <= 1.0
    ):
        raise fail(f"confidence must be a number in [0.0, 1.0]; got {confidence!r}")

    return ManualCapabilityScore(
        canonical_id=canonical_id,
        capability_id=capability_id,
        score=float(score),
        confidence=float(confidence),
    )


def descriptor_geometry(descriptor: ModelDescriptor) -> dict[str, int]:
    """Return the descriptor's model geometry, omitting everything unreported.

    Args:
        descriptor: One model as reported by a provider.

    Returns:
        Field name -> value, containing only the fields this provider actually reported. An
        empty mapping is a legitimate result and means the provider exposed no geometry at all.
    """
    geometry: dict[str, int] = {}
    for name in DESCRIPTOR_GEOMETRY_FIELDS:
        value = getattr(descriptor, name)
        if is_supported(value):
            geometry[name] = int(value)
    return geometry


def geometry_from_json(stored: object, field: str) -> int | None:
    """Read one geometry field back out of a stored ``descriptor_json`` column.

    Args:
        stored: Whatever the column held. Anything that is not a mapping yields ``None``.
        field: The field to read.

    Returns:
        The value, or ``None`` when it was never stored — never ``0``.
    """
    if not isinstance(stored, dict):
        return None
    value = stored.get(field)
    return value if isinstance(value, int) and not isinstance(value, bool) else None
