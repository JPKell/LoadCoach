"""loadcoach.services.doctor — every documented failure mode, diagnosed by name (dev-plan P9).

``loadcoach doctor`` walks spec §13's error codes and §5's degradation contract and, for each one
a local check can detect, says whether this installation would hit it, why, and what to do. It
is a list of named checks over the same services the health report uses — not a second opinion
on health, but the *explanation* health does not give: health says ``database: unavailable``;
the doctor says ``DATABASE_UNAVAILABLE: sqlite:///… cannot be opened (…); create the directory or
set storage.database_url``.

Each check names the code it stands for, so the list can be held against spec §13 by a test.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import urlsplit

from loadcoach.config import LOOPBACK_HOSTS, ConfigurationError, load_settings

if TYPE_CHECKING:
    from loadcoach.config import Settings
    from loadcoach.services.database import Database

__all__ = ["DOCUMENTED_FAILURE_MODES", "Diagnosis", "Finding", "diagnose"]

type Verdict = Literal["ok", "warn", "fail", "skip"]

DOCUMENTED_FAILURE_MODES: tuple[str, ...] = (
    # spec §13's codes a local check can reach, and §5's degradation contract
    "CONFIGURATION_ERROR",
    "INSECURE_BINDING",
    "DATABASE_UNAVAILABLE",
    "MIGRATION_REQUIRED",
    "SCHEMA_AHEAD",
    "STORAGE_FULL",
    "PROVIDER_UNAVAILABLE",
    "MODEL_NOT_FOUND",
    "NO_ELIGIBLE_MODEL",
    "TASK_PROFILE_NOT_FOUND",
    "EVIDENCE_SOURCE_REFUSED",
    "EVIDENCE_IMPORT_FAILED",
    "QUEUE_FULL",
    "MAX_WAIT_EXCEEDED",
    "INSUFFICIENT_RESOURCES",
    "STORAGE_BUSY",
    "RATE_LIMITED",
    "degraded:provider",
    "degraded:evidence",
    "degraded:queue",
    "degraded:reliability",
    "degraded:telemetry",
)
"""What the doctor knows how to look for. A test holds this list against spec §13."""


@dataclass(frozen=True, slots=True)
class Finding:
    """One check's result."""

    code: str
    verdict: Verdict
    detail: str
    remedy: str | None = None

    def as_json(self) -> dict[str, Any]:
        """The record ``loadcoach doctor --json`` prints."""
        return {
            "code": self.code,
            "verdict": self.verdict,
            "detail": self.detail,
            "remedy": self.remedy,
        }


@dataclass(frozen=True, slots=True)
class Diagnosis:
    """Every finding, and the worst of them."""

    findings: tuple[Finding, ...]

    @property
    def status(self) -> Verdict:
        """``fail`` if any check failed, else ``warn`` if any warned, else ``ok``."""
        verdicts = {finding.verdict for finding in self.findings}
        if "fail" in verdicts:
            return "fail"
        if "warn" in verdicts:
            return "warn"
        return "ok"

    def as_json(self) -> dict[str, Any]:
        """The ``--json`` document."""
        return {"status": self.status, "findings": [f.as_json() for f in self.findings]}


def _settings(config_path: str | None) -> tuple[Settings | None, Finding]:
    try:
        loaded = load_settings(config_path=config_path)
    except ConfigurationError as exc:
        remedy = (
            "create at least one token with `loadcoach token create`, name the hosts in "
            "server.allowed_hosts, and set server.allow_lan_exposure only if you mean 0.0.0.0"
            if exc.code == "INSECURE_BINDING"
            else "fix the named field; `loadcoach config validate` shows the problem"
        )
        return None, Finding(exc.code, "fail", exc.message, remedy)
    return loaded.settings, Finding(
        "CONFIGURATION_ERROR",
        "ok",
        f"configuration loads; server.host {loaded.settings.server.host}:"
        f"{loaded.settings.server.port}",
    )


