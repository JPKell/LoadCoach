"""loadcoach.cli.commands.db — upgrade, status, backup, restore.

Every command here is **local** mode (CLI standards §6): it runs the service layer in-process
against the configured database and needs no server running. Only ``typer`` and ``json`` load at
module level, so registering this subgroup never pulls in SQLAlchemy or Alembic
(CLI standards §12).
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import typer

if TYPE_CHECKING:
    from loadcoach.config import StorageSettings
    from loadcoach.services.database import Database

__all__ = ["app"]

app = typer.Typer(help="Database migration and maintenance.")


@contextmanager
def _open_database(config: str | None) -> Iterator[tuple[Database, StorageSettings]]:
    """Resolve configuration and open one database handle for this command, or exit 3.

    One handle per command, closed on the way out — the CLI is one-shot, so it neither needs nor
    wants the server's application-lifetime engine.
    """
    from loadcoach.config import ConfigurationError, load_settings
    from loadcoach.services.database import Database

    try:
        loaded = load_settings(config_path=config)
    except ConfigurationError as exc:
        typer.echo(f"Error: {exc.message} ({exc.code})", err=True)
        raise typer.Exit(3) from exc
    storage = loaded.settings.storage
    if storage.database_url is None:  # pragma: no cover — StorageSettings always fills this in
        typer.echo("Error: no database_url configured (CONFIGURATION_ERROR)", err=True)
        raise typer.Exit(3)
    with Database.from_url(
        storage.database_url, statement_timeout_ms=storage.statement_timeout_ms
    ) as database:
        yield database, storage


def _human_bytes(value: int) -> str:
    """Render a byte count at human scale, exact below 1 KiB."""
    if value < 1024:
        return f"{value} B"
    scaled = float(value)
    for unit in ("KiB", "MiB", "GiB"):
        scaled /= 1024
        if scaled < 1024 or unit == "GiB":
            return f"{scaled:.1f} {unit}"
    raise AssertionError("unreachable: the GiB branch always returns")  # pragma: no cover


def _fail(exc: Exception) -> typer.Exit:
    """Print the standard CLI error line and return the exit-4 (dependency unavailable) signal."""
    code = getattr(exc, "code", "DATABASE_ERROR")
    typer.echo(f"Error: {exc} ({code})", err=True)
    return typer.Exit(4)


@app.command("upgrade")
def upgrade(
    revision: Annotated[str, typer.Argument(help="Target revision.")] = "head",
    config: Annotated[
        str | None, typer.Option("--config", help="Path to a config.toml file.")
    ] = None,
    json_output: Annotated[
        bool, typer.Option("--json", help="Print JSON instead of text.")
    ] = False,
) -> None:
    """Migrate the database to REVISION. Mode: local. A no-op at the target revision (exit 0).

    Example:
        loadcoach db upgrade
    """
    from weightsdb import DatabaseError

    from loadcoach.services.database import upgrade as upgrade_database

    with _open_database(config) as (database, storage):
        try:
            outcome = upgrade_database(
                database, revision=revision, backup_retention=storage.backup_retention
            )
        except DatabaseError as exc:
            raise _fail(exc) from exc

    if json_output:
        typer.echo(
            json.dumps(
                {
                    "from_revision": outcome.from_revision,
                    "to_revision": outcome.to_revision,
                    "backed_up": outcome.backed_up,
                    "backup_path": str(outcome.backup_path) if outcome.backup_path else None,
                    "pruned_backups": [str(path) for path in outcome.pruned_backups],
                    "restore_on_failure_available": outcome.restore_on_failure_available,
                }
            )
        )
    else:
        typer.echo(f"{outcome.from_revision or '(empty)'} -> {outcome.to_revision}")
        if outcome.backed_up:
            typer.echo(f"Backup written to {outcome.backup_path}")
        for path in outcome.pruned_backups:
            typer.echo(f"Rotated out old backup {path}")


@app.command("status")
def status(
    config: Annotated[
        str | None, typer.Option("--config", help="Path to a config.toml file.")
    ] = None,
    json_output: Annotated[
        bool, typer.Option("--json", help="Print JSON instead of a table.")
    ] = False,
) -> None:
    """Report revision, table row counts and integrity status. Mode: local.

    Example:
        loadcoach db status --json
    """
    from weightsdb import DatabaseError

    from loadcoach.services.database import get_status

    with _open_database(config) as (database, _):
        try:
            report = get_status(database)
        except DatabaseError as exc:
            raise _fail(exc) from exc

    if json_output:
        typer.echo(
            json.dumps(
                {
                    "dialect": report.dialect,
                    "current_revision": report.current_revision,
                    "head_revision": report.head_revision,
                    "is_at_head": report.is_at_head,
                    "table_row_counts": report.table_row_counts,
                    "size_bytes": report.size_bytes,
                    "integrity_ok": report.integrity_ok,
                    "integrity_detail": report.integrity_detail,
                }
            )
        )
        return
    typer.echo(f"dialect:       {report.dialect}")
    typer.echo(
        f"revision:      {report.current_revision or '(none)'} (head: {report.head_revision})"
    )
    typer.echo(f"at head:       {report.is_at_head}")
    typer.echo(f"size:          {_human_bytes(report.size_bytes)}")
    typer.echo(f"integrity:     {'ok' if report.integrity_ok else report.integrity_detail}")
    for table, count in sorted(report.table_row_counts.items()):
        typer.echo(f"  {table:<24} {count}")


@app.command("backup")
def backup(
    output: Annotated[
        Path | None, typer.Option("--output", "-o", help="Backup destination.")
    ] = None,
    config: Annotated[
        str | None, typer.Option("--config", help="Path to a config.toml file.")
    ] = None,
    json_output: Annotated[
        bool, typer.Option("--json", help="Print JSON instead of text.")
    ] = False,
) -> None:
    """Take a consistent backup of the database. Mode: local.

    Example:
        loadcoach db backup --output ./loadcoach-before-upgrade.sqlite3
    """
    from weightsdb import DatabaseError

    from loadcoach.services.database import backup_database

    with _open_database(config) as (database, storage):
        try:
            result = backup_database(database, output=output, keep=storage.backup_retention)
        except DatabaseError as exc:
            raise _fail(exc) from exc

    if json_output:
        typer.echo(
            json.dumps(
                {
                    "path": str(result.path),
                    "size_bytes": result.size_bytes,
                    "created_at": result.created_at.isoformat(),
                    "dialect": result.dialect,
                    "pruned": [str(path) for path in result.pruned],
                }
            )
        )
    else:
        typer.echo(str(result.path))
        for path in result.pruned:
            typer.echo(f"Rotated out old backup {path}")


@app.command("restore")
def restore(
    source: Annotated[Path, typer.Argument(help="Backup file to restore from.")],
    yes: Annotated[
        bool, typer.Option("--yes", "-y", help="Confirm the restore; required, non-interactive.")
    ] = False,
    config: Annotated[
        str | None, typer.Option("--config", help="Path to a config.toml file.")
    ] = None,
) -> None:
    """Restore the database from SOURCE, overwriting the current one. Mode: local.

    Requires ``--yes``: there is no interactive prompt (CLI standards §5), and refusing without it
    is exit 2 naming the flag that would have answered it.

    Example:
        loadcoach db restore ./backups/loadcoach-0001-20260829T090000Z.sqlite3 --yes
    """
    from weightsdb import DatabaseError

    from loadcoach.services.database import restore_database

    if not yes:
        typer.echo("Error: --yes is required to confirm this destructive operation.", err=True)
        raise typer.Exit(2)

    with _open_database(config) as (database, _):
        try:
            result = restore_database(database, source=source, confirm=True)
        except DatabaseError as exc:
            raise _fail(exc) from exc
    typer.echo(
        f"Restored {result.path} from {result.source} (revision {result.revision or '(none)'})"
    )
