"""feedback and reliability_stats

Revision ID: 0006
Revises: 0005
Create Date: 2026-08-30 00:00:00.000000

Phase 7's two tables and nothing else. Every column an earlier revision created is left alone:
re-declaring one is drift, and ``check_parity`` reports it as such.

``feedback`` is unique per ``(job_id, source)`` (api.md §6: a second call from the same source
updates the record; two sources' verdicts on one job are both kept). ``validation_detail_json``
is not in data model §2's column list: api.md §6's body carries ``validation.detail``, and a
caller's stated reason for a failed check is the one thing a person reading the feedback wants.

``reliability_stats`` is unique per ``(model_id, task_profile_id, window)``, which is also the
index data model §4 requires the reliability lookup to use. The five ``*_count`` columns beside
the statistics are ADR-0016 rule 6: the sample count that produced each statistic is reported
alongside it, and ``acceptance_rate`` over two verdicts must be distinguishable from one over
two hundred. ``circuit_state`` defaults to ``closed`` because a pair with no verdict yet is not
open.
"""

from __future__ import annotations

import sqlalchemy as sa
import weightsdb
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None


def upgrade() -> None:
    op.create_table(
        "feedback",
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column("job_id", sa.String(length=26), nullable=False),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("accepted", sa.Boolean(), nullable=False),
        sa.Column("quality_score", sa.Float(), nullable=True),
        sa.Column("edited", sa.Boolean(), nullable=False),
        sa.Column("validation_passed", sa.Boolean(), nullable=True),
        sa.Column("validation_detail_json", weightsdb.PortableJSON(), nullable=True),
        sa.Column("notes", sa.String(), nullable=True),
        sa.Column("created_at", weightsdb.UtcDateTime(), nullable=False),
        sa.Column("updated_at", weightsdb.UtcDateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["job_id"], ["jobs.id"], name=op.f("fk_feedback_job_id_jobs"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_feedback")),
        sa.UniqueConstraint("job_id", "source", name=op.f("uq_feedback_job_id_source")),
    )
    op.create_table(
        "reliability_stats",
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column("model_id", sa.String(length=26), nullable=False),
        sa.Column("task_profile_id", sa.String(), nullable=False),
        sa.Column("window", sa.String(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("successes", sa.Integer(), nullable=False),
        sa.Column("validation_passes", sa.Integer(), nullable=False),
        sa.Column("errors", sa.Integer(), nullable=False),
        sa.Column("timeouts", sa.Integer(), nullable=False),
        sa.Column("cancellations", sa.Integer(), nullable=False),
        sa.Column("latency_count", sa.Integer(), nullable=False),
        sa.Column("p50_latency_ms", sa.Integer(), nullable=True),
        sa.Column("p95_latency_ms", sa.Integer(), nullable=True),
        sa.Column("output_token_count", sa.Integer(), nullable=False),
        sa.Column("mean_output_tokens", sa.Float(), nullable=True),
        sa.Column("tokens_per_second_count", sa.Integer(), nullable=False),
        sa.Column("mean_tokens_per_second", sa.Float(), nullable=True),
        sa.Column("feedback_count", sa.Integer(), nullable=False),
        sa.Column("acceptance_rate", sa.Float(), nullable=True),
        sa.Column("quality_count", sa.Integer(), nullable=False),
        sa.Column("mean_quality", sa.Float(), nullable=True),
        sa.Column("circuit_state", sa.String(), nullable=False),
        sa.Column("circuit_opened_at", weightsdb.UtcDateTime(), nullable=True),
        sa.Column("circuit_reason", sa.String(), nullable=True),
        sa.Column("updated_at", weightsdb.UtcDateTime(), nullable=False),
        sa.CheckConstraint(
            "window IN ('7d', '30d', 'all')", name=op.f("ck_reliability_stats_window")
        ),
        sa.ForeignKeyConstraint(
            ["model_id"],
            ["models.id"],
            name=op.f("fk_reliability_stats_model_id_models"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_reliability_stats")),
        sa.UniqueConstraint(
            "model_id",
            "task_profile_id",
            "window",
            name=op.f("uq_reliability_stats_model_id_task_profile_id_window"),
        ),
    )


def downgrade() -> None:
    op.drop_table("reliability_stats")
    op.drop_table("feedback")
