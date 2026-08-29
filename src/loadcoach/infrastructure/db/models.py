"""loadcoach.infrastructure.db.models — the declarative base and Phase 1 tables.

LoadCoach owns this ``MetaData``/``DeclarativeBase`` exclusively (database standards §1): WeightsDB
provides plumbing only and defines no application table, so each application — this one included —
keeps its own base with no cross-application meaning.

The naming convention is not cosmetic. Alembic's autogenerate diff and SQLite's batch-mode ALTER
both need every constraint and index to have a stable, predictable name; without one, a constraint
recreated by batch mode gets an auto-generated name that differs from the one the model produces,
and the parity check (database standards §5.2) fails forever on a schema that is actually correct.

``models``, ``model_capabilities``, ``runtime_profiles`` and ``task_profiles`` come from Phase 1's
migration ``0001``; ``routing_decisions`` and ``routing_candidates`` from Phase 3's ``0002``.
``settings`` and ``api_tokens`` mirror FreeWeight's own tables (data model §2).
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from weightsdb import PortableJSON, UtcDateTime, ulid_primary_key

__all__ = [
    "ApiToken",
    "Base",
    "Model",
    "ModelCapability",
    "RoutingCandidate",
    "RoutingDecision",
    "RuntimeProfile",
    "Setting",
    "TaskProfile",
    "utcnow",
]

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """The one declarative base for every LoadCoach-owned table.

    ``metadata`` here is the single source of truth Alembic's autogenerate compares against
    (``MigrationRunner.check_parity``) — a model added without importing it here is invisible to
    that check, not merely untested.
    """

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def utcnow() -> datetime:
    """Return the current instant, timezone-aware in UTC.

    Used only as a ``mapped_column`` default for ``created_at``/``first_seen_at``-style columns —
    an infrastructure-layer concern distinct from the ``Clock`` a service or domain function takes
    as a parameter (coding standards §5).
    """
    return datetime.now(UTC)


class Model(Base):
    """A discovered or declared model. Identity columns identical to FreeWeight's (data model §2).

    Discovery (Phase 2) populates this table; nothing writes to it yet.
    """

    __tablename__ = "models"
    __table_args__ = (
        Index(
            "uq_models_identity_triple",
            "provider_kind",
            "provider_model_name",
            "artifact_digest",
            unique=True,
        ),
        Index(
            "uq_models_name_only",
            "provider_kind",
            "provider_model_name",
            unique=True,
            sqlite_where="artifact_digest IS NULL",
            postgresql_where="artifact_digest IS NULL",
        ),
        CheckConstraint(
            "identity_confidence IN ('digest', 'name_only')",
            name="identity_confidence",
        ),
        CheckConstraint(
            "provider_kind IN ('ollama', 'openai_compatible', 'llamacpp', 'vllm', 'fake')",
            name="provider_kind",
        ),
    )

    id: Mapped[str] = ulid_primary_key()
    provider_kind: Mapped[str] = mapped_column(String, nullable=False)
    provider_model_name: Mapped[str] = mapped_column(String, nullable=False)
    artifact_digest: Mapped[str | None] = mapped_column(String, nullable=True)
    canonical_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    identity_confidence: Mapped[str] = mapped_column(String, nullable=False)
    descriptor_json: Mapped[object | None] = mapped_column(PortableJSON, nullable=True)
    declared_capabilities_json: Mapped[object | None] = mapped_column(PortableJSON, nullable=True)
    max_context: Mapped[int | None] = mapped_column(Integer, nullable=True)
    size_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    quantization: Mapped[str | None] = mapped_column(String, nullable=True)
    family: Mapped[str | None] = mapped_column(String, nullable=True)
    parameter_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    first_seen_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, default=utcnow)
    available: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    unavailable_reason: Mapped[str | None] = mapped_column(String, nullable=True)


class ModelCapability(Base):
    """A non-benchmark capability signal: a declared flag, a manual score, or a prior.

    Populated by Phase 2 (manual scores from configuration) and later phases (declared, production).
    """

    __tablename__ = "model_capabilities"
    __table_args__ = (
        UniqueConstraint("model_id", "capability_id", "source"),
        CheckConstraint(
            "source IN ('declared', 'manual', 'prior', 'production')",
            name="source",
        ),
    )

    id: Mapped[str] = ulid_primary_key()
    model_id: Mapped[str] = mapped_column(
        String(26), ForeignKey("models.id", ondelete="CASCADE"), nullable=False, index=True
    )
    capability_id: Mapped[str] = mapped_column(String, nullable=False)
    score: Mapped[float | None] = mapped_column(nullable=True)
    confidence: Mapped[float] = mapped_column(nullable=False)
    source: Mapped[str] = mapped_column(String, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, default=utcnow)


class RuntimeProfile(Base):
    """The settings an execution runs under (ADR-0023). Mirrors FreeWeight's table (data model)."""

    __tablename__ = "runtime_profiles"

    id: Mapped[str] = ulid_primary_key()
    profile_hash: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    context_size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    kv_cache_precision: Mapped[str | None] = mapped_column(String, nullable=True)
    gpu_layers: Mapped[int | None] = mapped_column(Integer, nullable=True)
    flash_attention: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    threads: Mapped[int | None] = mapped_column(Integer, nullable=True)
    batch_size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    keep_alive: Mapped[str | None] = mapped_column(String, nullable=True)
    provider_options_json: Mapped[object | None] = mapped_column(PortableJSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, default=utcnow)


