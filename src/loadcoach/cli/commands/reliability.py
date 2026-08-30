"""loadcoach.cli.commands.reliability — ``loadcoach reliability show`` (spec §7.2).

Reads the same :func:`~loadcoach.services.reliability.reliability_report` the API and the page
render, so the three cannot disagree. Only ``typer`` and ``json`` load at import time (CLI
standards §12).
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING, Annotated

import typer

if TYPE_CHECKING:
    from loadcoach.services.database import Database

__all__ = ["app", "show"]

app = typer.Typer(help="Production reliability per model and task profile.")


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


def _cell(statistic: dict[str, object], fmt: str) -> str:
    value = statistic["value"]
    if value is None:
        return f"— ({statistic['reason']})"
    return fmt.format(value)


@app.command("show")
def show(
    task: Annotated[str | None, typer.Option("--task", help="Only this task profile.")] = None,
    model: Annotated[
        str | None, typer.Option("--model", help="Only this model (canonical ID).")
    ] = None,
    config: Annotated[
        str | None, typer.Option("--config", help="Path to a config.toml file.")
    ] = None,
    json_output: Annotated[
        bool, typer.Option("--json", help="Print the same JSON as GET /reliability.")
    ] = False,
) -> None:
    """Show production reliability per model and task profile. Mode: local."""
    from loadcoach.services.reliability import reliability_report

    with _open(config) as database:
        entries = reliability_report(database, task_profile_id=task, canonical_id=model)
    if json_output:
        typer.echo(json.dumps({"reliability": [entry.as_json() for entry in entries]}))
        return
    if not entries:
        typer.echo("no production evidence yet")
        return
    for entry in entries:
        week = entry.windows["7d"]
        typer.echo(f"{entry.canonical_id} · {entry.task_profile_id}")
        typer.echo(f"  factor {entry.factor.value:.3f} ({entry.factor.reason})")
        answered = _cell(week.success_rate().as_json(), "{:.0%}")
        typer.echo(
            f"  7d: {week.counted} counted · answered {answered}"
            f" · validated {_cell(week.validation_pass_rate().as_json(), '{:.0%}')}"
            f" · accepted {_cell(week.acceptance().as_json(), '{:.0%}')}"
            f" · p50 {_cell(week.p50().as_json(), '{:.0f} ms')}"
            f" · p95 {_cell(week.p95().as_json(), '{:.0f} ms')}"
        )
        typer.echo(f"  trend: {entry.regression.reason}")
        typer.echo(
            f"  breaker: {entry.circuit_state}"
            + (f" — {entry.circuit_reason}" if entry.circuit_reason else "")
        )
