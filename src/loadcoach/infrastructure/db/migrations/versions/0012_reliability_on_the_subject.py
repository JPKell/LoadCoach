"""reliability and the breaker key on the subject, never the base

Revision ID: 0012
Revises: 0011
Create Date: 2026-09-05 00:00:00.000000

Gate G of LoadCoach 1.1 (ADR-0067). `reliability_stats`' uniqueness moves from
`(model_id, task_profile_id, window)` to `(model_id, adapter_key, task_profile_id, window)`, so a
failing `(base, adapterA)` is deprioritized and eventually broken **as that subject** — it never
breaks the bare base and never breaks a sibling adapter.

**Existing rows are base subjects, and the migration says so rather than guessing.** Every row
written before 1.1 was computed from attempts on a bare base, because no adapter could be applied;
`adapter_key = ''` is therefore a fact about them, and their statistics are unchanged — the
migration adds a column and a key, and touches no count.

The empty string is again the sentinel (ADR-0080 rule 5), and `adapter_key` repeats `adapter_id`
on purpose: the foreign key is nullable and would go `NULL` if an adapter row were removed, and a
unique key that moved when that happened would merge two subjects' history.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None


def upgrade() -> None:
    with op.batch_alter_table("reliability_stats", schema=None) as batch_op:
        batch_op.add_column(sa.Column("adapter_id", sa.String(length=26), nullable=True))
        batch_op.add_column(
            sa.Column("adapter_key", sa.String(), nullable=False, server_default="")
        )
        batch_op.create_foreign_key(
            "fk_reliability_stats_adapter_id",
            "adapters",
            ["adapter_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch_op.drop_constraint(
            "uq_reliability_stats_model_id_task_profile_id_window", type_="unique"
        )
        batch_op.create_unique_constraint(
            "uq_reliability_stats_model_id_adapter_key_task_profile_id_window",
            ["model_id", "adapter_key", "task_profile_id", "window"],
        )


def downgrade() -> None:
    with op.batch_alter_table("reliability_stats", schema=None) as batch_op:
        batch_op.drop_constraint(
            "uq_reliability_stats_model_id_adapter_key_task_profile_id_window", type_="unique"
        )
        batch_op.create_unique_constraint(
            "uq_reliability_stats_model_id_task_profile_id_window",
            ["model_id", "task_profile_id", "window"],
        )
        batch_op.drop_constraint("fk_reliability_stats_adapter_id", type_="foreignkey")
        batch_op.drop_column("adapter_key")
        batch_op.drop_column("adapter_id")
