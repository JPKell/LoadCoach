"""loadcoach.cli.commands.adapters — ``loadcoach adapters scan|sync|list|show`` (spec §7.2).

``scan`` drafts; a human keeps (ADR-0061 rule 4). ``sync`` registers what they kept. ``list`` and
``show`` report what the directory holds and what each provider makes of it. Only ``typer`` and
``json`` load at import time (CLI standards §12).
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from typing import TYPE_CHECKING, Annotated

import typer

if TYPE_CHECKING:
    from collections.abc import Iterator

    from loadcoach.config import LoadedSettings
    from loadcoach.infrastructure.providers.factory import ProviderRegistration
    from loadcoach.services.database import Database

__all__ = ["app", "list_adapters", "scan", "show", "sync"]

app = typer.Typer(help="The adapter registry: an operator's directory and reviewed manifests.")


def _load(config: str | None) -> LoadedSettings:
    from loadcoach.config import ConfigurationError, load_settings

    try:
        return load_settings(config_path=config)
    except ConfigurationError as exc:
        typer.echo(f"Error: {exc.message} ({exc.code})", err=True)
        raise typer.Exit(3) from exc


@contextmanager
def _open_database(loaded: LoadedSettings) -> Iterator[Database]:
    """Open the configured database for the one command that writes rows."""
    from loadcoach.services.database import Database

    storage = loaded.settings.storage
    if storage.database_url is None:  # pragma: no cover — StorageSettings always fills this in
        typer.echo("Error: no database_url configured (CONFIGURATION_ERROR)", err=True)
        raise typer.Exit(3)
    with Database.from_url(
        storage.database_url, statement_timeout_ms=storage.statement_timeout_ms
    ) as database:
        yield database


def _registrations(loaded: LoadedSettings) -> tuple[ProviderRegistration, ...]:
    """Build the providers so their adapter state can be reported; none on a failure.

    A one-shot command must still answer about the directory when a provider is unreachable or
    misconfigured, so a construction failure degrades to "no provider state" rather than to an
    error: the directory is the registry, and the providers only say what they currently hold.
    """
    from loadcoach.infrastructure.providers.factory import build_registrations

    try:
        return build_registrations(loaded.settings)
    except Exception:  # noqa: BLE001 — an unbuildable provider must not hide the directory
        return ()


@app.command("scan")
def scan(
    config: Annotated[str | None, typer.Option("--config", help="Path to config.toml.")] = None,
    json_output: Annotated[bool, typer.Option("--json", help="Machine-readable output.")] = False,
) -> None:
    """Draft a manifest for every artifact that has none, for a person to review and keep."""
    from loadcoach.domain.authorization import LOCAL
    from loadcoach.services.adapters import AdaptersDisabled, scan_adapters

    loaded = _load(config)
    try:
        outcome = scan_adapters(loaded.settings, principal=LOCAL)
    except AdaptersDisabled as exc:
        typer.echo(f"Error: {exc} (CONFIGURATION_ERROR)", err=True)
        raise typer.Exit(3) from exc
    except FileNotFoundError as exc:
        typer.echo(f"Error: {exc} (CONFIGURATION_ERROR)", err=True)
        raise typer.Exit(3) from exc

    if json_output:
        typer.echo(
            json.dumps(
                {
                    "drafted": [str(path) for path in outcome.drafted],
                    "skipped": [
                        {"artifact": str(path), "reason": reason}
                        for path, reason in outcome.skipped
                    ],
                },
                indent=2,
            )
        )
        return

    for path in outcome.drafted:
        typer.echo(f"drafted {path}")
    for path, reason in outcome.skipped:
        typer.echo(f"skipped {path}: {reason}")
    if outcome.drafted:
        typer.echo(
            f"\n{len(outcome.drafted)} draft(s) written. Review every field, then rename each to "
            "'.manifest.json' to register it. Nothing trusts a draft."
        )
    elif not outcome.skipped:
        typer.echo("no adapter artifacts found.")


@app.command("sync")
def sync(
    config: Annotated[str | None, typer.Option("--config", help="Path to config.toml.")] = None,
    json_output: Annotated[bool, typer.Option("--json", help="Machine-readable output.")] = False,
) -> None:
    """Register the reviewed manifests, and bind any evidence that was waiting for them.

    The directory is the registry and this table is its projection, so nothing here is a decision:
    the same pass runs when the server starts and before every routed decision. It exists as a
    command because the sequence an operator actually performs — import a bundle, review a manifest,
    expect the evidence to attach — otherwise depends on a routing call happening next, which is a
    side effect rather than an instruction.
    """
    from datetime import UTC, datetime

    from loadcoach.services.adapters import sync_adapters
    from loadcoach.services.evidence import evidence_overview

    loaded = _load(config)
    with _open_database(loaded) as database:
        registered = sync_adapters(database, loaded.settings, now=datetime.now(UTC))
        overview = evidence_overview(
            database, configured_url=loaded.settings.evidence.freeweight_url.strip()
        )

    if json_output:
        typer.echo(
            json.dumps(
                {
                    "adapters": registered,
                    "evidence": {
                        "rows": overview.rows,
                        "bound": overview.bound,
                        "unmatched": overview.unmatched,
                        "ambiguous": overview.ambiguous,
                    },
                },
                indent=2,
            )
        )
        return

    if registered == 0:
        typer.echo("no adapters registered ([adapters] directory is empty, or holds no manifest).")
    else:
        typer.echo(f"{registered} adapter(s) registered from the reviewed manifests.")
    if overview.rows:
        typer.echo(
            f"evidence: {overview.bound} bound, {overview.unmatched} unmatched, "
            f"{overview.ambiguous} ambiguous, of {overview.rows} row(s)."
        )


@app.command("list")
def list_adapters(
    config: Annotated[str | None, typer.Option("--config", help="Path to config.toml.")] = None,
    json_output: Annotated[bool, typer.Option("--json", help="Machine-readable output.")] = False,
) -> None:
    """List every adapter the directory holds, with its status and base compatibility."""
    from loadcoach.domain.authorization import LOCAL
    from loadcoach.services.adapters import AdaptersDisabled, adapter_overview

    loaded = _load(config)
    try:
        overview = adapter_overview(loaded.settings, _registrations(loaded), principal=LOCAL)
    except AdaptersDisabled as exc:
        typer.echo(f"Error: {exc} (CONFIGURATION_ERROR)", err=True)
        raise typer.Exit(3) from exc

    if json_output:
        typer.echo(json.dumps(overview.as_json(), indent=2))
        return

    typer.echo(f"{overview.directory}\n")
    if not overview.adapters:
        typer.echo("no reviewed manifests.")
    for view in overview.adapters:
        entry = view.entry
        status = "available" if entry.available else "UNAVAILABLE"
        where = ", ".join(view.registered_on) or "—"
        pending = f", pending restart on {', '.join(view.pending_on)}" if view.pending_on else ""
        typer.echo(
            f"{entry.name:<24} {status:<12} base {entry.base_model_name} "
            f"({entry.base_confidence.value})  class {entry.data_classification.value}  "
            f"registered on {where}{pending}"
        )
        if entry.unavailable_reason:
            typer.echo(f"    {entry.unavailable_reason}")
    for path, problem in overview.invalid:
        typer.echo(f"\nunreadable manifest {path}: {problem}")
    for path in overview.drafts:
        typer.echo(f"\ndraft awaiting review: {path}")
    for path in overview.unmanifested:
        typer.echo(f"\nno manifest yet (run `loadcoach adapters scan`): {path}")


@app.command("show")
def show(
    name: Annotated[str, typer.Argument(help="The adapter's manifest name.")],
    config: Annotated[str | None, typer.Option("--config", help="Path to config.toml.")] = None,
    json_output: Annotated[bool, typer.Option("--json", help="Machine-readable output.")] = False,
) -> None:
    """Show one adapter in full: identity, base, declared capabilities and provider state."""
    from loadcoach.domain.authorization import LOCAL
    from loadcoach.services.adapters import AdapterNotFound, AdaptersDisabled, show_adapter

    loaded = _load(config)
    try:
        view = show_adapter(name, loaded.settings, _registrations(loaded), principal=LOCAL)
    except AdaptersDisabled as exc:
        typer.echo(f"Error: {exc} (CONFIGURATION_ERROR)", err=True)
        raise typer.Exit(3) from exc
    except AdapterNotFound as exc:
        typer.echo(f"Error: {exc} (ADAPTER_NOT_FOUND)", err=True)
        raise typer.Exit(4) from exc

    if json_output:
        typer.echo(json.dumps(view.as_json(), indent=2))
        return

    entry = view.entry
    typer.echo(f"{entry.name}")
    typer.echo(f"  artifact       {entry.artifact_path}")
    typer.echo(f"  identity       {entry.artifact_sha256}")
    if entry.source_sha256:
        typer.echo(f"  source         {entry.source_sha256} (lineage only)")
    typer.echo(
        f"  base           {entry.base_model_name} "
        f"({entry.base_confidence.value}"
        f"{', ' + entry.base_artifact_digest if entry.base_artifact_digest else ''})"
    )
    typer.echo(f"  classification {entry.data_classification.value}")
    typer.echo(f"  capabilities   {', '.join(entry.declared_capabilities) or '—'}")
    typer.echo(f"  manifest       {entry.manifest_path}")
    typer.echo(f"  available      {'yes' if entry.available else 'no'}")
    if entry.unavailable_reason:
        typer.echo(f"                 {entry.unavailable_reason}")
    for registration, note in view.provider_notes:
        typer.echo(f"  on {registration:<12} {note}")
    if entry.notes:
        typer.echo(f"  notes          {entry.notes}")
