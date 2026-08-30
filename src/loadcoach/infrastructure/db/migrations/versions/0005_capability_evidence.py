"""capability_evidence and evidence_sources

Revision ID: 0005
Revises: 0004
Create Date: 2026-08-29 00:00:00.000000

Phase 6's two tables and nothing else. Every column an earlier revision created is left alone:
re-declaring one is drift, and ``check_parity`` reports it as such.

``capability_evidence.model_id`` is nullable with ``ON DELETE SET NULL``, which is the schema
half of ADR-0022 §4 — evidence for a model discovery has not seen is *retained*, and a model that
disappears from the registry leaves its evidence behind unbound rather than taking it along. The
uniqueness key carries ``policy_version`` so two confidence policies coexist during a policy
change (ADR-0022 §3), and it is named explicitly because the naming convention would generate an
identifier PostgreSQL would truncate.

The three indexes are data model §4's, and each answers a query the application actually makes:
``(model_id, capability_id)`` is routing's evidence lookup, ``(canonical_id, capability_id)`` is
the same lookup for evidence that is not bound to a registry row yet, and ``match_state`` is the
evidence page's filter and the import report's counts.
"""

from __future__ import annotations

import sqlalchemy as sa
import weightsdb
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None


def upgrade() -> None:
    op.create_table(
        "evidence_sources",
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column("source_key", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("url", sa.String(), nullable=True),
        sa.Column("last_import_at", weightsdb.UtcDateTime(), nullable=True),
        sa.Column("last_status", sa.String(), nullable=True),
        sa.Column("schema_version", sa.String(), nullable=True),
        sa.Column("record_count", sa.Integer(), nullable=False),
        sa.Column("error_text", sa.String(), nullable=True),
        sa.Column("generated_at", weightsdb.UtcDateTime(), nullable=True),
        sa.Column("created_at", weightsdb.UtcDateTime(), nullable=False),
        sa.CheckConstraint(
            "kind IN ('freeweight_api', 'file', 'manual')",
            name=op.f("ck_evidence_sources_kind"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_evidence_sources")),
        sa.UniqueConstraint("source_key", name=op.f("uq_evidence_sources_source_key")),
    )
    op.create_table(
        "capability_evidence",
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column("model_id", sa.String(length=26), nullable=True),
        sa.Column("provider_kind", sa.String(), nullable=False),
        sa.Column("provider_model_name", sa.String(), nullable=False),
        sa.Column("artifact_digest", sa.String(), nullable=True),
        sa.Column("canonical_id", sa.String(), nullable=False),
        sa.Column("match_state", sa.String(), nullable=False),
        sa.Column("runtime_profile_hash", sa.String(), nullable=False),
        sa.Column("machine_fingerprint", sa.String(), nullable=False),
        sa.Column("capability_id", sa.String(), nullable=False),
        sa.Column("score", sa.Float(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("sample_count", sa.Integer(), nullable=False),
        sa.Column("excluded_count", sa.Integer(), nullable=False),
        sa.Column("dispersion", sa.Float(), nullable=True),
        sa.Column("dispersion_unavailable_reason", sa.String(), nullable=True),
        sa.Column("benchmark_versions_json", weightsdb.PortableJSON(), nullable=True),
        sa.Column("dataset_hashes_json", weightsdb.PortableJSON(), nullable=True),
        sa.Column("prompt_subset_hashes_json", weightsdb.PortableJSON(), nullable=True),
        sa.Column("contributing_metrics_json", weightsdb.PortableJSON(), nullable=True),
        sa.Column("source_run_ids_json", weightsdb.PortableJSON(), nullable=True),
        sa.Column("identity_confidence", sa.String(), nullable=False),
        sa.Column("environment_snapshot_json", weightsdb.PortableJSON(), nullable=True),
        sa.Column("goal_json", weightsdb.PortableJSON(), nullable=True),
        sa.Column("measured_at", weightsdb.UtcDateTime(), nullable=False),
        sa.Column("computed_at", weightsdb.UtcDateTime(), nullable=False),
        sa.Column("imported_at", weightsdb.UtcDateTime(), nullable=False),
        sa.Column("source_id", sa.String(length=26), nullable=False),
        sa.Column("policy_version", sa.String(), nullable=False),
        sa.Column("vocabulary_version", sa.String(), nullable=False),
        sa.Column("stale", sa.Boolean(), nullable=False),
        sa.Column("stale_reason", sa.String(), nullable=True),
        sa.CheckConstraint(
            "match_state IN ('bound', 'unmatched', 'ambiguous_name_only')",
            name=op.f("ck_capability_evidence_match_state"),
        ),
        sa.ForeignKeyConstraint(
            ["model_id"],
            ["models.id"],
            name=op.f("fk_capability_evidence_model_id_models"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["source_id"],
            ["evidence_sources.id"],
            name=op.f("fk_capability_evidence_source_id_evidence_sources"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_capability_evidence")),
        sa.UniqueConstraint(
            "source_id",
            "canonical_id",
            "runtime_profile_hash",
            "machine_fingerprint",
            "capability_id",
            "policy_version",
            name="uq_capability_evidence_subject",
        ),
    )
    with op.batch_alter_table("capability_evidence", schema=None) as batch_op:
        batch_op.create_index(
            "ix_capability_evidence_canonical_id_capability_id",
            ["canonical_id", "capability_id"],
            unique=False,
        )
        batch_op.create_index("ix_capability_evidence_match_state", ["match_state"], unique=False)
        batch_op.create_index(
            "ix_capability_evidence_model_id_capability_id",
            ["model_id", "capability_id"],
            unique=False,
        )


def downgrade() -> None:
    with op.batch_alter_table("capability_evidence", schema=None) as batch_op:
        batch_op.drop_index("ix_capability_evidence_model_id_capability_id")
        batch_op.drop_index("ix_capability_evidence_match_state")
        batch_op.drop_index("ix_capability_evidence_canonical_id_capability_id")
    op.drop_table("capability_evidence")
    op.drop_table("evidence_sources")
