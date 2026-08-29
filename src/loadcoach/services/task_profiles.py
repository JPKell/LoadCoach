"""loadcoach.services.task_profiles — load, validate and import the shipped task profiles.

A malformed profile refuses startup (dev-plan P2 acceptance criterion 3) — this module is called
from :mod:`loadcoach.bootstrap`, before the server accepts a request, for exactly that reason.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from weightsdb import upsert

from loadcoach.domain.task_profile import TaskProfile, TaskProfileInvalid, load_task_profiles
from loadcoach.infrastructure.db.models import TaskProfile as TaskProfileModel

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from loadcoach.services.database import Database

__all__ = [
    "DEFAULT_SCHEMAS_DIR",
    "DEFAULT_TASK_PROFILES_PATH",
    "TaskProfileInvalid",
    "import_task_profiles",
    "list_stored_task_profiles",
    "read_task_profiles_file",
]

DEFAULT_TASK_PROFILES_PATH = (
    Path(__file__).resolve().parent.parent / "config" / "task_profiles.toml"
)
DEFAULT_SCHEMAS_DIR = Path(__file__).resolve().parent.parent / "config" / "schemas"


def read_task_profiles_file(
    path: Path = DEFAULT_TASK_PROFILES_PATH, *, schemas_dir: Path = DEFAULT_SCHEMAS_DIR
) -> tuple[TaskProfile, ...]:
    """Read and validate every profile in ``path``.

    Args:
        path: The ``task_profiles.toml`` file. Defaults to the shipped configuration.
        schemas_dir: Directory ``execution.json_schema_ref`` paths resolve against.

    Returns:
        Every profile, validated.

    Raises:
        TaskProfileInvalid: A profile fails validation — see
            :func:`loadcoach.domain.task_profile.load_task_profiles`.
    """
    with path.open("rb") as handle:
        raw = tomllib.load(handle)
    table = raw.get("task_profiles", {})
    return load_task_profiles(table, file=path, schemas_dir=schemas_dir)


def _import_one(session: Session, profile: TaskProfile, *, now: datetime) -> None:
    upsert(
        session,
        TaskProfileModel,
        {
            "profile_id": profile.profile_id,
            "version": profile.version,
            "description": profile.description,
            "weights_json": profile.weights,
            "constraints_json": profile.constraints.model_dump(mode="json"),
            "execution_json": profile.execution.model_dump(mode="json"),
            "validation_json": profile.validation.model_dump(mode="json"),
            "enabled": profile.enabled,
            "created_at": now,
            "updated_at": now,
        },
        index_elements=["profile_id", "version"],
        no_update=frozenset({"created_at"}),
    )


def import_task_profiles(
    database: Database, profiles: tuple[TaskProfile, ...], *, now: datetime
) -> int:
    """Upsert every validated profile into the ``task_profiles`` table.

    Idempotent: re-importing the same ``(profile_id, version)`` updates the row in place rather
    than duplicating it, and never moves ``created_at``.

    Args:
        database: The application's database handle.
        profiles: Already-validated profiles, from :func:`read_task_profiles_file`.
        now: The instant to record; injected for deterministic tests.

    Returns:
        How many profiles were imported.
    """
    with database.write() as session:
        for profile in profiles:
            _import_one(session, profile, now=now)
    return len(profiles)


@dataclass(frozen=True, slots=True)
class StoredTaskProfile:
    """A row from the ``task_profiles`` table, as read back for the API and CLI."""

    profile_id: str
    version: str
    description: str
    weights: dict[str, float]
    constraints: dict[str, object]
    execution: dict[str, object]
    validation: dict[str, object]
    enabled: bool
    updated_at: datetime


def list_stored_task_profiles(database: Database) -> tuple[StoredTaskProfile, ...]:
    """Return every task profile stored in the database, newest-updated first."""
    with database.read() as session:
        rows = session.query(TaskProfileModel).order_by(TaskProfileModel.updated_at.desc()).all()
        return tuple(
            StoredTaskProfile(
                profile_id=row.profile_id,
                version=row.version,
                description=row.description or "",
                # PortableJSON columns are typed Mapped[object]; this application only ever
                # writes a dict into them (see _import_one), so the cast reflects a stored
                # invariant rather than an assumption about untrusted input.
                weights=cast("dict[str, float]", row.weights_json or {}),
                constraints=cast("dict[str, Any]", row.constraints_json or {}),
                execution=cast("dict[str, Any]", row.execution_json or {}),
                validation=cast("dict[str, Any]", row.validation_json or {}),
                enabled=row.enabled,
                updated_at=row.updated_at,
            )
            for row in rows
        )
