"""the evidence uniqueness key carries the adapter

Revision ID: 0013
Revises: 0012
Create Date: 2026-09-06 00:00:00.000000

Gate B of LoadCoach 1.1's LA3 half (ADR-0085, ADR-0086). `capability_evidence`' uniqueness moves
from `(source_id, canonical_id, runtime_profile_hash, machine_fingerprint, capability_id,
policy_version)` to the same key with `adapter_artifact_digest` in it, so a base and every adapter
subject measured on it occupy separate rows. Under the old key they collapsed into one and the
importer's duplicate detector — correctly, under the contract it was given — discarded all but the
first: a real FreeWeight `1.1` bundle of three subjects imported one record and rejected two.

**The column is `NOT NULL` and the bare base is the empty string** (ADR-0086), not `NULL` as
ADR-0085 first wrote it. LoadCoach writes this table through `weightsdb.upsert`, an
`INSERT ... ON CONFLICT (...) DO UPDATE`, and a conflict target containing a `NULL` never fires: a
nullable key column would make every re-import of a bare-base record insert a second row rather
than update the first, silently and for ever. It is the sentinel ADR-0080 rule 5 already put in
`reliability_stats` and `residency`, for this reason.

**Existing rows are bare-base subjects, and this migration says so rather than guessing.** Every
row written before it arrived through a `capability.evidence` `1.0` document, which has no adapter
field at all, so `adapter_artifact_digest = ''` is a fact about them and not a default. No score,
no count and no binding is touched.

The constraint is named explicitly, as the key it replaces already was: the naming convention
would generate an identifier past PostgreSQL's 63-character limit, and a truncated name makes the
model and the database disagree for ever (testing standards §10.1).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None


def upgrade() -> None:
    with op.batch_alter_table("capability_evidence", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "adapter_artifact_digest",
                sa.String(),
                nullable=False,
                server_default="",
            )
        )
        batch_op.drop_constraint("uq_capability_evidence_subject", type_="unique")
        batch_op.create_unique_constraint(
            "uq_capability_evidence_subject",
            [
                "source_id",
                "canonical_id",
                "adapter_artifact_digest",
                "runtime_profile_hash",
                "machine_fingerprint",
                "capability_id",
                "policy_version",
            ],
        )


def downgrade() -> None:
    with op.batch_alter_table("capability_evidence", schema=None) as batch_op:
        batch_op.drop_constraint("uq_capability_evidence_subject", type_="unique")
        batch_op.create_unique_constraint(
            "uq_capability_evidence_subject",
            [
                "source_id",
                "canonical_id",
                "runtime_profile_hash",
                "machine_fingerprint",
                "capability_id",
                "policy_version",
            ],
        )
        batch_op.drop_column("adapter_artifact_digest")
