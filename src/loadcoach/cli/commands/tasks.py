"""loadcoach.cli.commands.tasks — list, show, validate.

Not in the Phase 2 file list verbatim, but required by its Work item ("CLI equivalents" of
``GET /task-profiles``).
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING, Annotated

import typer

if TYPE_CHECKING:
    from loadcoach.services.database import Database

__all__ = ["app"]

app = typer.Typer(help="Task profile inspection and validation.")


@contextmanager
def _open_database(config: str | None) -> Iterator[Database]:
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
        yield database


def _ensure_imported(database: Database) -> None:
    """Import the shipped profiles before reading, so a fresh install shows them without
    requiring ``loadcoach serve`` (which is where :func:`~loadcoach.bootstrap.bootstrap`
    otherwise does this) to have run first. Idempotent — an upsert against the same
    ``(profile_id, version)``.
    """
    from datetime import UTC, datetime

    from loadcoach.services.task_profiles import import_task_profiles, read_task_profiles_file

    import_task_profiles(database, read_task_profiles_file(), now=datetime.now(UTC))


@app.command("list")
def list_tasks(
    config: Annotated[
        str | None, typer.Option("--config", help="Path to a config.toml file.")
    ] = None,
    json_output: Annotated[
        bool, typer.Option("--json", help="Print JSON instead of a table.")
    ] = False,
) -> None:
    """List every stored task profile. Mode: local.

    Example:
        loadcoach tasks list
    """
    from loadcoach.services.task_profiles import list_stored_task_profiles

    with _open_database(config) as database:
        _ensure_imported(database)
        profiles = list_stored_task_profiles(database)

    if json_output:
        typer.echo(
            json.dumps(
                [
                    {"profile_id": p.profile_id, "version": p.version, "enabled": p.enabled}
                    for p in profiles
                ]
            )
        )
        return
    for profile in profiles:
        typer.echo(f"{profile.profile_id:<30} {profile.version:<10} {profile.description}")


@app.command("show")
def show_task(
    profile_id: Annotated[str, typer.Argument(help="The task profile's dotted ID.")],
    config: Annotated[
        str | None, typer.Option("--config", help="Path to a config.toml file.")
    ] = None,
) -> None:
    """Show one task profile's full definition. Mode: local. Exit 5 if not found.

    Example:
        loadcoach tasks show code.review
    """
    from loadcoach.services.task_profiles import list_stored_task_profiles

    with _open_database(config) as database:
        _ensure_imported(database)
        profiles = list_stored_task_profiles(database)

    matches = [profile for profile in profiles if profile.profile_id == profile_id]
    if not matches:
        typer.echo(f"Error: no task profile {profile_id!r} (TASK_PROFILE_NOT_FOUND)", err=True)
        raise typer.Exit(5)
    profile = matches[0]
    typer.echo(
        json.dumps(
            {
                "profile_id": profile.profile_id,
                "version": profile.version,
                "description": profile.description,
                "weights": profile.weights,
                "constraints": profile.constraints,
                "execution": profile.execution,
                "validation": profile.validation,
                "enabled": profile.enabled,
            },
            indent=2,
        )
    )


@app.command("validate")
def validate_tasks(
    path: Annotated[
        str | None,
        typer.Option("--file", help="A task_profiles.toml to validate instead of the shipped one."),
    ] = None,
) -> None:
    """Validate task profile definitions without importing them. Mode: local. Exit 0 or 3.

    Example:
        loadcoach tasks validate
    """
    from pathlib import Path

    from loadcoach.services.task_profiles import (
        DEFAULT_SCHEMAS_DIR,
        DEFAULT_TASK_PROFILES_PATH,
        TaskProfileInvalid,
        read_task_profiles_file,
    )

    target = Path(path) if path is not None else DEFAULT_TASK_PROFILES_PATH
    try:
        profiles = read_task_profiles_file(target, schemas_dir=DEFAULT_SCHEMAS_DIR)
    except TaskProfileInvalid as exc:
        typer.echo(f"Error: {exc.message} ({exc.code})", err=True)
        raise typer.Exit(3) from exc
    typer.echo(f"{len(profiles)} task profile(s) valid.")
