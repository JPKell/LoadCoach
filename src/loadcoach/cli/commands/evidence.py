"""loadcoach.cli.commands.evidence — import, show, sources, refresh (spec §7.2).

Every command runs against the database directly, without ``serve``: importing a bundle on a
fresh install is exactly the case where no server is running yet. Only ``typer`` and ``json``
load at module level, per the same startup discipline as every other command module.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING, Annotated

import typer

if TYPE_CHECKING:
    from loadcoach.config import LoadedSettings
    from loadcoach.services.database import Database

__all__ = ["app"]

app = typer.Typer(help="FreeWeight evidence import and inspection.")

_ConfigOption = Annotated[str | None, typer.Option("--config", help="Path to a config.toml file.")]
_JsonOption = Annotated[bool, typer.Option("--json", help="Print JSON instead of a table.")]


def _load(config: str | None) -> LoadedSettings:
    from loadcoach.config import ConfigurationError, load_settings

    try:
        return load_settings(config_path=config)
    except ConfigurationError as exc:
        typer.echo(f"Error: {exc.message} ({exc.code})", err=True)
        raise typer.Exit(3) from exc


@contextmanager
def _open_database(loaded: LoadedSettings) -> Iterator[Database]:
    from loadcoach.services.database import Database

    storage = loaded.settings.storage
    if storage.database_url is None:  # pragma: no cover — StorageSettings always fills this in
        typer.echo("Error: no database_url configured (CONFIGURATION_ERROR)", err=True)
        raise typer.Exit(3)
    with Database.from_url(
        storage.database_url, statement_timeout_ms=storage.statement_timeout_ms
    ) as database:
        yield database


def _report(outcome: object, *, json_output: bool) -> None:
    """Print an import outcome as JSON or as lines."""
    payload = outcome.as_json()  # type: ignore[attr-defined]  # ImportOutcome, kept lazy
    if json_output:
        typer.echo(json.dumps(payload, indent=2, sort_keys=True))
        return
    typer.echo(f"source        {payload['source_id']} ({payload['schema_version']})")
    typer.echo(f"records       {payload['total']}")
    typer.echo(f"  imported    {payload['imported']}")
    typer.echo(f"  updated     {payload['updated']}")
    typer.echo(f"  bound       {payload['bound']}")
    typer.echo(f"  unmatched   {payload['unmatched']}")
    typer.echo(f"  ambiguous   {payload['ambiguous_name_only']}")
    typer.echo(f"  superseded  {payload['superseded']}")
    typer.echo(f"  rejected    {len(payload['rejected'])}")
    for rejection in payload["rejected"]:
        typer.echo(
            f"    [{rejection['index']}] {rejection['reason']}: {rejection['detail']}", err=True
        )
    if payload["upgraded_models"]:
        typer.echo(f"upgraded      {payload['upgraded_models']} registry row(s) gained a digest")


@app.command("import")
def import_evidence(
    file: Annotated[
        str | None, typer.Option("--file", help="A benchmark.evidence_bundle document.")
    ] = None,
    url: Annotated[
        str | None, typer.Option("--url", help="A FreeWeight to pull from, through the allowlist.")
    ] = None,
    config: _ConfigOption = None,
    json_output: _JsonOption = False,
) -> None:
    """Import an evidence bundle from a file or a URL. Mode: local.

    Exit codes: 0 imported (even with per-record rejections, which are reported), 2 the bundle was
    unusable or its schema major unsupported, 4 the URL was refused by the fetch allowlist.

    Example:
        loadcoach evidence import --url http://127.0.0.1:8765
    """
    from pathlib import Path

    from loadcoach.infrastructure.freeweight_client import (
        EvidenceSourceRefused,
        EvidenceSourceUnreachable,
        FreeWeightClient,
        policy_from_settings,
    )
    from loadcoach.services.evidence import (
        EvidenceImportFailed,
        EvidenceSchemaVersionUnsupported,
        credential_for,
        import_bundle,
        last_generated_at,
    )

    if (file is None) == (url is None):
        typer.echo("Error: give exactly one of --file or --url (VALIDATION_ERROR)", err=True)
        raise typer.Exit(2)

    from datetime import UTC, datetime

    loaded = _load(config)
    evidence_settings = loaded.settings.evidence
    now = datetime.now(UTC)
    with _open_database(loaded) as database:
        try:
            if url is not None:
                with FreeWeightClient(policy_from_settings(evidence_settings)) as client:
                    fetched = client.fetch(
                        url,
                        since=last_generated_at(database, url=url),
                        credential=credential_for(evidence_settings, url),
                    )
                document: bytes | str = fetched.document
                kind, source_url = "freeweight_api", url
            else:
                document = Path(file or "").read_bytes()
                kind, source_url = "file", None
            outcome = import_bundle(
                database,
                document,
                now=now,
                accept_schema_majors=evidence_settings.accept_schema_majors,
                source_kind=kind,
                url=source_url,
            )
        except EvidenceSourceRefused as exc:
            typer.echo(f"Error: {exc.message} ({exc.code})", err=True)
            raise typer.Exit(4) from exc
        except EvidenceSourceUnreachable as exc:
            typer.echo(f"Error: {exc.message} ({exc.code})", err=True)
            raise typer.Exit(2) from exc
        except (EvidenceSchemaVersionUnsupported, EvidenceImportFailed) as exc:
            typer.echo(f"Error: {exc.message} ({exc.code})", err=True)
            raise typer.Exit(2) from exc
        except OSError as exc:
            typer.echo(f"Error: cannot read {file!r}: {exc}", err=True)
            raise typer.Exit(2) from exc
        _report(outcome, json_output=json_output)


@app.command("show")
def show_evidence(
    capability: Annotated[
        str | None, typer.Option("--capability", help="Filter to one capability ID.")
    ] = None,
    model: Annotated[
        str | None, typer.Option("--model", help="Filter to one model canonical ID.")
    ] = None,
    match_state: Annotated[
        str | None,
        typer.Option("--match-state", help="bound | unmatched | ambiguous_name_only."),
    ] = None,
    limit: Annotated[int, typer.Option("--limit", min=1, help="How many records.")] = 50,
    config: _ConfigOption = None,
    json_output: _JsonOption = False,
) -> None:
    """Show imported evidence, with its coverage per capability. Mode: local.

    Example:
        loadcoach evidence show --capability reasoning --json
    """
    from datetime import UTC, datetime

    from loadcoach.domain.evidence_policy import MATCH_STATES
    from loadcoach.services.evidence import (
        EvidenceQuery,
        capability_coverage,
        evidence_overview,
        query_evidence,
    )

    if match_state is not None and match_state not in MATCH_STATES:
        typer.echo(
            f"Error: --match-state must be one of {sorted(MATCH_STATES)} (VALIDATION_ERROR)",
            err=True,
        )
        raise typer.Exit(2)

    loaded = _load(config)
    configured = loaded.settings.evidence.freeweight_url.strip()
    with _open_database(loaded) as database:
        overview = evidence_overview(database, configured_url=configured)
        coverage = capability_coverage(database)
        page = query_evidence(
            database,
            EvidenceQuery(capability=capability, model=model, match_state=match_state, limit=limit),
            now=datetime.now(UTC),
        )

    if json_output:
        typer.echo(
            json.dumps(
                {
                    "status": overview.status,
                    "note": overview.note,
                    "coverage": [
                        {
                            "capability_id": entry.capability_id,
                            "models": entry.models,
                            "bound": entry.bound,
                            "stale": entry.stale,
                            "best_score": entry.best_score,
                            "best_confidence": entry.best_confidence,
                        }
                        for entry in coverage
                    ],
                    "records": [
                        {
                            "canonical_id": row.canonical_id,
                            "capability_id": row.capability_id,
                            "score": row.score,
                            "confidence": row.confidence,
                            "sample_count": row.sample_count,
                            "age_days": row.age_days,
                            "match_state": row.match_state,
                            "stale": row.stale,
                            "stale_reason": row.stale_reason,
                            "runtime_profile_hash": row.runtime_profile_hash,
                            "machine_fingerprint": row.machine_fingerprint,
                            "source_id": row.source_key,
                        }
                        for row in page.items
                    ],
                    "total": page.total,
                },
                indent=2,
                sort_keys=True,
            )
        )
        return

    typer.echo(overview.note)
    if not page.items:
        typer.echo("No evidence imported.")
        return
    typer.echo("")
    typer.echo(f"{'CAPABILITY':<26} {'MODELS':>6} {'SCORING':>7} {'BEST':>6} {'STALE':>6}")
    for entry in coverage:
        best = "—" if entry.best_score is None else f"{entry.best_score:.3f}"
        typer.echo(
            f"{entry.capability_id:<26} {entry.models:>6} {entry.bound:>7} {best:>6} "
            f"{entry.stale:>6}"
        )
    typer.echo("")
    typer.echo(f"{'MODEL':<44} {'CAPABILITY':<24} {'SCORE':>6} {'CONF':>5} {'AGE':>4}  STATE")
    for row in page.items:
        state = row.match_state + (f" · stale: {row.stale_reason}" if row.stale else "")
        typer.echo(
            f"{row.canonical_id[:44]:<44} {row.capability_id[:24]:<24} {row.score:>6.3f} "
            f"{row.confidence:>5.2f} {row.age_days:>4}  {state}"
        )
    typer.echo("")
    typer.echo(f"{len(page.items)} of {page.total} record(s).")


@app.command("sources")
def show_sources(config: _ConfigOption = None, json_output: _JsonOption = False) -> None:
    """Show every evidence source with its last import and status. Mode: local.

    Example:
        loadcoach evidence sources
    """
    from loadcoach.services.evidence import evidence_overview, list_sources

    loaded = _load(config)
    configured = loaded.settings.evidence.freeweight_url.strip()
    with _open_database(loaded) as database:
        sources = list_sources(database, configured_url=configured)
        overview = evidence_overview(database, configured_url=configured)

    if json_output:
        typer.echo(
            json.dumps(
                {
                    "configured_url": configured or None,
                    "status": overview.status,
                    "sources": [source.as_json() for source in sources],
                },
                indent=2,
                sort_keys=True,
            )
        )
        return
    typer.echo(f"configured_url  {configured or '(not configured)'}")
    typer.echo(f"status          {overview.status}")
    typer.echo(overview.note)
    if not sources:
        return
    typer.echo("")
    typer.echo(f"{'SOURCE':<28} {'KIND':<15} {'STATUS':<13} {'ROWS':>5} {'STALE':>5}  LAST IMPORT")
    for source in sources:
        last = "never" if source.last_import_at is None else source.last_import_at.isoformat()
        typer.echo(
            f"{source.source_key[:28]:<28} {source.kind:<15} "
            f"{(source.last_status or 'unknown'):<13} {source.rows:>5} {source.stale_rows:>5}  "
            f"{last}"
        )


@app.command("refresh")
def refresh_evidence(config: _ConfigOption = None, json_output: _JsonOption = False) -> None:
    """Pull from the configured FreeWeight now. Mode: local.

    Exit codes: 0 imported; 3 no source is configured (which is a state, not a failure);
    4 the source is configured but could not be used — the previous import is retained.

    Example:
        loadcoach evidence refresh
    """
    from datetime import UTC, datetime

    from loadcoach.services.evidence import evidence_overview, refresh_from_freeweight

    loaded = _load(config)
    evidence_settings = loaded.settings.evidence
    if not evidence_settings.freeweight_url.strip():
        typer.echo(
            "No evidence source is configured: set [evidence] freeweight_url. Routing continues "
            "on declared capabilities and priors.",
            err=True,
        )
        raise typer.Exit(3)
    with _open_database(loaded) as database:
        outcome = refresh_from_freeweight(database, evidence_settings, now=datetime.now(UTC))
        if outcome is None:
            overview = evidence_overview(
                database, configured_url=evidence_settings.freeweight_url.strip()
            )
            typer.echo(f"Error: {overview.note} ({overview.status})", err=True)
            raise typer.Exit(4)
        _report(outcome, json_output=json_output)