def _database(settings: Settings) -> tuple[Database | None, list[Finding]]:
    from loadcoach.services.database import Database, get_status

    url = settings.storage.database_url or ""
    try:
        database = Database.from_url(
            url, statement_timeout_ms=settings.storage.statement_timeout_ms
        )
        status = get_status(database)
    except Exception as exc:  # noqa: BLE001 — every failure here is the documented one
        return None, [
            Finding(
                "DATABASE_UNAVAILABLE",
                "fail",
                f"{url} cannot be opened: {exc}",
                "create the directory, fix storage.database_url, or check the server is up",
            )
        ]
    findings: list[Finding] = []
    if status.is_at_head:
        findings.append(
            Finding("MIGRATION_REQUIRED", "ok", f"database at head {status.head_revision}")
        )
    else:
        from loadcoach.services.database import migration_runner

        known = migration_runner(database.engine).known_revisions()
        if status.current_revision is not None and status.current_revision not in known:
            findings.append(
                Finding(
                    "SCHEMA_AHEAD",
                    "fail",
                    f"database at {status.current_revision}, this build's head is "
                    f"{status.head_revision}: written by a newer version",
                    "install the newer version, or restore the pre-migration backup and stay",
                )
            )
        else:
            findings.append(
                Finding(
                    "MIGRATION_REQUIRED",
                    "warn" if settings.storage.auto_migrate else "fail",
                    f"database at {status.current_revision or 'no revision'}, head is "
                    f"{status.head_revision}",
                    "run `loadcoach db upgrade`"
                    + ("" if settings.storage.auto_migrate else " (auto_migrate is off)"),
                )
            )
    if url.startswith("sqlite"):
        path = Path(urlsplit(url).path)
        try:
            free = shutil.disk_usage(path.parent if path.parent.exists() else Path.cwd()).free
        except OSError as exc:
            free = -1
            findings.append(Finding("STORAGE_FULL", "warn", f"free space unreadable: {exc}"))
        if free >= 0:
            low = free < 512 * 1024 * 1024
            findings.append(
                Finding(
                    "STORAGE_FULL",
                    "warn" if low else "ok",
                    f"{free // (1024 * 1024)} MiB free beside the database",
                    "free disk space; a write that fails is STORAGE_FULL" if low else None,
                )
            )
    findings.append(
        Finding(
            "STORAGE_BUSY",
            "ok",
            "SQLite busy_timeout is set on every connection; contention beyond it is reported, "
            "never silently retried"
            if url.startswith("sqlite")
            else "PostgreSQL statement and lock timeouts apply",
        )
    )
    return database, findings


def _provider(settings: Settings) -> list[Finding]:
    from modelrack.provider import ProviderStatus

    from loadcoach.infrastructure.providers.factory import build_provider

    try:
        provider = build_provider(settings.provider)
        health = provider.health()
    except Exception as exc:  # noqa: BLE001 — an unreachable provider is the documented case
        return [
            Finding(
                "PROVIDER_UNAVAILABLE",
                "warn",
                f"{settings.provider.kind} at {settings.provider.base_url}: {exc}",
                "start the provider, or set provider.base_url; LoadCoach serves without it, "
                "degraded, and every generation is PROVIDER_UNAVAILABLE until it is back",
            ),
            Finding("degraded:provider", "warn", "health reports provider: unavailable"),
        ]
    if health.status is not ProviderStatus.OK:
        return [
            Finding(
                "PROVIDER_UNAVAILABLE",
                "warn",
                f"{settings.provider.kind}: {health.detail}",
                "start the provider or fix provider.base_url",
            ),
            Finding("degraded:provider", "warn", f"health reports provider: {health.status}"),
        ]
    return [
        Finding("PROVIDER_UNAVAILABLE", "ok", f"{settings.provider.kind} reachable"),
        Finding("degraded:provider", "ok", "provider healthy"),
    ]


def _models_and_profiles(database: Database, settings: Settings) -> list[Finding]:
    from loadcoach.services.models import list_registry
    from loadcoach.services.task_profiles import list_stored_task_profiles

    findings: list[Finding] = []
    entries = list_registry(database)
    available = [entry for entry in entries if entry.available]
    if not entries:
        findings.append(
            Finding(
                "MODEL_NOT_FOUND",
                "warn",
                "no model has been discovered",
                "start the provider and run `loadcoach models refresh`; until then every route "
                "is NO_ELIGIBLE_MODEL",
            )
        )
        findings.append(
            Finding("NO_ELIGIBLE_MODEL", "warn", "nothing to route to: no models discovered")
        )
    elif not available:
        findings.append(
            Finding(
                "MODEL_NOT_FOUND",
                "warn",
                f"{len(entries)} model(s) known, none available: "
                + "; ".join(f"{e.canonical_id}: {e.unavailable_reason}" for e in entries[:3]),
                "start the provider; `loadcoach models list` shows each reason",
            )
        )
        findings.append(
            Finding("NO_ELIGIBLE_MODEL", "warn", "nothing to route to: no model is available")
        )
    else:
        findings.append(
            Finding("MODEL_NOT_FOUND", "ok", f"{len(available)} of {len(entries)} models available")
        )
        findings.append(
            Finding(
                "NO_ELIGIBLE_MODEL",
                "ok",
                "at least one model can be a candidate; a task profile's constraints decide "
                "the rest",
            )
        )
    profiles = list_stored_task_profiles(database)
    enabled = [profile for profile in profiles if profile.enabled]
    findings.append(
        Finding(
            "TASK_PROFILE_NOT_FOUND",
            "ok" if enabled else "fail",
            f"{len(enabled)} enabled task profile(s) imported"
            if enabled
            else "no task profile is imported: every request is TASK_PROFILE_NOT_FOUND",
            None if enabled else "start the server once, or `loadcoach tasks validate`",
        )
    )
    _ = settings
    return findings