class TaskProfile(Base):
    """A named routing intent, imported from configuration (Phase 2)."""

    __tablename__ = "task_profiles"
    __table_args__ = (UniqueConstraint("profile_id", "version"),)

    id: Mapped[str] = ulid_primary_key()
    profile_id: Mapped[str] = mapped_column(String, nullable=False)
    version: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str | None] = mapped_column(String, nullable=True)
    weights_json: Mapped[object] = mapped_column(PortableJSON, nullable=False)
    constraints_json: Mapped[object] = mapped_column(PortableJSON, nullable=False)
    execution_json: Mapped[object] = mapped_column(PortableJSON, nullable=False)
    validation_json: Mapped[object | None] = mapped_column(PortableJSON, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, default=utcnow)


class Setting(Base):
    """One runtime-changeable configuration value. As in FreeWeight (data model §2)."""

    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String, primary_key=True)
    value_json: Mapped[object | None] = mapped_column(PortableJSON)
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, default=utcnow)


class ApiToken(Base):
    """One bearer token accepted for a non-loopback bind (ADR-0026). As in FreeWeight.

    ``token_sha256`` is the only form of the token this table ever stores; the bearer value itself
    is shown to the operator exactly once, at creation, and is not recoverable from this row.
    """

    __tablename__ = "api_tokens"

    id: Mapped[str] = ulid_primary_key()
    name: Mapped[str] = mapped_column(String, nullable=False)
    token_sha256: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    scope: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, default=utcnow)
    last_used_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    expires_at: Mapped[datetime | None] = mapped_column(UtcDateTime)
    revoked_at: Mapped[datetime | None] = mapped_column(UtcDateTime)


class RoutingDecision(Base):
    """One complete routing decision, persisted for every decision — never sampled (routing §8).

    ``job_id`` is absent from Phase 3 entirely: the ``jobs`` table arrives with Phase 4, and a
    nullable foreign key to a table that does not exist yet is not a column, it is a migration
    that cannot run. Phase 4 adds it, which is additive and needs no rewrite here (data model's
    own ``routing_decisions`` sketch marks the column ``FK NULL`` for exactly the ``/route``
    case this phase serves).

    ``explanation_json`` holds routing §8's document verbatim. The individual columns beside it
    are what queries filter and sort on; the document is what a person reads. Both are written
    from one in-memory structure, so they cannot disagree.
    """

    __tablename__ = "routing_decisions"
    __table_args__ = (Index("ix_routing_decisions_requested_at", "requested_at"),)

    id: Mapped[str] = ulid_primary_key()
    task_profile_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    task_profile_version: Mapped[str] = mapped_column(String, nullable=False)
    strategy_name: Mapped[str] = mapped_column(String, nullable=False)
    strategy_version: Mapped[str] = mapped_column(String, nullable=False)
    confidence_policy_version: Mapped[str] = mapped_column(String, nullable=False)
    requested_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    selected_model_id: Mapped[str | None] = mapped_column(
        String(26), ForeignKey("models.id", ondelete="SET NULL"), nullable=True
    )
    selected_score: Mapped[float | None] = mapped_column(nullable=True)
    selected_runtime_profile_id: Mapped[str | None] = mapped_column(
        String(26), ForeignKey("runtime_profiles.id", ondelete="SET NULL"), nullable=True
    )
    selected_served_context: Mapped[int | None] = mapped_column(Integer, nullable=True)
    selected_served_context_source: Mapped[str | None] = mapped_column(String, nullable=True)
    selected_target_gpu_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    flags_json: Mapped[object | None] = mapped_column(PortableJSON, nullable=True)
    evidence_summary_json: Mapped[object | None] = mapped_column(PortableJSON, nullable=True)
    overrides_json: Mapped[object | None] = mapped_column(PortableJSON, nullable=True)
    telemetry_snapshot_json: Mapped[object | None] = mapped_column(PortableJSON, nullable=True)
    explanation_json: Mapped[object] = mapped_column(PortableJSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, default=utcnow)


class RoutingCandidate(Base):
    """One model as this decision saw it: scored and ranked, or rejected with its numbers.

    ``rank`` is ``NULL`` for a rejected candidate. ``runtime_profile_id``, ``served_context`` and
    ``served_context_source`` are here rather than on the decision because a candidate *is* the
    pair ``(identity, resolved runtime profile)`` (ADR-0023): two candidates in one decision can
    resolve to different profiles and different served contexts, so the values belong to the
    candidate row. ``target_gpu_index`` likewise names the device that satisfied admission for
    that candidate specifically (ADR-0027 §2).
    """

    __tablename__ = "routing_candidates"
    __table_args__ = (Index("ix_routing_candidates_decision_id_rank", "decision_id", "rank"),)

    id: Mapped[str] = ulid_primary_key()
    decision_id: Mapped[str] = mapped_column(
        String(26),
        ForeignKey("routing_decisions.id", ondelete="CASCADE"),
        nullable=False,
    )
    model_id: Mapped[str] = mapped_column(
        String(26), ForeignKey("models.id", ondelete="CASCADE"), nullable=False, index=True
    )
    runtime_profile_id: Mapped[str | None] = mapped_column(
        String(26), ForeignKey("runtime_profiles.id", ondelete="SET NULL"), nullable=True
    )
    served_context: Mapped[int | None] = mapped_column(Integer, nullable=True)
    served_context_source: Mapped[str | None] = mapped_column(String, nullable=True)
    target_gpu_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    rank: Mapped[int | None] = mapped_column(Integer, nullable=True)
    task_fit: Mapped[float | None] = mapped_column(nullable=True)
    final_score: Mapped[float | None] = mapped_column(nullable=True)
    estimated_vram_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    capability_breakdown_json: Mapped[object | None] = mapped_column(PortableJSON, nullable=True)
    factors_json: Mapped[object | None] = mapped_column(PortableJSON, nullable=True)
    rejected: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    rejection_reason: Mapped[str | None] = mapped_column(String, nullable=True)
    rejection_detail_json: Mapped[object | None] = mapped_column(PortableJSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, default=utcnow)
