"""loadcoach.cli.commands.config — show, validate, init, path, schema.

Only ``typer`` and ``json`` load at module level; ``loadcoach.config`` (which imports pydantic) is
imported lazily inside each command body, per the same startup-performance discipline as
:mod:`loadcoach.cli.commands.system`.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer

__all__ = ["app"]

app = typer.Typer(help="Configuration inspection and management.")


def _looks_secret(field_name: str) -> bool:
    lowered = field_name.lower()
    return any(marker in lowered for marker in ("token", "key", "secret", "password"))


@app.command("show")
def show(
    config: Annotated[
        str | None, typer.Option("--config", help="Path to a config.toml file.")
    ] = None,
    json_output: Annotated[
        bool, typer.Option("--json", help="Print JSON instead of a table.")
    ] = False,
) -> None:
    """Print the effective configuration, with the source of every value.

    A runtime-changeable key whose stored row is in force is marked ``(database)`` and shows the
    stored value (configuration standards §7); a stored row the environment shadows is marked as
    shadowed beside the variable that beats it. With no readable database — absent, unmigrated or
    on another host — the output is exactly what it was before there was a settings table, and no
    database file is created.

    Example:
        loadcoach config show --json
    """
    from loadcoach.config import ConfigurationError, load_settings
    from loadcoach.services.settings import database_overlay

    try:
        loaded = load_settings(config_path=config)
    except ConfigurationError as exc:
        typer.echo(f"Error: {exc.message} ({exc.code})", err=True)
        raise typer.Exit(3) from exc

    dumped = loaded.settings.model_dump(mode="json")
    sources = dict(loaded.sources)
    for path, (value, source) in database_overlay(loaded.settings).items():
        sources[path] = source
        section, _, field_name = path.partition(".")
        if section in dumped and field_name in dumped[section]:
            dumped[section][field_name] = value
    if json_output:
        typer.echo(
            json.dumps(
                {
                    "values": dumped,
                    "sources": sources,
                    "config_path": str(loaded.config_path),
                }
            )
        )
        return

    typer.echo(
        f"# {loaded.config_path}{'' if loaded.config_file_used else ' (not found; defaults apply)'}"
    )
    for section, fields in dumped.items():
        for field_name, value in fields.items():
            path = f"{section}.{field_name}"
            source = sources.get(path, "default")
            rendered = "********" if _looks_secret(field_name) else value
            typer.echo(f"{path:<40} {rendered!s:<24} ({source})")


@app.command("validate")
def validate(
    config: Annotated[
        str | None, typer.Option("--config", help="Path to a config.toml file.")
    ] = None,
    file: Annotated[
        str | None,
        typer.Option(
            "--file", help="Validate this candidate file instead of the installation's own."
        ),
    ] = None,
) -> None:
    """Validate configuration without starting the service. Exit 0 or 3.

    ``--file`` runs an arbitrary candidate through the same parse, the same validation and the
    same security refusals as startup (ADR-0127 rule 2) — the check WeightRoomGym runs before it
    writes a settings-form edit back to disk. The installation's own configuration file is never
    read or written for it. Without ``--file`` the verb keeps its present meaning: validate the
    resolved installation config (or ``--config``, if given).

    Example:
        loadcoach config validate --file /tmp/candidate.toml
    """
    from loadcoach.config import ConfigurationError, load_settings

    if file is not None and not Path(file).is_file():
        typer.echo(f"Error: {file} not found (CONFIGURATION_ERROR)", err=True)
        raise typer.Exit(3)
    try:
        load_settings(config_path=file if file is not None else config)
    except ConfigurationError as exc:
        typer.echo(f"Error: {exc.message} ({exc.code})", err=True)
        raise typer.Exit(3) from exc
    typer.echo("Configuration is valid.")


@app.command("path")
def path(
    config: Annotated[
        str | None, typer.Option("--config", help="Path to a config.toml file.")
    ] = None,
) -> None:
    """Print the resolved configuration file location.

    Example:
        loadcoach config path
    """
    from loadcoach.config import resolve_config_path

    typer.echo(str(resolve_config_path(config)))


@app.command("init")
def init(
    config: Annotated[
        str | None, typer.Option("--config", help="Path to write the config file to.")
    ] = None,
    force: Annotated[bool, typer.Option("--force", help="Overwrite an existing file.")] = False,
) -> None:
    """Write a fully commented example configuration file.

    Example:
        loadcoach config init --force
    """
    from loadcoach.config import EXAMPLE_CONFIG_TOML, resolve_config_path

    target = resolve_config_path(config)
    if target.exists() and not force:
        typer.echo(f"Error: {target} already exists (use --force to overwrite).", err=True)
        raise typer.Exit(3)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(EXAMPLE_CONFIG_TOML, encoding="utf-8")
    typer.echo(str(target))


@app.command("reference")
def reference(
    check: Annotated[
        bool,
        typer.Option(
            "--check", help="Exit 1 if docs/configuration.md differs from the generated text."
        ),
    ] = False,
    output: Annotated[
        str | None, typer.Option("--output", help="Write to this file instead of stdout.")
    ] = None,
) -> None:
    """Generate the configuration reference from the settings model (configuration standards §8).

    Mode: local. With ``--check``, compares against ``--output`` (or ``docs/configuration.md``)
    and exits 1 on drift, which is what CI runs.
    """
    from pathlib import Path

    from loadcoach.services.config_reference import render_configuration_reference

    rendered = render_configuration_reference()
    target = Path(output) if output else Path("docs/configuration.md")
    if check:
        committed = target.read_text(encoding="utf-8") if target.is_file() else ""
        if committed != rendered:
            typer.echo(
                f"{target} differs from the generated reference; run "
                "`loadcoach config reference --output docs/configuration.md`",
                err=True,
            )
            raise typer.Exit(1)
        typer.echo(f"{target} matches the settings model")
        return
    if output:
        target.write_text(rendered, encoding="utf-8")
        typer.echo(f"wrote {target}")
    else:
        typer.echo(rendered)


@app.command("schema")
def schema(
    config: Annotated[
        str | None, typer.Option("--config", help="Path to a config.toml file.")
    ] = None,
    json_output: Annotated[
        bool, typer.Option("--json", help="Print the schema document as canonical JSON.")
    ] = False,
) -> None:
    """Print the settings-schema document: the JSON Schema, the runtime-changeable registry, the
    security-relevant keys, every other key, and the source of each (ADR-0127 rule 1).

    Built for WeightRoomGym's settings form, which hardcodes none of LoadCoach's configuration
    surface and instead reads this document. Never prints a secret: the document carries key
    paths and layers, never a value.

    Example:
        loadcoach config schema --json
    """
    from baseaicore import canonical_json

    from loadcoach.config import ConfigurationError
    from loadcoach.services.settings import config_schema_document

    try:
        document = config_schema_document(config)
    except ConfigurationError as exc:
        typer.echo(f"Error: {exc.message} ({exc.code})", err=True)
        raise typer.Exit(3) from exc

    if json_output:
        typer.echo(canonical_json(document))
        return

    typer.echo(
        f"schema_version {document['schema_version']}  application {document['application']}"
        f"  version {document['version']}"
    )
    typer.echo(f"config_path {document['config_path']}")
    typer.echo(f"provider_form {document['provider_form']}")
    typer.echo(f"runtime_changeable ({len(document['runtime_changeable'])}):")
    for entry in document["runtime_changeable"]:
        typer.echo(f"  {entry['key']} ({entry['kind']})")
    typer.echo(f"security_keys ({len(document['security_keys'])}):")
    for key in document["security_keys"]:
        typer.echo(f"  {key}")
    if document["problems"]:
        typer.echo("problems:")
        for problem in document["problems"]:
            typer.echo(f"  {problem}")
