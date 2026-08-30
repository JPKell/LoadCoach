"""loadcoach.cli.commands.token — ``loadcoach token create|list|revoke`` (spec §7.2, api.md §11).

A token is shown exactly once, at creation; the table holds only its SHA-256. Creating the first
token is what makes a non-loopback bind startable (ADR-0026), and revoking the last one is what
makes it refuse again. Only ``typer`` and ``json`` load at import time (CLI standards §12).
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING, Annotated

import typer

if TYPE_CHECKING:
    from loadcoach.services.database import Database

__all__ = ["app", "create", "list_command", "revoke"]

app = typer.Typer(help="API tokens for a non-loopback bind: create, list, revoke.")

_SCOPES = ("read", "write", "admin")


@contextmanager
def _open(config: str | None) -> Iterator[Database]:
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


@app.command("create")
def create(
    name: Annotated[str, typer.Argument(help="The token's name — also the job/feedback source.")],
    scope: Annotated[
        str, typer.Option("--scope", help="read, write or admin (cumulative, api.md §11).")
    ] = "read",
    expires_days: Annotated[
        int | None, typer.Option("--expires-days", min=1, help="Expiry, in days from now.")
    ] = None,
    config: Annotated[
        str | None, typer.Option("--config", help="Path to a config.toml file.")
    ] = None,
    json_output: Annotated[
        bool, typer.Option("--json", help="Print the record, including the token, as JSON.")
    ] = False,
) -> None:
    """Create a token and print it once. Mode: local. Exit 2 on a bad scope or a taken name."""
    from datetime import UTC, datetime

    from baseaicore import ValidationError

    from loadcoach.services.tokens import create_token

    if scope not in _SCOPES:
        typer.echo(
            f"Error: --scope must be one of {', '.join(_SCOPES)} (VALIDATION_ERROR)", err=True
        )
        raise typer.Exit(2)
    with _open(config) as database:
        try:
            issued = create_token(
                database,
                name=name,
                scope=scope,
                expires_days=expires_days,
                now=datetime.now(UTC),
            )
        except ValidationError as exc:
            typer.echo(f"Error: {exc.message} ({exc.code})", err=True)
            raise typer.Exit(2) from exc
    if json_output:
        typer.echo(json.dumps({**issued.record.as_json(), "token": issued.token}))
        return
    typer.echo(f"token {issued.record.name!r} created with scope {issued.record.scope}")
    typer.echo(f"  {issued.token}")
    typer.echo(
        "  Shown once; only its SHA-256 is stored. Send it as: Authorization: Bearer <token>"
    )


@app.command("list")
def list_command(
    config: Annotated[
        str | None, typer.Option("--config", help="Path to a config.toml file.")
    ] = None,
    json_output: Annotated[bool, typer.Option("--json", help="Print JSON.")] = False,
) -> None:
    """List tokens: name, scope, created, expiry, revoked. Never the token itself. Mode: local."""
    from loadcoach.services.tokens import list_tokens

    with _open(config) as database:
        records = list_tokens(database)
    if json_output:
        typer.echo(json.dumps({"tokens": [record.as_json() for record in records]}))
        return
    if not records:
        typer.echo("no tokens; loopback is open, and a non-loopback bind will refuse to start")
        return
    for record in records:
        state = (
            f"revoked {record.revoked_at.isoformat()}"
            if record.revoked_at is not None
            else f"expires {record.expires_at.isoformat()}"
            if record.expires_at is not None
            else "active"
        )
        typer.echo(
            f"{record.name}\t{record.scope}\t{state}\tcreated {record.created_at.isoformat()}"
        )


@app.command("revoke")
def revoke(
    name: Annotated[str, typer.Argument(help="The token's name.")],
    config: Annotated[
        str | None, typer.Option("--config", help="Path to a config.toml file.")
    ] = None,
) -> None:
    """Revoke a token by name. Mode: local. Exit 5 if there is no such active token."""
    from datetime import UTC, datetime

    from loadcoach.services.tokens import TokenNotFound, revoke_token

    with _open(config) as database:
        try:
            record = revoke_token(database, name=name, now=datetime.now(UTC))
        except TokenNotFound as exc:
            typer.echo(f"Error: {exc.message} ({exc.code})", err=True)
            raise typer.Exit(5) from exc
    typer.echo(f"token {record.name!r} revoked; a request presenting it is now 401")
