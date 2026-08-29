"""routing decisions and candidates

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-29 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
import weightsdb
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None


def upgrade() -> None:
    op.create_table(
        "routing_decisions",
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column("task_profile_id", sa.String(), nullable=False),
        sa.Column("task_profile_version", sa.String(), nullable=False),
        sa.Column("strategy_name", sa.String(), nullable=False),
        sa.Column("strategy_version", sa.String(), nullable=False),
        sa.Column("confidence_policy_version", sa.String(), nullable=False),
        sa.Column("requested_at", weightsdb.UtcDateTime(), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=False),
        sa.Column("selected_model_id", sa.String(length=26), nullable=True),
        sa.Column("selected_score", sa.Float(), nullable=True),
        sa.Column("selected_runtime_profile_id", sa.String(length=26), nullable=True),
        sa.Column("selected_served_context", sa.Integer(), nullable=True),
        sa.Column("selected_served_context_source", sa.String(), nullable=True),
        sa.Column("selected_target_gpu_index", sa.Integer(), nullable=True),
        sa.Column("flags_json", weightsdb.PortableJSON(), nullable=True),
        sa.Column("evidence_summary_json", weightsdb.PortableJSON(), nullable=True),
        sa.Column("overrides_json", weightsdb.PortableJSON(), nullable=True),
        sa.Column("telemetry_snapshot_json", weightsdb.PortableJSON(), nullable=True),
        sa.Column("explanation_json", weightsdb.PortableJSON(), nullable=False),
        sa.Column("created_at", weightsdb.UtcDateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["selected_model_id"],
            ["models.id"],
            name=op.f("fk_routing_decisions_selected_model_id_models"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["selected_runtime_profile_id"],
            ["runtime_profiles.id"],
            name=op.f("fk_routing_decisions_selected_runtime_profile_id_runtime_profiles"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_routing_decisions")),
    )
    with op.batch_alter_table("routing_decisions", schema=None) as batch_op:
        batch_op.create_index("ix_routing_decisions_requested_at", ["requested_at"], unique=False)
        batch_op.create_index(
            "ix_routing_decisions_task_profile_id", ["task_profile_id"], unique=False
        )

    op.create_table(
        "routing_candidates",
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column("decision_id", sa.String(length=26), nullable=False),
        sa.Column("model_id", sa.String(length=26), nullable=False),
        sa.Column("runtime_profile_id", sa.String(length=26), nullable=True),
        sa.Column("served_context", sa.Integer(), nullable=True),
        sa.Column("served_context_source", sa.String(), nullable=True),
        sa.Column("target_gpu_index", sa.Integer(), nullable=True),
        sa.Column("rank", sa.Integer(), nullable=True),
        sa.Column("task_fit", sa.Float(), nullable=True),
        sa.Column("final_score", sa.Float(), nullable=True),
        sa.Column("estimated_vram_bytes", sa.Integer(), nullable=True),
        sa.Column("capability_breakdown_json", weightsdb.PortableJSON(), nullable=True),
        sa.Column("factors_json", weightsdb.PortableJSON(), nullable=True),
        sa.Column("rejected", sa.Boolean(), nullable=False),
        sa.Column("rejection_reason", sa.String(), nullable=True),
        sa.Column("rejection_detail_json", weightsdb.PortableJSON(), nullable=True),
        sa.Column("created_at", weightsdb.UtcDateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["decision_id"],
            ["routing_decisions.id"],
            name=op.f("fk_routing_candidates_decision_id_routing_decisions"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["model_id"],
            ["models.id"],
            name=op.f("fk_routing_candidates_model_id_models"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["runtime_profile_id"],
            ["runtime_profiles.id"],
            name=op.f("fk_routing_candidates_runtime_profile_id_runtime_profiles"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_routing_candidates")),
    )
    with op.batch_alter_table("routing_candidates", schema=None) as batch_op:
        batch_op.create_index(
            "ix_routing_candidates_decision_id_rank", ["decision_id", "rank"], unique=False
        )
        batch_op.create_index("ix_routing_candidates_model_id", ["model_id"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("routing_candidates", schema=None) as batch_op:
        batch_op.drop_index("ix_routing_candidates_model_id")
        batch_op.drop_index("ix_routing_candidates_decision_id_rank")
    op.drop_table("routing_candidates")
    with op.batch_alter_table("routing_decisions", schema=None) as batch_op:
        batch_op.drop_index("ix_routing_decisions_task_profile_id")
        batch_op.drop_index("ix_routing_decisions_requested_at")
    op.drop_table("routing_decisions")
