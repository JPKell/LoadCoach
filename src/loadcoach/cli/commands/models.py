"""loadcoach.cli.commands.models — list, show, refresh.

Not in the Phase 2 file list verbatim, but required by its Work item ("CLI equivalents" of
``GET /models``). Only ``typer`` and ``json`` load at module level, per the same startup-performance
discipline as every other command module.
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

app = typer.Typer(help="Model discovery and inspection.")


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


@app.command("list")
def list_models(
    config: Annotated[
        str | None, typer.Option("--config", help="Path to a config.toml file.")
    ] = None,
    json_output: Annotated[
        bool, typer.Option("--json", help="Print JSON instead of a table.")
    ] = False,
) -> None:
    """List every known model, available or not. Mode: local.

    Example:
        loadcoach models list --json
    """
    from loadcoach.services.models import list_registry

    with _open_database(config) as database:
        entries = list_registry(database)

    if json_output:
        typer.echo(
            json.dumps(
                [
                    {
                        "canonical_id": entry.canonical_id,
                        "available": entry.available,
                        "unavailable_reason": entry.unavailable_reason,
                        "declared_capabilities": entry.declared_capabilities,
                    }
                    for entry in entries
                ]
            )
        )
        return
    if not entries:
        typer.echo("No models discovered yet. Run `loadcoach models refresh`.")
        return
    for entry in entries:
        status = "available" if entry.available else f"unavailable ({entry.unavailable_reason})"
        capabilities = ", ".join(sorted(entry.declared_capabilities)) or "none declared"
        typer.echo(f"{entry.canonical_id:<60} {status:<30} {capabilities}")


@app.command("show")
def show_model(
    canonical_id: Annotated[str, typer.Argument(help="The model's canonical ID.")],
    config: Annotated[
        str | None, typer.Option("--config", help="Path to a config.toml file.")
    ] = None,
) -> None:
    """Show one model's full record. Mode: local. Exit 5 if not found.

    Example:
        loadcoach models show "ollama/qwen3.5:9b@sha256:1f3a9c4e2b70"
    """
    from loadcoach.services.models import list_registry

    with _open_database(config) as database:
        entries = list_registry(database)

    matches = [entry for entry in entries if entry.canonical_id == canonical_id]
    if not matches:
        typer.echo(
            f"Error: no model with canonical_id {canonical_id!r} (MODEL_NOT_FOUND)", err=True
        )
        raise typer.Exit(5)
    entry = matches[0]
    typer.echo(
        json.dumps(
            {
                "canonical_id": entry.canonical_id,
                "provider_kind": entry.provider_kind,
                "provider_model_name": entry.provider_model_name,
                "identity_confidence": entry.identity_confidence,
                "family": entry.family,
                "quantization": entry.quantization,
                "max_context": entry.max_context,
                "size_bytes": entry.size_bytes,
                "parameter_count": entry.parameter_count,
                "available": entry.available,
                "unavailable_reason": entry.unavailable_reason,
                "declared_capabilities": entry.declared_capabilities,
                "first_seen_at": entry.first_seen_at.isoformat(),
                "last_seen_at": entry.last_seen_at.isoformat(),
            },
            indent=2,
        )
    )


@app.command("refresh")
def refresh_models(
    config: Annotated[
        str | None, typer.Option("--config", help="Path to a config.toml file.")
    ] = None,
    json_output: Annotated[
        bool, typer.Option("--json", help="Print JSON instead of text.")
    ] = False,
) -> None:
    """Run discovery against the configured provider. Mode: local. Exit 4 if unreachable.

    Example:
        loadcoach models refresh
    """
    from datetime import UTC, datetime

    from modelrack import ProviderError

    from loadcoach.config import load_settings
    from loadcoach.infrastructure.providers.factory import build_provider
    from loadcoach.services.models import discover_models

    loaded = load_settings(config_path=config)
    provider = build_provider(loaded.settings.provider)
    with _open_database(config) as database:
        try:
            outcome = discover_models(database, provider, now=datetime.now(UTC))
        except ProviderError as exc:
            typer.echo(f"Error: {exc} (PROVIDER_UNAVAILABLE)", err=True)
            raise typer.Exit(4) from exc

    if json_output:
        typer.echo(
            json.dumps(
                {
                    "added": outcome.added,
                    "updated": outcome.updated,
                    "unavailable": outcome.unavailable,
                    "total": outcome.total,
                }
            )
        )
        return
    typer.echo(
        f"discovered {outcome.total} model(s): {outcome.added} added, {outcome.updated} updated, "
        f"{outcome.unavailable} now unavailable"
    )
