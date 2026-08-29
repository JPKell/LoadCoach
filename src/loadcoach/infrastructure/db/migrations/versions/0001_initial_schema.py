"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-08-29 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
import weightsdb
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0001"
down_revision: str | None = None
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None


def upgrade() -> None:
    op.create_table(
        "api_tokens",
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("token_sha256", sa.String(), nullable=False),
        sa.Column("scope", sa.String(), nullable=False),
        sa.Column("created_at", weightsdb.UtcDateTime(), nullable=False),
        sa.Column("last_used_at", weightsdb.UtcDateTime(), nullable=True),
        sa.Column("expires_at", weightsdb.UtcDateTime(), nullable=True),
        sa.Column("revoked_at", weightsdb.UtcDateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_api_tokens")),
        sa.UniqueConstraint("token_sha256", name=op.f("uq_api_tokens_token_sha256")),
    )
    op.create_table(
        "settings",
        sa.Column("key", sa.String(), nullable=False),
        sa.Column("value_json", weightsdb.PortableJSON(), nullable=True),
        sa.Column("updated_at", weightsdb.UtcDateTime(), nullable=False),
        sa.PrimaryKeyConstraint("key", name=op.f("pk_settings")),
    )
    op.create_table(
        "runtime_profiles",
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column("profile_hash", sa.String(), nullable=False),
        sa.Column("context_size", sa.Integer(), nullable=True),
        sa.Column("kv_cache_precision", sa.String(), nullable=True),
        sa.Column("gpu_layers", sa.Integer(), nullable=True),
        sa.Column("flash_attention", sa.Boolean(), nullable=True),
        sa.Column("threads", sa.Integer(), nullable=True),
        sa.Column("batch_size", sa.Integer(), nullable=True),
        sa.Column("keep_alive", sa.String(), nullable=True),
        sa.Column("provider_options_json", weightsdb.PortableJSON(), nullable=True),
        sa.Column("created_at", weightsdb.UtcDateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_runtime_profiles")),
        sa.UniqueConstraint("profile_hash", name=op.f("uq_runtime_profiles_profile_hash")),
    )
    op.create_table(
        "task_profiles",
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column("profile_id", sa.String(), nullable=False),
        sa.Column("version", sa.String(), nullable=False),
        sa.Column("description", sa.String(), nullable=True),
        sa.Column("weights_json", weightsdb.PortableJSON(), nullable=False),
        sa.Column("constraints_json", weightsdb.PortableJSON(), nullable=False),
        sa.Column("execution_json", weightsdb.PortableJSON(), nullable=False),
        sa.Column("validation_json", weightsdb.PortableJSON(), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("created_at", weightsdb.UtcDateTime(), nullable=False),
        sa.Column("updated_at", weightsdb.UtcDateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_task_profiles")),
        sa.UniqueConstraint(
            "profile_id", "version", name=op.f("uq_task_profiles_profile_id_version")
        ),
    )
    op.create_table(
        "models",
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column("provider_kind", sa.String(), nullable=False),
        sa.Column("provider_model_name", sa.String(), nullable=False),
        sa.Column("artifact_digest", sa.String(), nullable=True),
        sa.Column("canonical_id", sa.String(), nullable=False),
        sa.Column("identity_confidence", sa.String(), nullable=False),
        sa.Column("descriptor_json", weightsdb.PortableJSON(), nullable=True),
        sa.Column("declared_capabilities_json", weightsdb.PortableJSON(), nullable=True),
        sa.Column("max_context", sa.Integer(), nullable=True),
        sa.Column("size_bytes", sa.Integer(), nullable=True),
        sa.Column("quantization", sa.String(), nullable=True),
        sa.Column("family", sa.String(), nullable=True),
        sa.Column("parameter_count", sa.Integer(), nullable=True),
        sa.Column("first_seen_at", weightsdb.UtcDateTime(), nullable=False),
        sa.Column("last_seen_at", weightsdb.UtcDateTime(), nullable=False),
        sa.Column("available", sa.Boolean(), nullable=False),
        sa.Column("unavailable_reason", sa.String(), nullable=True),
        sa.CheckConstraint(
            "identity_confidence IN ('digest', 'name_only')",
            name=op.f("ck_models_identity_confidence"),
        ),
        sa.CheckConstraint(
            "provider_kind IN ('ollama', 'openai_compatible', 'llamacpp', 'vllm', 'fake')",
            name=op.f("ck_models_provider_kind"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_models")),
    )
    with op.batch_alter_table("models", schema=None) as batch_op:
        batch_op.create_index("ix_models_canonical_id", ["canonical_id"], unique=False)
        batch_op.create_index(
            "uq_models_identity_triple",
            ["provider_kind", "provider_model_name", "artifact_digest"],
            unique=True,
        )
        batch_op.create_index(
            "uq_models_name_only",
            ["provider_kind", "provider_model_name"],
            unique=True,
            sqlite_where=sa.text("artifact_digest IS NULL"),
            postgresql_where=sa.text("artifact_digest IS NULL"),
        )

    op.create_table(
        "model_capabilities",
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column("model_id", sa.String(length=26), nullable=False),
        sa.Column("capability_id", sa.String(), nullable=False),
        sa.Column("score", sa.Float(), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("updated_at", weightsdb.UtcDateTime(), nullable=False),
        sa.CheckConstraint(
            "source IN ('declared', 'manual', 'prior', 'production')",
            name=op.f("ck_model_capabilities_source"),
        ),
        sa.ForeignKeyConstraint(
            ["model_id"],
            ["models.id"],
            name=op.f("fk_model_capabilities_model_id_models"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_model_capabilities")),
        sa.UniqueConstraint(
            "model_id",
            "capability_id",
            "source",
            name=op.f("uq_model_capabilities_model_id_capability_id_source"),
        ),
    )
    with op.batch_alter_table("model_capabilities", schema=None) as batch_op:
        batch_op.create_index("ix_model_capabilities_model_id", ["model_id"], unique=False)


def downgrade() -> None:
    op.drop_table("model_capabilities")
    with op.batch_alter_table("models", schema=None) as batch_op:
        batch_op.drop_index("uq_models_name_only")
        batch_op.drop_index("uq_models_identity_triple")
        batch_op.drop_index("ix_models_canonical_id")
    op.drop_table("models")
    op.drop_table("task_profiles")
    op.drop_table("runtime_profiles")
    op.drop_table("settings")
    op.drop_table("api_tokens")
