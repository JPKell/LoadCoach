"""loadcoach.cli.commands.models — list, show, refresh.

Not in the Phase 2 file list verbatim, but required by its Work item ("CLI equivalents" of
``GET /models``). Only ``typer`` and ``json`` load at module level, per the same startup-performance
discipline as every other command module.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Annotated

import typer

from loadcoach.cli._backend import open_database

if TYPE_CHECKING:
    pass

__all__ = ["app"]

app = typer.Typer(help="Model discovery and inspection.")


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

    with open_database(config) as (database, _settings):
        entries = list_registry(database)

    if json_output:
        typer.echo(
            json.dumps(
                [
                    {
                        "canonical_id": entry.canonical_id,
                        "provider_name": entry.provider_name,
                        "is_remote": entry.is_remote,
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
    from loadcoach.domain.routing.subject import resolve_runtime_profile, runtime_profile_refusal
    from loadcoach.services.models import list_registry
    from loadcoach.services.routing import runtime_layers

    with open_database(config) as (database, settings):
        entries = list_registry(database)
        defaults, per_model = runtime_layers(settings.runtime)

    matches = [entry for entry in entries if entry.canonical_id == canonical_id]
    if not matches:
        typer.echo(
            f"Error: no model with canonical_id {canonical_id!r} (MODEL_NOT_FOUND)", err=True
        )
        raise typer.Exit(5)
    entry = matches[0]
    # The profile routing would resolve for this model from configuration alone — the two
    # operator levels of ADR-0023's chain — and whether its provider can serve it as stated
    # (ADR-0120). A task profile or a request override may still change it per decision.
    profile = resolve_runtime_profile(defaults=defaults, per_model=per_model.get(canonical_id))
    refusal = runtime_profile_refusal(profile, provider_kind=entry.provider_kind)
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
                "runtime_profile": {
                    "context_size": profile.context_size,
                    "kv_cache_precision": profile.kv_cache_precision,
                    "flash_attention": profile.flash_attention,
                    "keep_alive": profile.keep_alive,
                    "profile_hash": profile.profile_hash,
                },
                "runtime_profile_refusal": (
                    None if refusal is None else {"reason": refusal[0], **refusal[1]}
                ),
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
    from loadcoach.domain.authorization import LOCAL
    from loadcoach.infrastructure.providers.factory import (
        build_registrations,
        disabled_registration_names,
    )
    from loadcoach.services.models import discover_models

    loaded = load_settings(config_path=config)
    registrations = build_registrations(loaded.settings)
    with open_database(config) as (database, _settings):
        try:
            outcome = discover_models(
                database,
                registrations,
                now=datetime.now(UTC),
                disabled_provider_names=disabled_registration_names(loaded.settings),
                principal=LOCAL,
            )
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


@app.command("residency")
def residency(
    config: Annotated[
        str | None, typer.Option("--config", help="Path to a config.toml file.")
    ] = None,
    json_output: Annotated[
        bool, typer.Option("--json", help="Print JSON instead of a table.")
    ] = False,
) -> None:
    """Which models are resident, on which device, and for how long (queue §6). Mode: local.

    Example:
        loadcoach models residency --json
    """
    from loadcoach.services.status import residency_rows

    with open_database(config) as (database, _settings):
        rows = residency_rows(database)
    if json_output:
        typer.echo(json.dumps(rows))
        return
    if not rows:
        typer.echo("nothing resident (or the provider cannot report residency)")
        return
    for row in rows:
        vram = row["vram_bytes"]
        typer.echo(
            f"{row['canonical_id']}  gpu {row['gpu_index']}  "
            f"{'unknown' if vram is None else f'{vram / 1024**3:.1f} GiB'}  "
            f"last used {row['last_used_at']}"
        )
