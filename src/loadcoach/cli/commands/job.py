"""loadcoach.cli.commands.job — ``job submit|list|show|cancel|wait`` (spec §7.2).

Mode: local. Every command opens the database directly and calls the same service functions the
HTTP API calls (CLI standards: no business logic here). A ``submit`` from here is picked up by the
running server's workers at their next poll — the in-process enqueue wake-up cannot cross a
process boundary, so dispatch may take up to the idle poll interval (one second). ``feedback`` is
Phase 7's, with the feedback service.
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

app = typer.Typer(help="Submit, inspect, cancel and wait for queued jobs.")

_TERMINAL = frozenset({"completed", "failed", "cancelled"})


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
        _ensure_profiles_imported(database)
        yield database, loaded.settings


def _ensure_profiles_imported(database: Database) -> None:
    """Import the shipped task profiles before reading, as ``tasks list`` does (LC14).

    A fresh install that ran ``db upgrade`` and then ``job submit`` without ever starting the
    server must still find ``general.chat``; the import is an idempotent upsert.
    """
    from datetime import UTC, datetime

    from loadcoach.services.task_profiles import import_task_profiles, read_task_profiles_file

    import_task_profiles(database, read_task_profiles_file(), now=datetime.now(UTC))


def _print_job(document: dict[str, object], *, json_output: bool) -> None:
    if json_output:
        typer.echo(json.dumps(document))
        return
    model = document["model"]
    error = document["error"]
    typer.echo(f"job {document['job_id']}: {document['state']} ({document['class']})")
    typer.echo(
        f"  task {document['task']['id']} · attempts {document['attempt']}/"  # type: ignore[index]
        f"{document['max_attempts']} · source {document['source']}"
    )
    if isinstance(model, dict) and model.get("canonical_id"):
        typer.echo(f"  model {model['canonical_id']} on gpu {model.get('target_gpu_index')}")
    if isinstance(error, dict):
        typer.echo(f"  error {error['code']}: {error['message']}")
    output = document["output"]
    if isinstance(output, dict) and output.get("text"):
        typer.echo(f"  output: {output['text']}")


def _wait_for(database: Database, job_id: str, *, timeout_seconds: float) -> dict[str, object]:
    import time

    from loadcoach.services.queue import job_document

    deadline = time.monotonic() + timeout_seconds
    while True:
        document = job_document(database, job_id)
        if document["state"] in _TERMINAL or time.monotonic() >= deadline:
            return document
        time.sleep(0.1)


@app.command("submit")
def submit(
    task: Annotated[str, typer.Option("--task", help="The task profile to route for.")],
    prompt: Annotated[str | None, typer.Option("--prompt", help="The prompt text.")] = None,
    prompt_file: Annotated[
        str | None, typer.Option("--prompt-file", help="Read the prompt from a file.")
    ] = None,
    system: Annotated[str | None, typer.Option("--system", help="A system turn.")] = None,
    job_class: Annotated[
        str, typer.Option("--class", help="interactive | normal | background | batch.")
    ] = "normal",
    priority: Annotated[
        int | None, typer.Option("--priority", help="A priority within the class's band.")
    ] = None,
    max_wait_seconds: Annotated[
        int | None, typer.Option("--max-wait-seconds", help="The absolute wait bound.")
    ] = None,
    idempotent: Annotated[
        bool,
        typer.Option(
            "--idempotent/--no-idempotent",
            help="Whether a lost lease may re-run the job (default: yes).",
        ),
    ] = True,
    idempotency_key: Annotated[
        str | None, typer.Option("--idempotency-key", help="Makes a retried submit safe.")
    ] = None,
    source: Annotated[str, typer.Option("--source", help="The calling application.")] = "cli",
    wait: Annotated[
        bool, typer.Option("--wait", help="Block until the job reaches a terminal state.")
    ] = False,
    timeout_seconds: Annotated[
        float, typer.Option("--timeout", help="With --wait: how long to block, in seconds.")
    ] = 600.0,
    config: Annotated[
        str | None, typer.Option("--config", help="Path to a config.toml file.")
    ] = None,
    json_output: Annotated[
        bool, typer.Option("--json", help="Print the job document as JSON.")
    ] = False,
) -> None:
    """Enqueue a job. Mode: local. Exit 0, 1 (failed/cancelled with --wait), 2, 3, 4 or 5.

    Example:
        loadcoach job submit --task code.review --prompt-file review.txt --class background
    """
    from datetime import UTC, datetime
    from pathlib import Path

    from baseaicore import ValidationError

    from loadcoach.domain.priority import JobClass
    from loadcoach.services.job_events import JobEventSink
    from loadcoach.services.queue import JobSubmission, QueueFull, enqueue, job_document
    from loadcoach.services.routing import TaskProfileNotFound

    if (prompt is None) == (prompt_file is None):
        typer.echo("Error: supply exactly one of --prompt or --prompt-file", err=True)
        raise typer.Exit(2)
    text = prompt if prompt is not None else Path(prompt_file or "").read_text(encoding="utf-8")
    try:
        submission = JobSubmission(
            task=task,
            prompt=text,
            system=system,
            job_class=JobClass(job_class),
            priority=priority,
            max_wait_seconds=max_wait_seconds,
            idempotent=idempotent,
            idempotency_key=idempotency_key,
            source=source,
        )
    except ValueError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(2) from exc
    with _open(config) as (database, settings):
        try:
            outcome = enqueue(
                database,
                submission,
                now=datetime.now(UTC),
                queue_settings=settings.queue,
                execution_settings=settings.execution,
                sink=JobEventSink(),
            )
        except TaskProfileNotFound as exc:
            typer.echo(f"Error: {exc.message} ({exc.code})", err=True)
            raise typer.Exit(5) from exc
        except ValidationError as exc:
            typer.echo(f"Error: {exc.message} ({exc.code})", err=True)
            raise typer.Exit(2) from exc
        except QueueFull as exc:
            typer.echo(f"Error: {exc.message} ({exc.code})", err=True)
            raise typer.Exit(4) from exc
        document = (
            _wait_for(database, outcome.job_id, timeout_seconds=timeout_seconds)
            if wait
            else job_document(database, outcome.job_id)
        )
    _print_job(document, json_output=json_output)
    if wait and document["state"] != "completed":
        raise typer.Exit(1)


@app.command("list")
def list_command(
    state: Annotated[
        str | None, typer.Option("--state", help="Comma-separated states to include.")
    ] = None,
    job_class: Annotated[str | None, typer.Option("--class", help="One class.")] = None,
    task: Annotated[str | None, typer.Option("--task", help="One task profile.")] = None,
    limit: Annotated[int, typer.Option("--limit", help="How many, newest first.")] = 50,
    config: Annotated[
        str | None, typer.Option("--config", help="Path to a config.toml file.")
    ] = None,
    json_output: Annotated[
        bool, typer.Option("--json", help="Print the job documents as JSON.")
    ] = False,
) -> None:
    """List jobs, newest first. Mode: local.

    Example:
        loadcoach job list --state queued,waiting_resources --json
    """
    from loadcoach.domain.priority import JobClass
    from loadcoach.domain.queue_state import JobState
    from loadcoach.services.queue import job_document, list_jobs

    with _open(config) as (database, _settings):
        records = list_jobs(
            database,
            states=None if state is None else [JobState(s) for s in state.split(",") if s],
            job_class=None if job_class is None else JobClass(job_class),
            task=task,
            limit=limit,
        )
        if json_output:
            typer.echo(json.dumps([job_document(database, r.job_id) for r in records]))
            return
    if not records:
        typer.echo("no jobs")
        return
    for record in records:
        typer.echo(
            f"{record.job_id}  {record.state.value:<18} {record.job_class.value:<12} "
            f"{record.task_profile_id:<28} {record.created_at.isoformat()}"
        )


@app.command("show")
def show(
    job_id: Annotated[str, typer.Argument(help="The job's ID.")],
    config: Annotated[
        str | None, typer.Option("--config", help="Path to a config.toml file.")
    ] = None,
    json_output: Annotated[
        bool, typer.Option("--json", help="Print the job document as JSON.")
    ] = False,
) -> None:
    """Show one job in full. Mode: local. Exit 5 if there is no such job."""
    from loadcoach.services.queue import JobNotFound, job_document

    with _open(config) as (database, _settings):
        try:
            document = job_document(database, job_id)
        except JobNotFound as exc:
            typer.echo(f"Error: {exc.message} ({exc.code})", err=True)
            raise typer.Exit(5) from exc
    _print_job(document, json_output=json_output)


@app.command("cancel")
def cancel(
    job_id: Annotated[str, typer.Argument(help="The job's ID.")],
    config: Annotated[
        str | None, typer.Option("--config", help="Path to a config.toml file.")
    ] = None,
    json_output: Annotated[bool, typer.Option("--json", help="Print JSON.")] = False,
) -> None:
    """Cancel a job. Mode: local. Exit 1 if it is already terminal, 5 if it does not exist.

    An executing job stops at its next chunk boundary once the running server's lease keeper
    carries the request across (within one renewal interval).
    """
    from datetime import UTC, datetime

    from loadcoach.services.job_events import JobEventSink
    from loadcoach.services.queue import JobNotCancellable, JobNotFound, cancel_job

    with _open(config) as (database, _settings):
        try:
            outcome = cancel_job(database, JobEventSink(), job_id, now=datetime.now(UTC))
        except JobNotFound as exc:
            typer.echo(f"Error: {exc.message} ({exc.code})", err=True)
            raise typer.Exit(5) from exc
        except JobNotCancellable as exc:
            typer.echo(f"Error: {exc.message} ({exc.code})", err=True)
            raise typer.Exit(1) from exc
    if json_output:
        typer.echo(
            json.dumps(
                {"job_id": outcome.job_id, "state": outcome.state.value, "already": outcome.already}
            )
        )
        return
    typer.echo(
        f"job {outcome.job_id}: {outcome.state.value}" + (" (already)" if outcome.already else "")
    )


@app.command("wait")
def wait_command(
    job_id: Annotated[str, typer.Argument(help="The job's ID.")],
    timeout_seconds: Annotated[
        float, typer.Option("--timeout", help="How long to block, in seconds.")
    ] = 600.0,
    config: Annotated[
        str | None, typer.Option("--config", help="Path to a config.toml file.")
    ] = None,
    json_output: Annotated[
        bool, typer.Option("--json", help="Print the job document as JSON.")
    ] = False,
) -> None:
    """Block until the job is terminal. Mode: local. Exit 0, 1 (failed/cancelled), 4 (timeout)."""
    from loadcoach.services.queue import JobNotFound

    with _open(config) as (database, _settings):
        try:
            document = _wait_for(database, job_id, timeout_seconds=timeout_seconds)
        except JobNotFound as exc:
            typer.echo(f"Error: {exc.message} ({exc.code})", err=True)
            raise typer.Exit(5) from exc
    _print_job(document, json_output=json_output)
    state = document["state"]
    if state not in _TERMINAL:
        raise typer.Exit(4)
    if state != "completed":
        raise typer.Exit(1)