def _evidence(database: Database, settings: Settings) -> list[Finding]:
    from loadcoach.infrastructure.freeweight_client import check_url, policy_from_settings
    from loadcoach.services.evidence import evidence_overview

    url = settings.evidence.freeweight_url.strip()
    findings: list[Finding] = []
    if not url:
        findings.append(
            Finding(
                "EVIDENCE_SOURCE_REFUSED",
                "ok",
                "no FreeWeight configured: routing runs on declared capabilities and priors, "
                "and health reports evidence: not_configured (healthy)",
            )
        )
        findings.append(Finding("degraded:evidence", "ok", "evidence not configured (healthy)"))
    else:
        try:
            check_url(url, policy_from_settings(settings.evidence))
            findings.append(
                Finding("EVIDENCE_SOURCE_REFUSED", "ok", f"{url} passes the fetch allowlist")
            )
        except Exception as exc:  # noqa: BLE001 — the refusal is the finding
            findings.append(
                Finding(
                    "EVIDENCE_SOURCE_REFUSED",
                    "fail",
                    f"{url}: {exc}",
                    "add the host to evidence.allowed_source_hosts, or fix the URL (ADR-0026 §3)",
                )
            )
        overview = evidence_overview(database, configured_url=url)
        verdict: Verdict = "ok" if overview.status in ("ok", "none", "not_configured") else "warn"
        findings.append(
            Finding(
                "degraded:evidence",
                verdict,
                f"{overview.status}: {overview.note}",
                None
                if verdict == "ok"
                else "run `loadcoach evidence refresh` once FreeWeight is up",
            )
        )
    findings.append(
        Finding(
            "EVIDENCE_IMPORT_FAILED",
            "ok",
            "accepting benchmark.evidence_bundle majors "
            f"{list(settings.evidence.accept_schema_majors)}",
        )
    )
    return findings


def _queue(database: Database, settings: Settings) -> list[Finding]:
    from loadcoach.services.queue import queue_flags, queue_snapshot

    now = datetime.now(UTC)
    snapshot = queue_snapshot(
        database, now=now, default_max_wait_seconds=settings.queue.max_wait_seconds
    )
    flags = queue_flags(database)
    findings: list[Finding] = []
    fraction = snapshot.active / settings.queue.max_depth if settings.queue.max_depth else 0.0
    findings.append(
        Finding(
            "QUEUE_FULL",
            "warn" if fraction >= 0.8 else "ok",
            f"{snapshot.active} active of max_depth {settings.queue.max_depth}"
            f" (per-source cap {settings.queue.max_active_per_source or 'off'})",
            "raise queue.max_depth or drain; submissions past it are QUEUE_FULL"
            if fraction >= 0.8
            else None,
        )
    )
    findings.append(
        Finding(
            "MAX_WAIT_EXCEEDED",
            "warn" if snapshot.starving else "ok",
            f"{snapshot.starving} starving job(s); default max_wait_seconds "
            f"{settings.queue.max_wait_seconds}",
            "the ageing sweep is running if the server is; a starving job past max_wait fails "
            "with MAX_WAIT_EXCEEDED"
            if snapshot.starving
            else None,
        )
    )
    findings.append(
        Finding(
            "INSUFFICIENT_RESOURCES",
            "warn" if snapshot.depth_by_state.get("waiting_resources") else "ok",
            f"{snapshot.depth_by_state.get('waiting_resources', 0)} job(s) waiting for resources",
            "free VRAM (unload an idle model) or lower the profile's context; a job waits "
            "until max_wait_seconds, then fails with INSUFFICIENT_RESOURCES"
            if snapshot.depth_by_state.get("waiting_resources")
            else None,
        )
    )
    paused, draining = flags["queue.paused"], flags["queue.draining"]
    findings.append(
        Finding(
            "degraded:queue",
            "warn" if (paused or draining or snapshot.starving) else "ok",
            "dispatch paused" if paused else "draining" if draining else "dispatching",
            "`loadcoach queue resume`" if (paused or draining) else None,
        )
    )
    return findings


