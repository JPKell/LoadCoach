"""A minimal declarative schema standing in for a second application's own tables.

FreeWeight cannot be installed into LoadCoach's own virtualenv for this test: its own pyproject.toml
pins ``setspec>=0.3,<0.4``, and this repository pins ``setspec>=0.4,<0.5`` — the two cannot coexist
in one venv, and SetSpec P5 (elsewhere in this run) upgrades this venv's own setspec to 0.4.0, which
would break a co-installed FreeWeight outright. This fixture proves the same property WeightsDB's
own test suite proves generically (two independent schemas, zero coupling) — the property that
matters here specifically is Alembic's own bookkeeping (``alembic_version``, per a distinct
``version_table``), which a synthetic ``create_all()`` schema cannot exercise but a second real
migration history, as built here, can.
"""

from __future__ import annotations

from sqlalchemy import String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

__all__ = ["Base", "Machine"]


class Base(DeclarativeBase):
    pass


class Machine(Base):
    __tablename__ = "machines"

    id: Mapped[str] = mapped_column(String(26), primary_key=True)
    machine_fingerprint: Mapped[str] = mapped_column(String, unique=True, nullable=False)
