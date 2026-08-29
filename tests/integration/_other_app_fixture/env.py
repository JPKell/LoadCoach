"""Alembic environment for the second-application stand-in used by test_two_schemas."""

from __future__ import annotations

import sys
from pathlib import Path

from alembic import context

_THIS_DIR = str(Path(__file__).resolve().parent)
if _THIS_DIR not in sys.path:
    sys.path.insert(0, _THIS_DIR)

import models as _models  # type: ignore[import-not-found] # noqa: F401,E402 — sys.path trick above
from models import Base  # noqa: E402

config = context.config
target_metadata = Base.metadata


def run_migrations_online() -> None:
    """Run migrations against the connection the caller placed in ``config.attributes``."""
    connection = config.attributes["connection"]
    version_table = config.attributes.get("version_table", "alembic_version")
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        version_table=version_table,
        render_as_batch=connection.dialect.name == "sqlite",
    )
    with context.begin_transaction():
        context.run_migrations()


run_migrations_online()
