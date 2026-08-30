"""loadcoach.cli.commands.generate — ``loadcoach generate`` (spec §7.2): route, execute, print.

The synchronous path, in process: the same :func:`~loadcoach.services.execution.execute` the
``POST /generate`` route calls, driven as the local principal. ``--stream`` prints each delta as
the provider produces it. Only ``typer`` and ``json`` load at import time (CLI standards §12).
"""

from __future__ import annotations

import json
from typing import Annotated

import typer

__all__ = ["generate"]


def generate(
    task: Annotated[str, typer.Option("--task", help="The task profile to route for.")],
    prompt: Annotated[str | None, typer.Option("--prompt", help="The prompt text.")] = None,
    prompt_file: Annotated[
        str | None, typer.Option("--prompt-file", help="Read the prompt from this file.")
    ] = None,
    system: Annotated[str | None, typer.Option("--system", help="A system turn.")] = None,
    stream: Annotated[
        bool, typer.Option("--stream", help="Print deltas as they arrive, then the summary.")
    ] = False,
    config: Annotated[
        str | None, typer.Option("--config", help="Path to a config.toml file.")
    ] = None,
    json_output: Annotated[
        bool, typer.Option("--json", help="Print the response document as JSON.")
    ] = False,
) -> None:
    """Route a task and execute it now, printing the output. Mode: local.

    Exit 0 on success; 2 for a bad invocation; 3 for a configuration error; 4 when no model was
    eligible or every candidate failed; 5 when the task is unknown.
    """
    from datetime import UTC, datetime
    from pathlib import Path

    from baseaicore import SuiteError
    from modelrack import ProviderError

    from loadcoach.cli.commands.route import _open, _snapshot
    from loadcoach.domain.authorization import LOCAL
    from loadcoach.infrastructure.providers.factory import build_provider
    from loadcoach.services.execution import (
        ExecutionContext,
        GenerateRequest,
        StreamChunk,
        execute,
        provider_facts_for,
    )
    from loadcoach.services.machine import machine_fingerprint
    from loadcoach.services.routing import RoutingPolicy
    from loadcoach.services.task_profiles import (
        DEFAULT_SCHEMAS_DIR,
        import_task_profiles,
        read_task_profiles_file,
    )

    if (prompt is None) == (prompt_file is None):
        typer.echo(
            "Error: give exactly one of --prompt or --prompt-file (VALIDATION_ERROR)", err=True
        )
        raise typer.Exit(2)
    text = prompt if prompt is not None else Path(str(prompt_file)).read_text(encoding="utf-8")

    with _open(config) as (database, settings):
        import_task_profiles(database, read_task_profiles_file(), now=datetime.now(UTC))
        provider = build_provider(settings.provider)
        try:
            facts = provider_facts_for(provider)
        except ProviderError as exc:
            typer.echo(f"Error: {exc.message} ({exc.code})", err=True)
            raise typer.Exit(4) from exc
        context = ExecutionContext(
            provider=provider,
            provider_facts=facts,
            policy=RoutingPolicy.from_settings(
                routing=settings.routing,
                runtime=settings.runtime,
                telemetry=settings.telemetry,
                evidence=settings.evidence,
                machine_fingerprint=machine_fingerprint(),
            ),
            schemas_dir=DEFAULT_SCHEMAS_DIR,
            snapshot=_snapshot(),
            timeout_seconds=settings.execution.default_timeout_seconds,
        )

        def on_chunk(chunk: StreamChunk) -> None:
            if chunk.kind != "token":
                return
            delta = chunk.payload.get("delta") if isinstance(chunk.payload, dict) else None
            if isinstance(delta, str) and delta:
                typer.echo(delta, nl=False)

        try:
            outcome = execute(
                database,
                GenerateRequest(task=task, system=system, prompt=text, source="cli", stream=stream),
                context,
                on_chunk=on_chunk if stream and not json_output else None,
                principal=LOCAL,
            )
        except SuiteError as exc:
            typer.echo(f"Error: {exc.message} ({exc.code})", err=True)
            details = exc.details if isinstance(exc.details, dict) else {}
            for attempt in details.get("attempts", []):
                line = (
                    f"  attempt {attempt.get('attempt')} on {attempt.get('model')}: "
                    f"{attempt.get('outcome')}"
                )
                typer.echo(line, err=True)
            raise typer.Exit(5 if exc.code == "TASK_PROFILE_NOT_FOUND" else 4) from exc

    document = outcome.as_json()
    if json_output:
        typer.echo(json.dumps(document, default=str))
        return
    if stream:
        typer.echo("")
    else:
        typer.echo(document["output"]["text"] or "")
    model = document["model"]
    timing = document["timing"]
    typer.echo(
        f"— {model['canonical_id']} · job {document['job_id']} · provider "
        f"{timing['provider_ms']} ms, overhead {timing['loadcoach_overhead_ms']} ms",
        err=True,
    )
