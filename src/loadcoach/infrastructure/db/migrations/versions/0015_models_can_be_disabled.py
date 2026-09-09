"""an operator can disable a discovered model

Revision ID: 0015
Revises: 0014
Create Date: 2026-09-09 00:00:00.000000

ADR-0118: a registry row now carries the operator's decision about whether the model may be used
at all, separately from `available`, which is the provider's report about whether it can be.

**Why a column rather than a denylist in the configuration file.** The thing being excluded is a
discovered row — identified by a canonical id nobody types by hand — and the exclusion is made by
clicking on the row it applies to. A list of `provider/name@sha256:…` strings in `config.toml`
would be configuration nobody maintains.

**Default true, and no backfill.** Every existing row was usable before this revision and stays
usable after it. A server default is set so a row inserted by an older code path against a
migrated database still satisfies `NOT NULL`; batch mode for the SQLite reason revisions 0003,
0007 and 0008 record.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None


def upgrade() -> None:
    with op.batch_alter_table("models", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true())
        )


def downgrade() -> None:
    with op.batch_alter_table("models", schema=None) as batch_op:
        batch_op.drop_column("enabled")
