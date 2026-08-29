"""loadcoach.services.database — engine construction, startup migration and status.

Route handlers and CLI command bodies never call :func:`weightsdb.create_engine_for` directly
(CLI standards §1 / coding standards §5); they call a function here, which is what makes
``loadcoach health --json`` and ``GET /api/v1/health`` report identical database status by
construction. Not in the Phase 1 file list verbatim, but required by it: both the CLI's ``db``
subgroup and the health service need one shared place that turns
:class:`~loadcoach.config.Settings` into a live database connection, and that place is
``services/`` per the application's own layering (``web``/``cli`` never talk to infrastructure
directly).
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType

from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session, sessionmaker
from weightsdb import (
    DatabaseError,
    DatabaseUnavailable,
    MigrationOutcome,
    MigrationRequired,
    MigrationRunner,
    SchemaAhead,
    create_engine_for,
    database_size_bytes,
    integrity_check,
    redact_url,
    session_factory,
    session_scope,
    transaction,
)
from weightsdb import (
    backup as weightsdb_backup,
)
from weightsdb import (
    restore as weightsdb_restore,
)
from weightsdb.backup import BackupResult, RestoreResult

from loadcoach.infrastructure.db.models import (
    ApiToken,
    Model,
    ModelCapability,
    RuntimeProfile,
    TaskProfile,
)
from loadcoach.infrastructure.db.models import Setting as SettingModel
from loadcoach.services.health import HealthComponent

__all__ = [
    "MIGRATIONS_LOCATION",
    "Database",
    "DatabaseStatus",
    "backup_database",
    "build_engine",
    "database_health_component",
    "ensure_ready",
    "get_status",
    "migration_runner",
    "restore_database",
    "upgrade",
]

MIGRATIONS_LOCATION = str(
    Path(__file__).resolve().parent.parent / "infrastructure" / "db" / "migrations"
)

_APPLICATION_NAME = "loadcoach"

_ROW_COUNT_MODELS = (
    Model,
    ModelCapability,
    RuntimeProfile,
    TaskProfile,
    SettingModel,
    ApiToken,
)


def build_engine(
    settings_storage_database_url: str, *, statement_timeout_ms: int | None = None
) -> Engine:
    """Build the engine for the configured database URL.

    Args:
        settings_storage_database_url: ``settings.storage.database_url`` — always non-``None`` by
            the time :class:`~loadcoach.config.Settings` has validated.
        statement_timeout_ms: ``settings.storage.statement_timeout_ms``; PostgreSQL only.
    """
    return create_engine_for(
        settings_storage_database_url,
        statement_timeout_ms=statement_timeout_ms,
        application_name=_APPLICATION_NAME,
    )


class Database:
    """The application's live connection to its database: one engine, for as long as it serves.

    Owned by the caller, not by the functions that use it — the web application creates one in its
    lifespan and disposes it at shutdown; a CLI command creates one, runs, and closes it on the way
    out. Every service function below takes a handle rather than building an engine from a URL.
    """

    __slots__ = ("_engine", "_sessions")

    def __init__(self, engine: Engine) -> None:
        """Wrap an existing engine. Prefer :meth:`from_url` unless you built the engine yourself."""
        self._engine = engine
        self._sessions = session_factory(engine)

    @classmethod
    def from_url(cls, database_url: str, *, statement_timeout_ms: int | None = None) -> Database:
        """Build a handle for ``database_url``. Opens no connection until first use."""
        return cls(build_engine(database_url, statement_timeout_ms=statement_timeout_ms))

    @property
    def engine(self) -> Engine:
        """The underlying engine, for the file-level operations that need one directly."""
        return self._engine

    @property
    def sessions(self) -> sessionmaker[Session]:
        """The session factory bound to this handle's engine."""
        return self._sessions

    @contextmanager
    def write(self) -> Iterator[Session]:
        """One read-write unit of work, committed on success and rolled back on any exception."""
        with session_scope(self._sessions) as session:
            yield session

    @contextmanager
    def read(self) -> Iterator[Session]:
        """One read-only unit of work.

        Enforced, not merely declared: a write attempted inside this scope is refused by SQLite
        rather than silently taken (:func:`weightsdb.transaction`).
        """
        with session_scope(self._sessions) as session, transaction(session, immediate=False):
            yield session

    def close(self) -> None:
        """Dispose the pool. The handle must not be used afterwards."""
        self._engine.dispose()

    def __enter__(self) -> Database:
        """Support ``with Database.from_url(...) as db:`` for one-shot callers like the CLI."""
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Always dispose the pool, whether the body succeeded or raised."""
        self.close()


def migration_runner(engine: Engine, *, backup_retention: int = 5) -> MigrationRunner:
    """Build the :class:`~weightsdb.MigrationRunner` for ``engine``.

    Always points at this application's own migration scripts
    (``loadcoach/infrastructure/db/migrations``) — every application in the suite owns its own
    linear history (database standards §1).
    """
    return MigrationRunner(
        engine, script_location=MIGRATIONS_LOCATION, backup_retention=backup_retention
    )


def ensure_ready(
    database: Database, *, auto_migrate: bool, backup_retention: int = 5
) -> MigrationOutcome | None:
    """Apply the startup revision check (database standards §5.1).

    ```text
    current == head              -> start
    current is None               -> new database: migrate to head
    current < head, auto_migrate  -> back up -> upgrade -> start
    current < head, not auto_migrate -> refuse with MigrationRequired
    current unknown to this build -> refuse with SchemaAhead (written by a newer version)
    ```

    Args:
        database: The application's database handle.
        auto_migrate: ``settings.storage.auto_migrate``.
        backup_retention: ``settings.storage.backup_retention``.

    Returns:
        The :class:`~weightsdb.MigrationOutcome` if a migration ran, else ``None``.

    Raises:
        MigrationRequired: The database is behind head and ``auto_migrate`` is ``False``.
        SchemaAhead: The database's current revision is not one this build's migrations produce.
        DatabaseUnavailable: The database could not be reached at all.
    """
    try:
        runner = migration_runner(database.engine, backup_retention=backup_retention)
        current = runner.current()
        heads = runner.heads()
    except DatabaseError:
        raise
    except Exception as exc:  # noqa: BLE001 — translated into the suite's own error type below
        raise DatabaseUnavailable(
            f"Could not open the database to check its migration state: {redact_url(str(exc))}",
        ) from exc

    if not heads:
        raise DatabaseError(
            f"No migrations are registered under {MIGRATIONS_LOCATION}; the migration history is "
            "missing or empty.",
        )
    head = heads[0]

    if current == head:
        return None
    if current is not None and current not in runner.known_revisions():
        raise SchemaAhead(
            f"The database is at revision {current!r}, which this build's migrations do not "
            f"produce (known head: {head!r}). It was likely written by a newer application "
            "version.",
            details={"current": current, "head": head},
        )
    if current is not None and not auto_migrate:
        raise MigrationRequired(
            f"The database is at revision {current!r}; head is {head!r}. Run "
            "`loadcoach db upgrade` to migrate.",
            details={"current": current, "head": head, "command": "loadcoach db upgrade"},
        )
    return runner.upgrade(backup=current is not None)


def upgrade(
    database: Database, *, revision: str = "head", backup_retention: int = 5
) -> MigrationOutcome:
    """Run ``loadcoach db upgrade``: migrate to ``revision``, taking a backup first.

    Idempotent — calling this when already at ``revision`` is a documented no-op (CLI standards
    §11).
    """
    runner = migration_runner(database.engine, backup_retention=backup_retention)
    return runner.upgrade(revision, backup=runner.current() is not None)


def backup_database(database: Database, *, output: Path | None, keep: int) -> BackupResult:
    """Run ``loadcoach db backup``: take a consistent backup, rotating automatic ones.

    Args:
        database: The application's database handle.
        output: An operator-chosen destination, never rotated. ``None`` chooses an automatic,
            timestamped path under the database's own directory and rotates it against ``keep``.
        keep: ``settings.storage.backup_retention``; ignored when ``output`` is given.
    """
    from datetime import UTC, datetime

    engine = database.engine
    if output is not None:
        return weightsdb_backup(engine, output)
    from weightsdb.backup import sqlite_path

    source = sqlite_path(engine)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    destination = source.parent / "backups" / f"manual-{stamp}{source.suffix}"
    return weightsdb_backup(engine, destination, keep=keep, prefix="manual-")


def restore_database(database: Database, *, source: Path, confirm: bool) -> RestoreResult:
    """Run ``loadcoach db restore``: restore from ``source``, overwriting the current database."""
    return weightsdb_restore(database.engine, source, confirm=confirm)


@dataclass(frozen=True, slots=True)
class DatabaseStatus:
    """The ``loadcoach db status`` / health-component snapshot.

    Attributes:
        dialect: ``"sqlite"`` or ``"postgresql"``.
        current_revision: The database's current Alembic revision, or ``None`` for a fresh,
            unmigrated database.
        head_revision: The revision this build's migrations produce.
        is_at_head: Whether ``current_revision == head_revision``.
        table_row_counts: Row count per table this application owns.
        size_bytes: How much disk the database occupies.
        integrity_ok: Whether the integrity check passed.
        integrity_detail: The backend's own integrity report.
    """

    dialect: str
    current_revision: str | None
    head_revision: str
    is_at_head: bool
    table_row_counts: dict[str, int]
    size_bytes: int
    integrity_ok: bool
    integrity_detail: str


def get_status(database: Database) -> DatabaseStatus:
    """Build the full ``loadcoach db status`` report.

    Raises:
        DatabaseUnavailable: The database could not be reached.
    """
    engine = database.engine
    try:
        runner = migration_runner(engine)
        current = runner.current()
        heads = runner.heads()
        head = heads[0] if heads else ""
        row_counts: dict[str, int] = {}
        if current is not None:
            with database.read() as session:
                for model in _ROW_COUNT_MODELS:
                    count = session.execute(select(func.count()).select_from(model)).scalar_one()
                    row_counts[model.__tablename__] = count
        integrity = integrity_check(engine)
        size_bytes = database_size_bytes(engine)
    except DatabaseError:
        raise
    except Exception as exc:  # noqa: BLE001 — translated into the suite's own error type below
        raise DatabaseUnavailable(f"Could not open the database: {redact_url(str(exc))}") from exc

    return DatabaseStatus(
        dialect=engine.dialect.name,
        current_revision=current,
        head_revision=head,
        is_at_head=current == head,
        table_row_counts=row_counts,
        size_bytes=size_bytes,
        integrity_ok=integrity.ok,
        integrity_detail=integrity.detail,
    )


def database_health_component(database: Database) -> HealthComponent:
    """Build the ``database`` :class:`~loadcoach.services.health.HealthComponent`.

    Never raises: a health check that itself crashes takes the whole health endpoint down with it.
    """
    try:
        status = get_status(database)
    except DatabaseUnavailable as exc:
        return HealthComponent(name="database", status="unavailable", detail=exc.message)
    except DatabaseError as exc:
        return HealthComponent(name="database", status="degraded", detail=exc.message)

    if not status.is_at_head:
        return HealthComponent(
            name="database",
            status="degraded",
            detail=(
                f"pending migration: at {status.current_revision!r}, "
                f"head is {status.head_revision!r}"
            ),
        )
    if not status.integrity_ok:
        return HealthComponent(
            name="database",
            status="degraded",
            detail=f"integrity check failed: {status.integrity_detail}",
        )
    return HealthComponent(name="database", status="ok", detail=f"{status.dialect} at head")
