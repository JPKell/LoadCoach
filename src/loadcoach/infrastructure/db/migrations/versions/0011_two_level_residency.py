"""residency keys on the subject, and a candidate records the residency it paid

Revision ID: 0011
Revises: 0010
Create Date: 2026-09-05 00:00:00.000000

Gate F of LoadCoach 1.1 (ADR-0066): residency is the pair `(resident base process, registered
adapter set)`. What occupies a device is still the **base** — `max_resident_models` counts bases,
not subjects — and the two new columns record which subject last used it.

**The empty string, not `NULL`.** `adapter_key` defaults to `''` for the bare base because the
unique key moves onto it, and SQL treats `NULL`s in a unique index as distinct: a key that admits
duplicates is not a key (ADR-0080 rule 5). It is a deliberate wart, and it lives beside the key it
exists for.

`routing_candidates.residency_detail_json` records which level was applied and both knobs' values,
so a decision can be read afterwards without knowing what the configuration said at the time.

The unique constraint moves, which is a table rebuild on SQLite; `residency` has no children, so
nothing cascades. Foreign keys are off for the run in any case (`env.py`).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None


def upgrade() -> None:
    with op.batch_alter_table("residency", schema=None) as batch_op:
        batch_op.add_column(sa.Column("adapter_id", sa.String(length=26), nullable=True))
        batch_op.add_column(
            sa.Column("adapter_key", sa.String(), nullable=False, server_default="")
        )
        batch_op.create_foreign_key(
            "fk_residency_adapter_id", "adapters", ["adapter_id"], ["id"], ondelete="SET NULL"
        )
        batch_op.drop_constraint("uq_residency_model_id_gpu_index_loaded_at", type_="unique")
        batch_op.create_unique_constraint(
            "uq_residency_model_id_adapter_key_gpu_index_loaded_at",
            ["model_id", "adapter_key", "gpu_index", "loaded_at"],
        )
    op.add_column(
        "routing_candidates", sa.Column("residency_detail_json", sa.JSON(), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("routing_candidates", "residency_detail_json")
    with op.batch_alter_table("residency", schema=None) as batch_op:
        batch_op.drop_constraint(
            "uq_residency_model_id_adapter_key_gpu_index_loaded_at", type_="unique"
        )
        batch_op.create_unique_constraint(
            "uq_residency_model_id_gpu_index_loaded_at", ["model_id", "gpu_index", "loaded_at"]
        )
        batch_op.drop_constraint("fk_residency_adapter_id", type_="foreignkey")
        batch_op.drop_column("adapter_key")
        batch_op.drop_column("adapter_id")
