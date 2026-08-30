"""loadcoach.cli.commands.queue — ``queue status|pause|resume|drain`` (spec §7.2, api.md §8).

Mode: local. ``pause``/``resume``/``drain`` write the durable control flags the running server's
scheduler reads every second, so they reach it across the process boundary and survive a restart.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING, Annotated

import typer

if TYPE_CHECKING:
    from loadcoach.config import Settings
    from loadcoach.services.database import Database

__all__ = ["app"]

app = typer.Typer(help="Queue status and operator controls.")


@contextmanager
def _open(config: str | None) -> Iterator[tuple[Database, Settings]]:
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
        yield database, loaded.settings


@app.command("status")
def status(
    config: Annotated[
        str | None, typer.Option("--config", help="Path to a config.toml file.")
    ] = None,
    json_output: Annotated[
        bool, typer.Option("--json", help="Print the same JSON as GET /api/v1/queue.")
    ] = False,
) -> None:
    """Depth by state and class, oldest age, starvation, residency, flags. Mode: local.

    Executions, dispatch latency and breaker states live in the serving process and are
    reported as null here; ``GET /api/v1/queue`` has them.
    """
    from datetime import UTC, datetime

    from loadcoach.services.status import queue_status

    with _open(config) as (database, settings):
        report = queue_status(database, settings=settings, runtime=None, now=datetime.now(UTC))
    if json_output:
        typer.echo(json.dumps(report))
        return
    typer.echo(f"active {report['active']} of at most {report['max_depth']}")
    typer.echo(f"  by state: {report['depth_by_state'] or 'empty'}")
    typer.echo(f"  by class: {report['depth_by_class'] or 'empty'}")
    oldest = report["oldest_queued_age_seconds"]
    typer.echo(f"  oldest queued: {'—' if oldest is None else f'{oldest:.0f} s'}")
    typer.echo(f"  starving: {report['starving']}")
    typer.echo(f"  completed in the last 5 min: {report['throughput']['completed_last_5m']}")
    typer.echo(f"  paused: {report['flags']['paused']}  draining: {report['flags']['draining']}")
    for row in report["residency"]:
        typer.echo(f"  resident: {row['canonical_id']} on gpu {row['gpu_index']}")


def _set_flag(config: str | None, name: str, value: bool) -> dict[str, bool]:  # noqa: FBT001 — the flag's value
    from datetime import UTC, datetime

    from loadcoach.services.queue import queue_flags, set_queue_flag

    with _open(config) as (database, _settings):
        set_queue_flag(database, name, value, now=datetime.now(UTC))
        if name == "queue.paused" and not value:
            set_queue_flag(database, "queue.draining", False, now=datetime.now(UTC))
        flags = queue_flags(database)
    return {"paused": flags["queue.paused"], "draining": flags["queue.draining"]}


@app.command("pause")
def pause(
    config: Annotated[
        str | None, typer.Option("--config", help="Path to a config.toml file.")
    ] = None,
    json_output: Annotated[bool, typer.Option("--json", help="Print JSON.")] = False,
) -> None:
    """Stop dispatch without dropping jobs. Mode: local."""
    flags = _set_flag(config, "queue.paused", True)
    typer.echo(json.dumps(flags) if json_output else "queue paused")


@app.command("resume")
def resume(
    config: Annotated[
        str | None, typer.Option("--config", help="Path to a config.toml file.")
    ] = None,
    json_output: Annotated[bool, typer.Option("--json", help="Print JSON.")] = False,
) -> None:
    """Resume dispatch (also clears a drain). Mode: local."""
    flags = _set_flag(config, "queue.paused", False)
    typer.echo(json.dumps(flags) if json_output else "queue resumed")


@app.command("drain")
def drain(
    timeout_seconds: Annotated[
        float, typer.Option("--timeout", help="How long to wait for in-flight work.")
    ] = 600.0,
    config: Annotated[
        str | None, typer.Option("--config", help="Path to a config.toml file.")
    ] = None,
    json_output: Annotated[bool, typer.Option("--json", help="Print JSON.")] = False,
) -> None:
    """Finish in-flight work and stop claiming new jobs. Mode: local. Exit 4 on timeout.

    Waits until no job is in a lease-holding state, which is what the database can see of the
    serving process's workers.
    """
    import time
    from datetime import UTC, datetime

    from loadcoach.services.queue import queue_snapshot, set_queue_flag

    with _open(config) as (database, settings):
        set_queue_flag(database, "queue.draining", True, now=datetime.now(UTC))
        deadline = time.monotonic() + timeout_seconds
        while True:
            snapshot = queue_snapshot(
                database,
                now=datetime.now(UTC),
                default_max_wait_seconds=settings.queue.max_wait_seconds,
            )
            in_flight = sum(
                count
                for state, count in snapshot.depth_by_state.items()
                if state not in {"queued", "waiting_resources"}
            )
            if in_flight == 0:
                break
            if time.monotonic() >= deadline:
                typer.echo(
                    json.dumps({"draining": True, "in_flight": in_flight})
                    if json_output
                    else f"timed out with {in_flight} job(s) still in flight",
                    err=not json_output,
                )
                raise typer.Exit(4)
            time.sleep(0.2)
    typer.echo(json.dumps({"draining": True, "in_flight": 0}) if json_output else "queue drained")
