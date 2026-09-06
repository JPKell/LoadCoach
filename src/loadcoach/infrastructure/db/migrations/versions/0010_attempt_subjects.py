"""the subject on jobs and attempts, with the classification it ran under

Revision ID: 0010
Revises: 0009
Create Date: 2026-09-05 00:00:00.000000

Gate E of LoadCoach 1.1: a pin selects an adapter subject, and every attempt records the subject
that answered (ADR-0080). `jobs` gains the selected subject; `job_attempts` gains it too, plus the
two classification columns ADR-0065 rule 4 asks for.

**Why the classification is written when it is redundant.** Adapters are local-only, so the
lattice is satisfied by construction at serving time — and the adapter's own classification and
the effective `max(caller, adapter)` are *still* recorded on every attempt that used one. An
invariant nothing records is an invariant nobody can check.

**Everything is nullable with no backfill.** An attempt made before 1.1 ran on a bare base, and a
bare base's subject string is its model's canonical ID — but the model row it points at may since
have been re-discovered under a different digest, so writing one now would be a reconstruction
rather than a record. `NULL` reads as "not recorded", which is the truth about a row written
before the column existed (ADR-0016's instinct, applied to history).

**The claim index is rebuilt by hand afterwards.** Adding the foreign key to `jobs` is a table
rebuild on SQLite, and alembic recreates the table's indexes from what it reflected — which loses
the `DESC` direction on `(state, effective_priority DESC, created_at)`, the direction migration
`0004` existed to fix and the reason the hottest statement in the application walks the index
instead of sorting an equal-priority group through a temp B-tree. It is dropped and recreated with
the direction, exactly as `0004` writes it.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None


def upgrade() -> None:
    with op.batch_alter_table("jobs", schema=None) as batch_op:
        batch_op.add_column(sa.Column("selected_adapter_id", sa.String(length=26), nullable=True))
        batch_op.add_column(sa.Column("selected_subject_canonical_id", sa.String(), nullable=True))
        batch_op.create_foreign_key(
            "fk_jobs_selected_adapter_id",
            "adapters",
            ["selected_adapter_id"],
            ["id"],
            ondelete="SET NULL",
        )
    # 0004's direction, restored after the rebuild (module docstring).
    op.drop_index("ix_jobs_state_effective_priority_created_at", table_name="jobs")
    op.create_index(
        "ix_jobs_state_effective_priority_created_at",
        "jobs",
        ["state", sa.text("effective_priority DESC"), "created_at"],
        unique=False,
    )
    with op.batch_alter_table("job_attempts", schema=None) as batch_op:
        batch_op.add_column(sa.Column("adapter_id", sa.String(length=26), nullable=True))
        batch_op.add_column(sa.Column("subject_canonical_id", sa.String(), nullable=True))
        batch_op.add_column(sa.Column("adapter_data_classification", sa.String(), nullable=True))
        batch_op.add_column(sa.Column("effective_data_classification", sa.String(), nullable=True))
        batch_op.create_foreign_key(
            "fk_job_attempts_adapter_id",
            "adapters",
            ["adapter_id"],
            ["id"],
            ondelete="SET NULL",
        )


def downgrade() -> None:
    with op.batch_alter_table("job_attempts", schema=None) as batch_op:
        batch_op.drop_constraint("fk_job_attempts_adapter_id", type_="foreignkey")
        batch_op.drop_column("effective_data_classification")
        batch_op.drop_column("adapter_data_classification")
        batch_op.drop_column("subject_canonical_id")
        batch_op.drop_column("adapter_id")
    with op.batch_alter_table("jobs", schema=None) as batch_op:
        batch_op.drop_constraint("fk_jobs_selected_adapter_id", type_="foreignkey")
        batch_op.drop_column("selected_subject_canonical_id")
        batch_op.drop_column("selected_adapter_id")
    op.drop_index("ix_jobs_state_effective_priority_created_at", table_name="jobs")
    op.create_index(
        "ix_jobs_state_effective_priority_created_at",
        "jobs",
        ["state", sa.text("effective_priority DESC"), "created_at"],
        unique=False,
    )