def _reliability(database: Database) -> list[Finding]:
    from loadcoach.services.reliability import reliability_report

    entries = reliability_report(database)
    regressed = [e for e in entries if e.regression.regressed]
    open_breakers = [e for e in entries if e.circuit_state != "closed"]
    detail = f"{len(entries)} model/profile pair(s) tracked"
    if regressed:
        detail += "; regression: " + ", ".join(
            f"{e.canonical_id}/{e.task_profile_id}" for e in regressed[:3]
        )
    if open_breakers:
        detail += "; breaker open: " + ", ".join(e.canonical_id for e in open_breakers[:3])
    return [
        Finding(
            "degraded:reliability",
            "warn" if (regressed or open_breakers) else "ok",
            detail,
            "see /reliability; a regressed model is deprioritized, an open breaker excluded "
            "until its cool-down"
            if (regressed or open_breakers)
            else None,
        )
    ]


def _exposure(settings: Settings, database: Database | None) -> list[Finding]:
    findings: list[Finding] = []
    host = settings.server.host
    if host in LOOPBACK_HOSTS:
        findings.append(
            Finding("INSECURE_BINDING", "ok", f"bound to loopback {host}; no token needed")
        )
    else:
        tokens = 0
        if database is not None:
            from loadcoach.services.tokens import list_tokens

            tokens = sum(1 for t in list_tokens(database) if t.active)
        findings.append(
            Finding(
                "INSECURE_BINDING",
                "ok" if tokens else "fail",
                f"bound to {host} with allowed_hosts {list(settings.server.allowed_hosts)} and "
                f"{tokens} active token(s)",
                "create a token: `loadcoach token create <name> --scope <read|write|admin>`"
                if not tokens
                else None,
            )
        )
        findings.append(
            Finding(
                "RATE_LIMITED",
                "ok",
                f"rate limit {settings.server.rate_limit_per_minute}/min, burst "
                f"{settings.server.rate_limit_burst}; TLS is a reverse proxy's job (ADR-0014 §7) "
                "and the UI's token cookie needs a secure context",
            )
        )
    if host in LOOPBACK_HOSTS:
        findings.append(
            Finding(
                "RATE_LIMITED",
                "ok",
                f"rate limit {settings.server.rate_limit_per_minute}/min, burst "
                f"{settings.server.rate_limit_burst}",
            )
        )
    return findings


def _telemetry() -> list[Finding]:
    try:
        from sweatmeter import TelemetryCollector

        snapshot = TelemetryCollector().snapshot()
    except Exception as exc:  # noqa: BLE001 — an unreadable machine is the finding
        return [
            Finding(
                "degraded:telemetry",
                "warn",
                f"telemetry unreadable: {exc}",
                "routing runs without VRAM/RAM constraints rather than with invented zeros",
            )
        ]
    if not snapshot.gpus:
        return [
            Finding(
                "degraded:telemetry",
                "warn",
                "no GPU reported: admission degrades to RAM-only or unconstrained, with a reason",
            )
        ]
    return [Finding("degraded:telemetry", "ok", f"{len(snapshot.gpus)} GPU(s) readable")]


def diagnose(*, config_path: str | None = None) -> Diagnosis:
    """Run every check and return the diagnosis.

    Args:
        config_path: An explicit ``config.toml``, or ``None`` for the standard precedence chain.

    Returns:
        The :class:`Diagnosis`, one :class:`Finding` per documented failure mode reachable
        locally.
    """
    settings, configuration = _settings(config_path)
    findings: list[Finding] = [configuration]
    if settings is None:
        return Diagnosis(tuple(findings))
    database, database_findings = _database(settings)
    findings.extend(database_findings)
    findings.extend(_exposure(settings, database))
    findings.extend(_provider(settings))
    if database is not None:
        try:
            findings.extend(_models_and_profiles(database, settings))
            findings.extend(_evidence(database, settings))
            findings.extend(_queue(database, settings))
            findings.extend(_reliability(database))
        except Exception as exc:  # noqa: BLE001 — an unmigrated database explains itself above
            findings.append(
                Finding(
                    "MIGRATION_REQUIRED",
                    "warn",
                    f"tables unreadable: {exc}",
                    "run `loadcoach db upgrade`",
                )
            )
        finally:
            database.close()
    findings.extend(_telemetry())
    return Diagnosis(tuple(findings))
