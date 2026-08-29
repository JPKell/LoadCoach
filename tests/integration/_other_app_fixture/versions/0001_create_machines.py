"""create machines

Revision ID: 0001
Revises:
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None


def upgrade() -> None:
    op.create_table(
        "machines",
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column("machine_fingerprint", sa.String(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_machines")),
        sa.UniqueConstraint("machine_fingerprint", name=op.f("uq_machines_machine_fingerprint")),
    )


def downgrade() -> None:
    op.drop_table("machines")
