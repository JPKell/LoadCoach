"""adapter-bearing evidence binds to its subject

Revision ID: 0014
Revises: 0013
Create Date: 2026-09-06 00:00:00.000000

Gate C of LoadCoach 1.1's LA3 half (ADR-0058 §4, ADR-0085). `0013` let a base and its adapter
subjects survive import as separate rows; this migration gives those rows somewhere to bind.
`capability_evidence` gains a nullable `adapter_id` foreign key into `adapters`, and
`bind_identity` replaces the rule that retained every adapter-bearing record `unmatched` with a
real binding: the base by ADR-0022 §4's four rules, the adapter by artifact digest, and `bound`
only when both resolve.

**`adapter_id` is nullable and `adapter_artifact_digest` is not, and that is the point.** The
foreign key is what queries join on; the digest beside it is the identity, and it stays correct for
a record naming an adapter this operator has never held — which is `unmatched`, retained, and bound
by the next directory scan with no re-import (ADR-0022 §4 rule 4), never rejected. `ON DELETE SET
NULL` for the same reason: removing an adapter from the directory must not delete a measurement or
merge two subjects' histories.

**Every existing row is a bare-base subject**, written before `0013` from a `capability.evidence`
`1.0` document that has no adapter field at all, so `adapter_id` stays `NULL` and no backfill is
needed or possible. Nothing is rebound here: `rebind_evidence` runs on the next discovery pass and
is what attaches the surviving adapter-bearing rows.

SQLite foreign-key enforcement is suspended for the duration of a migration run
(ADR-0082), which is why adding this key to a populated table is safe on both dialects.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None


def upgrade() -> None:
    with op.batch_alter_table("capability_evidence", schema=None) as batch_op:
        batch_op.add_column(sa.Column("adapter_id", sa.String(length=26), nullable=True))
        batch_op.create_foreign_key(
            "fk_capability_evidence_adapter_id",
            "adapters",
            ["adapter_id"],
            ["id"],
            ondelete="SET NULL",
        )


def downgrade() -> None:
    with op.batch_alter_table("capability_evidence", schema=None) as batch_op:
        batch_op.drop_constraint("fk_capability_evidence_adapter_id", type_="foreignkey")
        batch_op.drop_column("adapter_id")
