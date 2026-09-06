"""the adapters table, and the subject on every routing row

Revision ID: 0009
Revises: 0008
Create Date: 2026-09-05 00:00:00.000000

LoadCoach 1.1's execution subject gains an adapter axis (ADR-0058), so the operator's reviewed
adapter directory becomes rows and every routing row names the subject it decided about.

**Why a table at all, when the directory is the truth.** The directory is read and hashed, which
is file I/O no routing decision may do; the table is the projection routing reads, refreshed by
the same pass that discovers models. Identity is the artifact hash (ADR-0061 rule 5), so a rename
moves `artifact_path` and nothing else, and an edited artifact is a different row.

**Why both a foreign key and a string** (ADR-0080). `adapter_id` answers *which adapter*, and it
answers it after a rename. `subject_canonical_id` is what the explanation quotes, written at
decision time and never parsed back, so a stored decision still reads correctly after the
directory has changed underneath it — an explanation is kept for ever and an adapter directory is
not.

**The backfill is a fact, not a guess.** Every routing candidate written before this migration
decided about a bare base, and a bare base's subject string is byte-for-byte its model's canonical
ID (ADR-0058 §3). So existing rows are filled from `models.canonical_id`; a row whose model has
since been deleted keeps the `''` server default, which reads as "not recorded" and never as a
subject.

**This is the first migration to add a foreign key to a table that has children**, and on SQLite
that is a table rebuild: alembic's batch mode copies the rows, drops the old table and renames the
new one. Dropping `routing_decisions` with `foreign_keys=ON` (database standards §2) cascades
through `routing_candidates.decision_id` and would silently delete every stored candidate — the
explainability promise itself. `env.py` therefore enforces foreign keys **off for the duration of
a migration run** on SQLite and restores them after; the assertion that it works is
`test_migration_0009_creates_adapters_and_backfills_the_subject`, which inserts a decision and
its candidate at 0008 and reads both back afterwards.

Batch mode for the SQLite reason revisions 0003, 0007 and 0008 record.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None


def upgrade() -> None:
    op.create_table(
        "adapters",
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("artifact_sha256", sa.String(), nullable=False),
        sa.Column("source_sha256", sa.String(), nullable=True),
        sa.Column("artifact_path", sa.String(), nullable=False),
        sa.Column("manifest_path", sa.String(), nullable=False),
        sa.Column("base_model_name", sa.String(), nullable=False),
        sa.Column("base_artifact_digest", sa.String(), nullable=True),
        sa.Column("base_identity_confidence", sa.String(), nullable=False),
        sa.Column("declared_capabilities_json", sa.JSON(), nullable=True),
        sa.Column("data_classification", sa.String(), nullable=False),
        sa.Column("adapter_format", sa.String(), nullable=False),
        sa.Column("manifest_json", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("available", sa.Boolean(), nullable=False),
        sa.Column("unavailable_reason", sa.String(), nullable=True),
        sa.CheckConstraint(
            "base_identity_confidence IN ('digest', 'name_only')",
            name="base_identity_confidence",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("artifact_sha256"),
    )
    op.create_index("ix_adapters_name", "adapters", ["name"])
    op.create_index("ix_adapters_base_model_name", "adapters", ["base_model_name"])

    with op.batch_alter_table("routing_decisions", schema=None) as batch_op:
        batch_op.add_column(sa.Column("selected_adapter_id", sa.String(length=26), nullable=True))
        batch_op.add_column(sa.Column("selected_subject_canonical_id", sa.String(), nullable=True))
        batch_op.create_foreign_key(
            "fk_routing_decisions_selected_adapter_id",
            "adapters",
            ["selected_adapter_id"],
            ["id"],
            ondelete="SET NULL",
        )

    with op.batch_alter_table("routing_candidates", schema=None) as batch_op:
        batch_op.add_column(sa.Column("adapter_id", sa.String(length=26), nullable=True))
        batch_op.add_column(
            sa.Column("subject_canonical_id", sa.String(), nullable=False, server_default="")
        )
        batch_op.create_foreign_key(
            "fk_routing_candidates_adapter_id",
            "adapters",
            ["adapter_id"],
            ["id"],
            ondelete="SET NULL",
        )

    op.execute(
        sa.text(
            "UPDATE routing_candidates SET subject_canonical_id = COALESCE("
            "(SELECT canonical_id FROM models WHERE models.id = routing_candidates.model_id), '')"
        )
    )
    op.execute(
        sa.text(
            "UPDATE routing_decisions SET selected_subject_canonical_id = "
            "(SELECT canonical_id FROM models WHERE models.id = "
            "routing_decisions.selected_model_id) WHERE selected_model_id IS NOT NULL"
        )
    )


def downgrade() -> None:
    with op.batch_alter_table("routing_candidates", schema=None) as batch_op:
        batch_op.drop_constraint("fk_routing_candidates_adapter_id", type_="foreignkey")
        batch_op.drop_column("subject_canonical_id")
        batch_op.drop_column("adapter_id")
    with op.batch_alter_table("routing_decisions", schema=None) as batch_op:
        batch_op.drop_constraint("fk_routing_decisions_selected_adapter_id", type_="foreignkey")
        batch_op.drop_column("selected_subject_canonical_id")
        batch_op.drop_column("selected_adapter_id")
    op.drop_index("ix_adapters_base_model_name", table_name="adapters")
    op.drop_index("ix_adapters_name", table_name="adapters")
    op.drop_table("adapters")
