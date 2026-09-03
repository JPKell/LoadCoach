"""cache_write_tokens and cache_read_tokens on jobs and job_attempts

Revision ID: 0007
Revises: 0006
Create Date: 2026-09-03 00:00:00.000000

ADR-0070 decision 7: LoadCoach carries all four of ``baseaicore.TokenUsage``'s disjoint token
classes on its rows and on its wire, not only input and output. Without it the harness rebuilds a
``TokenUsage`` whose cache classes are unsupported, and a strict money ceiling trips on every
remote turn (ADR-0069 §"Not decided here").

Four nullable integer columns, two per table, and nothing else. Every column an earlier revision
created is left alone: re-declaring one is drift, and ``check_parity`` reports it as such.

**No backfill, no server default, no data migration.** An existing row genuinely has no value for
these — the executions it records happened before anything read the cache classes — and ``NULL``
is how this schema already says "not reported", which is why ``input_tokens`` and
``thinking_tokens`` are nullable for exactly the same reason. A ``0`` default would be the
fabricated zero ADR-0016 forbids, applied retroactively to thousands of rows at once: the whole
point of ADR-0070 is that ``0`` is a *count* — the provider's protocol could not have billed this
class — and a backfill would assert that about calls nobody measured.

**Batch mode, not a bare ALTER.** SQLite cannot drop a column with a plain ``ALTER TABLE``, so a
downgrade written as ``op.drop_column`` would work on PostgreSQL and fail on SQLite — the default
dialect (ADR-0006) and the one most likely to be
running when somebody actually reaches for a downgrade. ``batch_alter_table`` recreates the table
by copy-and-move on SQLite and emits a plain ``ALTER`` on PostgreSQL, which is the convention
revision 0003 already set for the same reason.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None


def upgrade() -> None:
    with op.batch_alter_table("jobs", schema=None) as batch_op:
        batch_op.add_column(sa.Column("cache_write_tokens", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("cache_read_tokens", sa.Integer(), nullable=True))
    with op.batch_alter_table("job_attempts", schema=None) as batch_op:
        batch_op.add_column(sa.Column("cache_write_tokens", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("cache_read_tokens", sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("job_attempts", schema=None) as batch_op:
        batch_op.drop_column("cache_read_tokens")
        batch_op.drop_column("cache_write_tokens")
    with op.batch_alter_table("jobs", schema=None) as batch_op:
        batch_op.drop_column("cache_read_tokens")
        batch_op.drop_column("cache_write_tokens")
