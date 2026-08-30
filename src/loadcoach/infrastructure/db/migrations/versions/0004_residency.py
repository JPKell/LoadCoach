"""residency, and the claim index's direction

Revision ID: 0004
Revises: 0003
Create Date: 2026-08-29 00:00:00.000000

Phase 5 needs one new table — ``residency`` (queue §6, ADR-0027) — and one correction. ``0003``
created the claim index as ``(state, effective_priority, created_at)``, ascending throughout; the
data model names it ``(state, effective_priority DESC, created_at)``, and the direction is what
lets the claim's ``ORDER BY effective_priority DESC, created_at ASC`` walk the index instead of
sorting every equal-priority job through a temp B-tree on the hottest statement in the application
(ADR-0029, alternatives considered). The index is dropped and recreated with the direction the
data model always specified; no column of any table ``0003`` created is touched.
"""

from __future__ import annotations

import sqlalchemy as sa
import weightsdb
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None


def upgrade() -> None:
    op.create_table(
        "residency",
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column("model_id", sa.String(length=26), nullable=False),
        sa.Column("gpu_index", sa.Integer(), nullable=False),
        sa.Column("loaded_at", weightsdb.UtcDateTime(), nullable=False),
        sa.Column("last_used_at", weightsdb.UtcDateTime(), nullable=False),
        sa.Column("vram_bytes", sa.Float(), nullable=True),
        sa.Column("vram_bytes_unavailable_reason", sa.String(), nullable=True),
        sa.Column("resident", sa.Boolean(), nullable=False),
        sa.Column("unloaded_at", weightsdb.UtcDateTime(), nullable=True),
        sa.Column("unload_reason", sa.String(), nullable=True),
        sa.ForeignKeyConstraint(
            ["model_id"],
            ["models.id"],
            name=op.f("fk_residency_model_id_models"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_residency")),
        sa.UniqueConstraint(
            "model_id",
            "gpu_index",
            "loaded_at",
            name=op.f("uq_residency_model_id_gpu_index_loaded_at"),
        ),
    )
    with op.batch_alter_table("residency", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_residency_model_id"), ["model_id"], unique=False)
        batch_op.create_index(
            "ix_residency_resident_gpu_index", ["resident", "gpu_index"], unique=False
        )

    # Plain DDL rather than batch mode: an index can be dropped and created on SQLite directly,
    # and the ``DESC`` direction is the whole point of this statement (module docstring).
    op.drop_index("ix_jobs_state_effective_priority_created_at", table_name="jobs")
    op.create_index(
        "ix_jobs_state_effective_priority_created_at",
        "jobs",
        ["state", sa.text("effective_priority DESC"), "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_jobs_state_effective_priority_created_at", table_name="jobs")
    op.create_index(
        "ix_jobs_state_effective_priority_created_at",
        "jobs",
        ["state", "effective_priority", "created_at"],
        unique=False,
    )
    with op.batch_alter_table("residency", schema=None) as batch_op:
        batch_op.drop_index("ix_residency_resident_gpu_index")
        batch_op.drop_index(batch_op.f("ix_residency_model_id"))
    op.drop_table("residency")
