"""loadcoach.cli.main — the Typer root app.

Registers the top-level ``serve``/``health``/``version``/``doctor`` commands and the ``config`` and
``db`` subgroups. Only ``typer`` and the lightweight command modules load at import time; every
heavier dependency stays behind a lazy import inside the command bodies (CLI Standards §12), so
building ``--help`` never imports FastAPI, SQLAlchemy or httpx.
"""

from __future__ import annotations

from typing import Annotated

import typer

from loadcoach.cli.commands import config as config_commands
from loadcoach.cli.commands import db as db_commands
from loadcoach.cli.commands import models as models_commands
from loadcoach.cli.commands import system as system_commands
from loadcoach.cli.commands import tasks as tasks_commands

__all__ = ["app"]

app = typer.Typer(
    name="loadcoach",
    help="Route, queue and execute inference against local models, fully explainably.",
    no_args_is_help=False,
    add_completion=True,
)


def _eager_version(show: bool) -> None:
    if not show:
        return
    system_commands.print_version(json_output=False)
    raise typer.Exit(0)


@app.callback(invoke_without_command=True)
def _root(
    ctx: typer.Context,
    version: Annotated[
        bool,
        typer.Option(
            "--version", is_eager=True, callback=_eager_version, help="Show the version and exit."
        ),
    ] = False,
) -> None:
    """loadcoach — routes, queues and executes inference against local models."""
    if ctx.invoked_subcommand is None:
        ctx.invoke(system_commands.serve)


app.command(name="serve", help="Start the web server (also the default with no subcommand).")(
    system_commands.serve
)
app.command(name="health", help="Report component health.")(system_commands.health)
app.command(name="version", help="Print the application and API versions.")(system_commands.version)
app.command(name="doctor", help="Diagnose a broken installation.")(system_commands.doctor)
app.add_typer(config_commands.app, name="config", help="Configuration inspection and management.")
app.add_typer(db_commands.app, name="db", help="Database migration and maintenance.")
app.add_typer(models_commands.app, name="models", help="Model discovery and inspection.")
app.add_typer(tasks_commands.app, name="tasks", help="Task profile inspection and validation.")
