"""loadcoach.infrastructure.db.models — the declarative base and Phase 1 tables.

LoadCoach owns this ``MetaData``/``DeclarativeBase`` exclusively (database standards §1): WeightsDB
provides plumbing only and defines no application table, so each application — this one included —
keeps its own base with no cross-application meaning.

The naming convention is not cosmetic. Alembic's autogenerate diff and SQLite's batch-mode ALTER
both need every constraint and index to have a stable, predictable name; without one, a constraint
recreated by batch mode gets an auto-generated name that differs from the one the model produces,
and the parity check (database standards §5.2) fails forever on a schema that is actually correct.

``models``, ``model_capabilities``, ``runtime_profiles`` and ``task_profiles`` come from Phase 1's
migration ``0001``; ``routing_decisions`` and ``routing_candidates`` from Phase 3's ``0002``;
``jobs``, ``job_attempts``, ``job_events`` and ``validations`` from Phase 4's ``0003``; and
``residency`` from Phase 5's ``0004``; and ``capability_evidence`` and ``evidence_sources``
from Phase 6's ``0005``. ``settings`` and ``api_tokens`` mirror FreeWeight's own tables
(data model §2).
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
from weightsdb import PortableJSON, UtcDateTime, measurement_columns, ulid_primary_key

__all__ = [
    "ApiToken",
    "Base",
    "CapabilityEvidence",
    "EvidenceSource",
    "Model",
    "Job",
    "JobAttempt",
    "JobEvent",
    "ModelCapability",
    "Residency",
    "RoutingCandidate",
    "RoutingDecision",
    "RuntimeProfile",
    "Setting",
    "TaskProfile",
    "Validation",
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


class EvidenceSource(Base):
    """Where an evidence import came from (data model §2, ``evidence_sources``).

    One row per source, not per import: ``last_import_at``, ``last_status`` and ``record_count``
    describe the most recent attempt against that source, which is what ``GET /evidence/sources``
    and the ``evidence`` health component report. ``error_text`` holds the last failure's message
    and is cleared by a success, so "it is broken now" and "it broke once" stay distinguishable.

    ``source_key`` is the natural key an import upserts on: FreeWeight's own ``source_id`` for a
    bundle, so that re-importing the same producer updates one row rather than accumulating one
    per file.
    """

    __tablename__ = "evidence_sources"
    __table_args__ = (
        UniqueConstraint("source_key"),
        CheckConstraint("kind IN ('freeweight_api', 'file', 'manual')", name="kind"),
    )

    id: Mapped[str] = ulid_primary_key()
    source_key: Mapped[str] = mapped_column(String, nullable=False)
    kind: Mapped[str] = mapped_column(String, nullable=False)
    url: Mapped[str | None] = mapped_column(String, nullable=True)
    last_import_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    last_status: Mapped[str | None] = mapped_column(String, nullable=True)
    schema_version: Mapped[str | None] = mapped_column(String, nullable=True)
    record_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_text: Mapped[str | None] = mapped_column(String, nullable=True)
    generated_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, default=utcnow)


class CapabilityEvidence(Base):
    """One imported ``capability.evidence`` record (data model §2, ADR-0022 §1).

    Never edited by LoadCoach: a recomputation is a re-import, and the field set is the producer's
    (ADR-0022's normative table). Two properties of this table are load-bearing rather than
    incidental:

    * ``model_id`` is **nullable**, and ``match_state`` records why. Import never fails because a
      model has not been discovered, and binding is re-evaluated on every discovery pass
      (ADR-0022 §4). The identity triple and ``canonical_id`` are stored denormalized precisely so
      that an unbound row still knows what it describes.
    * ``policy_version`` is part of the uniqueness key, so two confidence policies coexist during
      a policy change and a re-import is a row-wise upsert rather than a collision (ADR-0022 §3).

    ``measured_at`` drives freshness and ``computed_at`` never does; both are stored because
    ``computed_at`` is what the producer's ``?since=`` filter compares against (ADR-0022 §5).

    ``record_json`` holds the ``capability.evidence`` payload **exactly as it arrived**. The
    columns beside it are the queryable projection ADR-0022 §1 makes normative; this is the
    document itself, kept because ADR-0025 §2 requires ``GET /evidence`` to return real
    ``capability.evidence`` envelopes and a payload rebuilt from the projection would be missing
    fields the projection does not carry (``model.observed_at`` among them). Keeping the source
    document is also the strongest form of "never edited by LoadCoach": a re-export is the
    producer's bytes, not a reconstruction that could drift from them.
    """

    __tablename__ = "capability_evidence"
    __table_args__ = (
        # Named explicitly: the convention's ``uq_%(table_name)s_%(column_0_N_name)s`` would
        # produce a 96-character identifier, and PostgreSQL truncates at 63 — which would make
        # the model's name and the database's name disagree for ever, and ``check_parity`` fail
        # on a schema that is in fact correct.
        UniqueConstraint(
            "source_id",
            "canonical_id",
            "runtime_profile_hash",
            "machine_fingerprint",
            "capability_id",
            "policy_version",
            name="uq_capability_evidence_subject",
        ),
        CheckConstraint(
            "match_state IN ('bound', 'unmatched', 'ambiguous_name_only')",
            name="match_state",
        ),
        Index("ix_capability_evidence_canonical_id_capability_id", "canonical_id", "capability_id"),
        Index("ix_capability_evidence_model_id_capability_id", "model_id", "capability_id"),
        Index("ix_capability_evidence_match_state", "match_state"),
    )

    id: Mapped[str] = ulid_primary_key()
    model_id: Mapped[str | None] = mapped_column(
        String(26), ForeignKey("models.id", ondelete="SET NULL"), nullable=True
    )
    provider_kind: Mapped[str] = mapped_column(String, nullable=False)
    provider_model_name: Mapped[str] = mapped_column(String, nullable=False)
    artifact_digest: Mapped[str | None] = mapped_column(String, nullable=True)
    canonical_id: Mapped[str] = mapped_column(String, nullable=False)
    match_state: Mapped[str] = mapped_column(String, nullable=False)
    runtime_profile_hash: Mapped[str] = mapped_column(String, nullable=False)
    machine_fingerprint: Mapped[str] = mapped_column(String, nullable=False)
    capability_id: Mapped[str] = mapped_column(String, nullable=False)
    score: Mapped[float] = mapped_column(nullable=False)
    confidence: Mapped[float] = mapped_column(nullable=False)
    sample_count: Mapped[int] = mapped_column(Integer, nullable=False)
    excluded_count: Mapped[int] = mapped_column(Integer, nullable=False)
    dispersion, dispersion_unavailable_reason = measurement_columns("dispersion")
    benchmark_versions_json: Mapped[object | None] = mapped_column(PortableJSON, nullable=True)
    dataset_hashes_json: Mapped[object | None] = mapped_column(PortableJSON, nullable=True)
    prompt_subset_hashes_json: Mapped[object | None] = mapped_column(PortableJSON, nullable=True)
    contributing_metrics_json: Mapped[object | None] = mapped_column(PortableJSON, nullable=True)
    source_run_ids_json: Mapped[object | None] = mapped_column(PortableJSON, nullable=True)
    identity_confidence: Mapped[str] = mapped_column(String, nullable=False)
    environment_snapshot_json: Mapped[object | None] = mapped_column(PortableJSON, nullable=True)
    goal_json: Mapped[object | None] = mapped_column(PortableJSON, nullable=True)
    measured_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    computed_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    imported_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, default=utcnow)
    source_id: Mapped[str] = mapped_column(
        String(26), ForeignKey("evidence_sources.id", ondelete="CASCADE"), nullable=False
    )
    policy_version: Mapped[str] = mapped_column(String, nullable=False)
    vocabulary_version: Mapped[str] = mapped_column(String, nullable=False)
    stale: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    stale_reason: Mapped[str | None] = mapped_column(String, nullable=True)
    record_json: Mapped[object | None] = mapped_column(PortableJSON, nullable=True)


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

    ``job_id`` is ``NULL`` for a ``POST /route`` call, which makes a decision without a job — the
        cheapest way to understand the system, and the one to reach for when a decision looks wrong.
        It was added by Phase 4's migration, once ``jobs`` existed for it to point at.

        ``explanation_json`` holds routing §8's document verbatim. The individual columns beside it
        are what queries filter and sort on; the document is what a person reads. Both are written
        from one in-memory structure, so they cannot disagree.
    """

    __tablename__ = "routing_decisions"
    __table_args__ = (Index("ix_routing_decisions_requested_at", "requested_at"),)

    id: Mapped[str] = ulid_primary_key()
    job_id: Mapped[str | None] = mapped_column(
        String(26), ForeignKey("jobs.id", ondelete="CASCADE"), nullable=True, index=True
    )
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


class Job(Base):
    """One execution, synchronous or queued (data model's ``jobs``).

    A synchronous ``POST /generate`` gets a job row too, so every execution has an explanation and
    a history — the alternative is two classes of execution, only one of which can be debugged.

    Phase 3's ``routing_decisions`` gains its ``job_id`` here: it was deliberately absent from
    migration ``0002`` because a nullable foreign key to a table that did not exist yet is not a
    column, it is a migration that cannot run.

    Queue columns (``class``, priorities, lease, ``scheduled_for``, ``max_wait_seconds``) were
    declared by Phase 4 and are written by Phase 5. Declaring them there rather than in a later
    migration kept one table definition rather than two, and cost nothing: an unwritten nullable
    column is free.

    The claim index is ``(state, effective_priority DESC, created_at)`` — the direction matters.
    The claim orders by ``effective_priority DESC, created_at ASC`` and an index ascending on both
    columns can serve only the first term, leaving SQLite to sort the whole equal-priority group
    through a temp B-tree on every claim (``EXPLAIN QUERY PLAN`` says so). Migration ``0004``
    recreated the index with the direction the data model always named.
    """

    __tablename__ = "jobs"
    __table_args__ = (
        UniqueConstraint("source", "idempotency_key"),
        Index("ix_jobs_task_profile_id_created_at", "task_profile_id", "created_at"),
        Index("ix_jobs_selected_model_id_created_at", "selected_model_id", "created_at"),
        Index("ix_jobs_lease_expires_at", "lease_expires_at"),
        Index("ix_jobs_state_queued_at", "state", "queued_at"),
    )

    id: Mapped[str] = ulid_primary_key()
    task_profile_id: Mapped[str] = mapped_column(String, nullable=False)
    task_profile_version: Mapped[str] = mapped_column(String, nullable=False)
    job_class: Mapped[str] = mapped_column("class", String, nullable=False, default="normal")
    base_priority: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    effective_priority: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    source: Mapped[str] = mapped_column(String, nullable=False, default="anonymous")
    state: Mapped[str] = mapped_column(String, nullable=False)
    state_reason: Mapped[str | None] = mapped_column(String, nullable=True)
    idempotency_key: Mapped[str | None] = mapped_column(String, nullable=True)
    idempotent: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    idempotency_expires_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    request_json: Mapped[object | None] = mapped_column(PortableJSON, nullable=True)
    prompt_hash: Mapped[str | None] = mapped_column(String, nullable=True)
    prompt_text: Mapped[str | None] = mapped_column(String, nullable=True)
    response_hash: Mapped[str | None] = mapped_column(String, nullable=True)
    response_text: Mapped[str | None] = mapped_column(String, nullable=True)
    structured_output_json: Mapped[object | None] = mapped_column(PortableJSON, nullable=True)
    tool_calls_json: Mapped[object | None] = mapped_column(PortableJSON, nullable=True)
    reasoning_available: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    reasoning_summary: Mapped[str | None] = mapped_column(String, nullable=True)
    reasoning_source: Mapped[str | None] = mapped_column(String, nullable=True)
    selected_model_id: Mapped[str | None] = mapped_column(
        String(26), ForeignKey("models.id", ondelete="SET NULL"), nullable=True
    )
    runtime_profile_id: Mapped[str | None] = mapped_column(
        String(26), ForeignKey("runtime_profiles.id", ondelete="SET NULL"), nullable=True
    )
    runtime_profile_hash: Mapped[str | None] = mapped_column(String, nullable=True)
    served_context: Mapped[int | None] = mapped_column(Integer, nullable=True)
    served_context_source: Mapped[str | None] = mapped_column(String, nullable=True)
    target_gpu_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    lease_owner: Mapped[str | None] = mapped_column(String, nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, default=utcnow)
    scheduled_for: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    queued_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    max_wait_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    queue_wait_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    provider_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    loadcoach_overhead_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    ttft_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    thinking_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    validation_passed: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    degradations_json: Mapped[object | None] = mapped_column(PortableJSON, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String, nullable=True)
    error_text: Mapped[str | None] = mapped_column(String, nullable=True)


# Declared after the class so the direction can be expressed on the column itself
# (``effective_priority.desc()``) rather than as opaque SQL text, which Alembic's parity check
# cannot compare against the reflected index.
Index(
    "ix_jobs_state_effective_priority_created_at",
    Job.state,
    Job.effective_priority.desc(),
    Job.created_at,
)


class JobAttempt(Base):
    """One try at one job, on one model (data model's ``job_attempts``).

    A corrective retry is a **new row**, never an edit of the previous one: the original attempt's
    output, timings and failure are what make the retry explicable, and a retry that overwrote
    them would leave a job history saying only that it eventually worked.
    """

    __tablename__ = "job_attempts"
    __table_args__ = (UniqueConstraint("job_id", "attempt"),)

    id: Mapped[str] = ulid_primary_key()
    job_id: Mapped[str] = mapped_column(
        String(26), ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    model_id: Mapped[str | None] = mapped_column(
        String(26), ForeignKey("models.id", ondelete="SET NULL"), nullable=True
    )
    runtime_profile_hash: Mapped[str | None] = mapped_column(String, nullable=True)
    rank: Mapped[int | None] = mapped_column(Integer, nullable=True)
    started_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    outcome: Mapped[str] = mapped_column(String, nullable=False)
    provider_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    ttft_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    finish_reason: Mapped[str | None] = mapped_column(String, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String, nullable=True)
    error_text: Mapped[str | None] = mapped_column(String, nullable=True)
    partial_response_hash: Mapped[str | None] = mapped_column(String, nullable=True)
    prompt_id: Mapped[str | None] = mapped_column(String, nullable=True)
    prompt_version: Mapped[str | None] = mapped_column(String, nullable=True)
    prompt_sha256: Mapped[str | None] = mapped_column(String, nullable=True)


class JobEvent(Base):
    """One entry in a job's event stream (data model's ``job_events``).

    The persisted half of the SSE contract: a reconnecting client replays from here by
    ``sequence``, which is why ``(job_id, sequence)`` is unique rather than merely indexed.
    """

    __tablename__ = "job_events"
    __table_args__ = (UniqueConstraint("job_id", "sequence"),)

    id: Mapped[str] = ulid_primary_key()
    job_id: Mapped[str] = mapped_column(
        String(26), ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    timestamp: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, default=utcnow)
    event_type: Mapped[str] = mapped_column(String, nullable=False)
    message: Mapped[str | None] = mapped_column(String, nullable=True)
    data_json: Mapped[object | None] = mapped_column(PortableJSON, nullable=True)


class Validation(Base):
    """One validation check's result (data model's ``validations``)."""

    __tablename__ = "validations"

    id: Mapped[str] = ulid_primary_key()
    job_attempt_id: Mapped[str] = mapped_column(
        String(26), ForeignKey("job_attempts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    kind: Mapped[str] = mapped_column(String, nullable=False)
    passed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    detail_json: Mapped[object | None] = mapped_column(PortableJSON, nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, default=utcnow)


class Residency(Base):
    """One model's presence on one device (data model's ``residency``; queue §6, ADR-0027).

    A row is one *residency episode*: the model was loaded on ``gpu_index`` at ``loaded_at`` and,
    if ``resident`` is false, unloaded at ``unloaded_at`` for ``unload_reason``. A model loaded
    twice has two rows, which is what makes load counts and idle times readable after the fact.
    ``max_resident_models`` is interpreted per ``gpu_index``.

    ``vram_bytes`` and ``vram_bytes_unavailable_reason`` follow
    :func:`weightsdb.measurement_columns`: a provider that cannot report device memory leaves the
    value ``NULL`` and the reason set, so "not measured" and "not measurable here" stay
    distinguishable (ADR-0016).
    """

    __tablename__ = "residency"
    __table_args__ = (
        UniqueConstraint("model_id", "gpu_index", "loaded_at"),
        Index("ix_residency_resident_gpu_index", "resident", "gpu_index"),
    )

    id: Mapped[str] = ulid_primary_key()
    model_id: Mapped[str] = mapped_column(
        String(26), ForeignKey("models.id", ondelete="CASCADE"), nullable=False, index=True
    )
    gpu_index: Mapped[int] = mapped_column(Integer, nullable=False)
    loaded_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    last_used_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    vram_bytes, vram_bytes_unavailable_reason = measurement_columns("vram_bytes")
    resident: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    unloaded_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    unload_reason: Mapped[str | None] = mapped_column(String, nullable=True)
