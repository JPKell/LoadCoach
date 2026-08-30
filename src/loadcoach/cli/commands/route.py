"""loadcoach.cli.commands.route — ``loadcoach route explain``.

Not in the Phase 3 file list verbatim, but named in its Work item ("``loadcoach route explain``").
The CLI cannot import the web layer (``.importlinter``'s web/CLI independence contract), so this
command builds routing's injected inputs itself from configuration, a provider handle and one
telemetry snapshot — the same three values ``POST /route`` passes, obtained the same way.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING, Annotated

import typer

if TYPE_CHECKING:
    from sweatmeter import TelemetrySnapshot

    from loadcoach.config import Settings
    from loadcoach.services.database import Database

__all__ = ["app"]

app = typer.Typer(help="Explain a routing decision without executing it.")


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


@app.command("explain")
def explain(
    task: Annotated[str, typer.Option("--task", help="The task profile to route for.")],
    input_tokens: Annotated[
        int | None,
        typer.Option("--input-tokens", help="Estimated prompt size, for context budgeting."),
    ] = None,
    max_output_tokens: Annotated[
        int | None, typer.Option("--max-output-tokens", help="Override the profile's allowance.")
    ] = None,
    require_evidence: Annotated[
        bool,
        typer.Option("--require-evidence", help="Refuse to route on declared or manual priors."),
    ] = False,
    config: Annotated[
        str | None, typer.Option("--config", help="Path to a config.toml file.")
    ] = None,
    json_output: Annotated[
        bool, typer.Option("--json", help="Print the whole explanation as JSON.")
    ] = False,
) -> None:
    """Route a task and print the explanation. Mode: local. Exit 0, 3, 4 or 5.

    Exit 5 when the task profile is unknown; exit 4 when no model was eligible — in which case
    every candidate and the constraint that rejected it is printed, because "nothing was eligible"
    is useless without them.

    Example:
        loadcoach route explain --task code.review --input-tokens 12000
    """
    from datetime import UTC, datetime

    from modelrack import ProviderError, ProviderStatus

    from loadcoach.domain.routing.subject import ProviderFacts, RuntimeOverrides
    from loadcoach.infrastructure.providers.factory import build_provider
    from loadcoach.services.machine import machine_fingerprint
    from loadcoach.services.routing import (
        NoEligibleModel,
        RouteRequest,
        RoutingPolicy,
        TaskProfileNotFound,
        route,
    )
    from loadcoach.services.task_profiles import import_task_profiles, read_task_profiles_file

    with _open(config) as (database, settings):
        import_task_profiles(database, read_task_profiles_file(), now=datetime.now(UTC))
        provider = build_provider(settings.provider)
        try:
            capabilities = provider.capabilities()
            health = provider.health()
            facts = ProviderFacts(
                healthy=health.status is not ProviderStatus.UNAVAILABLE,
                context_configurable=capabilities.context_configurable,
                supports_tool_use=capabilities.tool_calling,
                supports_structured_output=capabilities.structured_output,
                supports_streaming=capabilities.streaming,
                is_remote=health.is_remote,
            )
        except ProviderError:
            facts = ProviderFacts(healthy=False)

        snapshot = _snapshot()
        try:
            result = route(
                database,
                RouteRequest(
                    task=task,
                    estimated_input_tokens=input_tokens,
                    max_output_tokens=max_output_tokens,
                    overrides=RuntimeOverrides(require_evidence=require_evidence),
                ),
                provider=facts,
                policy=RoutingPolicy.from_settings(
                    routing=settings.routing,
                    runtime=settings.runtime,
                    telemetry=settings.telemetry,
                    evidence=settings.evidence,
                    machine_fingerprint=machine_fingerprint(),
                ),
                snapshot=snapshot,
                now=datetime.now(UTC),
            )
        except TaskProfileNotFound as exc:
            typer.echo(f"Error: {exc.message} ({exc.code})", err=True)
            raise typer.Exit(5) from exc
        except NoEligibleModel as exc:
            typer.echo(f"Error: {exc.message} ({exc.code})", err=True)
            for candidate in exc.details.get("candidates", []):
                typer.echo(f"  {candidate['canonical_id']}: {candidate['reason']}", err=True)
            raise typer.Exit(4) from exc

    payload = result.explanation.payload
    if json_output:
        typer.echo(json.dumps(payload, indent=2, default=str))
        return
    _print_human(payload)


def _snapshot() -> TelemetrySnapshot | None:
    """Take one telemetry observation, or ``None`` when the machine cannot be read.

    ``None`` disables the resource constraints for this decision rather than substituting zeros:
    telemetry that could not be read has not reported that the machine has no memory (ADR-0016).
    """
    from sweatmeter import TelemetryCollector

    try:
        return TelemetryCollector().snapshot()
    except OSError:
        return None


def _print_human(payload: dict[str, object]) -> None:
    from typing import Any, cast

    selected = cast("dict[str, Any] | None", payload["selected"])
    typer.echo(f"decision  {payload['decision_id']}  ({payload['duration_ms']} ms)")
    if selected is not None:
        typer.echo(f"selected  {selected['canonical_id']}")
        typer.echo(f"  runtime_profile_hash  {selected['runtime_profile_hash']}")
        typer.echo(
            f"  served_context        {selected['served_context']} "
            f"({selected['served_context_source']})"
        )
        typer.echo(f"  final_score           {selected['final_score']:.4f}")
        typer.echo(f"  target_gpu_index      {selected['target_gpu_index']}")
    flags = cast("list[str]", payload["flags"])
    typer.echo(f"flags     {', '.join(flags) or 'none'}")
    evidence = cast("dict[str, Any]", payload["evidence_summary"])
    typer.echo(f"evidence  {evidence['source']}")
    for candidate in cast("list[dict[str, Any]]", payload["candidates"]):
        typer.echo(f"  #{candidate['rank']} {candidate['canonical_id']}")
        for capability in candidate["capabilities"]:
            score = capability["score"]
            rendered = "absent" if score is None else f"{score:.3f}"
            typer.echo(
                f"      {capability['capability']:<24} w={capability['weight']:<6} "
                f"{rendered:<8} {capability['source']}"
            )
    for rejection in cast("list[dict[str, Any]]", payload["rejected"]):
        typer.echo(f"  rejected {rejection['canonical_id']}: {rejection['reason']}")
