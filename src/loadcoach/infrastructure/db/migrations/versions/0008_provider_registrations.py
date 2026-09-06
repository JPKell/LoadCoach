"""provider_name and is_remote on models

Revision ID: 0008
Revises: 0007
Create Date: 2026-09-05 00:00:00.000000

LoadCoach 1.1 registers providers by name and kind (ADR-0055), so a discovered model now records
*which registration* served it and whether that registration is remote.

**Why two columns rather than a foreign key to a `providers` table.** A registration is
configuration, not data: it exists while the file says so and vanishes when an operator edits the
file, and a foreign key into a table rebuilt from configuration on every startup would either
orphan rows or resurrect deleted registrations. The name is recorded as it was seen, which is the
same treatment `jobs.source` gets for the same reason.

**`is_remote` is denormalized here on purpose.** Routing evaluates `allow_remote_providers` and the
cost factor per candidate, and reading them from the model row means a decision uses the egress
class that was recorded rather than one re-derived from whatever the configuration says at the
moment the decision is explained.

**Defaults, not a backfill.** `provider_name = ''` on an existing row is honest: those models were
discovered before registrations had names, and `''` reads as "not recorded" everywhere it
surfaces. The next discovery pass writes the real name. `is_remote = false` is equally honest —
LoadCoach 1.0 could not register a remote provider at all, so every existing row is local by
construction, and this is the one case where a default is a fact rather than a guess.

Server defaults are set so that a row inserted by an older code path against a migrated database
still satisfies `NOT NULL`; batch mode for the SQLite reason revisions 0003 and 0007 record.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None


def upgrade() -> None:
    with op.batch_alter_table("models", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("provider_name", sa.String(), nullable=False, server_default="")
        )
        batch_op.add_column(
            sa.Column("is_remote", sa.Boolean(), nullable=False, server_default=sa.false())
        )


def downgrade() -> None:
    with op.batch_alter_table("models", schema=None) as batch_op:
        batch_op.drop_column("is_remote")
        batch_op.drop_column("provider_name")
