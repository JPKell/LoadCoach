"""loadcoach.cli._backend — how a CLI command resolves configuration and opens its database.

Every local-mode command (CLI standards §6) resolves configuration and opens one database handle
for the life of the command before its own work. Both are here once, and both exit ``3`` on a
configuration error (CLI standards §4) with the same one-line message. The service layer is
imported inside the functions, never at module level, so ``--help`` stays cheap (CLI standards
§12).
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING

import typer

if TYPE_CHECKING:
    from collections.abc import Iterator

    from loadcoach.config import LoadedSettings, Settings
    from loadcoach.services.database import Database

__all__ = ["load_settings_or_exit", "open_database", "open_loaded_database"]


def load_settings_or_exit(config: str | None) -> LoadedSettings:
    """Resolve configuration, or exit ``3`` with the refusal on stderr.

    Args:
        config: The ``--config`` path, or ``None`` for the default search.
    """
    from loadcoach.config import ConfigurationError, load_settings

    try:
        return load_settings(config_path=config)
    except ConfigurationError as exc:
        typer.echo(f"Error: {exc.message} ({exc.code})", err=True)
        raise typer.Exit(3) from exc


@contextmanager
def open_loaded_database(loaded: LoadedSettings) -> Iterator[Database]:
    """Open one database handle for already-resolved configuration, closed on the way out.

    The CLI is one-shot, so it neither needs nor wants the server's application-lifetime engine.

    Yields:
        The open database.
    """
    from loadcoach.services.database import Database

    storage = loaded.settings.storage
    if storage.database_url is None:  # pragma: no cover — StorageSettings always fills this in
        typer.echo("Error: no database_url configured (CONFIGURATION_ERROR)", err=True)
        raise typer.Exit(3)
    with Database.from_url(
        storage.database_url, statement_timeout_ms=storage.statement_timeout_ms
    ) as database:
        yield database


@contextmanager
def open_database(config: str | None) -> Iterator[tuple[Database, Settings]]:
    """Resolve configuration and open one database handle for this command, or exit ``3``.

    Yields:
        The open database and the resolved settings.
    """
    loaded = load_settings_or_exit(config)
    with open_loaded_database(loaded) as database:
        yield database, loaded.settings
