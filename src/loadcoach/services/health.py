"""loadcoach.services.health — the one health report shared by the API and the CLI.

Both ``GET /api/v1/health`` and ``loadcoach health`` call :func:`get_health_report`, which is how
"the two surfaces return identical component data" holds by construction rather than by
coincidence. Not in the Phase 1 file list verbatim, but required by it for the same reason as
:mod:`loadcoach.services.database`: CLI and web must not import each other
(``.importlinter``'s ``web-cli-independence`` contract), so shared logic they both need lives in
``services/``.

Phase 1 reports two components only — ``database`` and ``provider`` — matching what exists to
report on; ``evidence``, ``queue`` and ``gpu_telemetry`` (spec §17) arrive with the phases that
build them.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from baseaicore.timeutil import Clock, to_rfc3339, utc_now
from pydantic import BaseModel, ConfigDict

from loadcoach.__about__ import __version__

if TYPE_CHECKING:
    from modelrack.provider import Provider

    from loadcoach.services.database import Database

__all__ = ["HealthComponent", "HealthReport", "get_health_report"]

type ComponentStatus = Literal["ok", "degraded", "unavailable", "not_configured"]
type OverallStatus = Literal["ok", "degraded", "unavailable"]

_SEVERITY: dict[ComponentStatus, int] = {
    "ok": 0,
    "not_configured": 0,
    "degraded": 1,
    "unavailable": 2,
}
_DEGRADED_SEVERITY = _SEVERITY["degraded"]

# Graceful Degradation §3: overall status is the worst of the components *required* for core
# function. LoadCoach starts and serves with no provider and no evidence (spec §5) — the database
# is the one dependency without which nothing can be read or written at all.
_REQUIRED_COMPONENTS: frozenset[str] = frozenset({"database"})


class HealthComponent(BaseModel):
    """One dependency's status, per Graceful Degradation §3."""

    model_config = ConfigDict(extra="forbid")

    name: str
    status: ComponentStatus
    detail: str


class HealthReport(BaseModel):
    """The application-wide health shape, identical across the HTTP API and the CLI."""

    model_config = ConfigDict(extra="forbid")

    status: OverallStatus
    version: str
    checked_at: str
    components: tuple[HealthComponent, ...] = ()


def _database_component(database: Database | None) -> HealthComponent:
    """Build the ``database`` component, tolerating a totally unreadable configuration.

    Imported lazily to avoid a cycle: :mod:`loadcoach.services.database` imports
    :class:`HealthComponent` from this module.

    Args:
        database: The caller's handle, or ``None`` to open one for this check alone. The web
            application passes the handle it serves from; a one-shot ``loadcoach health`` has no
            such handle and passes ``None``.
    """
    from loadcoach.config import ConfigurationError, load_settings
    from loadcoach.services.database import Database, database_health_component

    if database is not None:
        return database_health_component(database)

    try:
        loaded = load_settings()
    except ConfigurationError as exc:
        return HealthComponent(
            name="database", status="degraded", detail=f"configuration: {exc.message}"
        )
    database_url = loaded.settings.storage.database_url
    if database_url is None:  # pragma: no cover — StorageSettings always fills this in
        return HealthComponent(
            name="database", status="degraded", detail="no database_url configured"
        )
    with Database.from_url(
        database_url, statement_timeout_ms=loaded.settings.storage.statement_timeout_ms
    ) as opened:
        return database_health_component(opened)


def _provider_component(provider: Provider | None) -> HealthComponent:
    """Build the ``provider`` component, tolerating a totally unreachable or unconfigured provider.

    :meth:`~modelrack.provider.Provider.health` itself never raises (its own contract), so the
    only failure this function must absorb is a bad ``provider.kind`` in configuration.

    Args:
        provider: The caller's handle, or ``None`` to build one for this check alone.
    """
    from modelrack.provider import ProviderStatus

    if provider is None:
        from loadcoach.config import ConfigurationError, load_settings
        from loadcoach.infrastructure.providers.factory import build_provider

        try:
            loaded = load_settings()
            provider = build_provider(loaded.settings.provider)
        except ConfigurationError as exc:
            return HealthComponent(
                name="provider", status="degraded", detail=f"configuration: {exc.message}"
            )

    health = provider.health()
    status: ComponentStatus = (
        "ok"
        if health.status is ProviderStatus.OK
        else "degraded"
        if health.status is ProviderStatus.DEGRADED
        else "unavailable"
    )
    return HealthComponent(name="provider", status=status, detail=health.detail)


def get_health_report(
    *,
    database: Database | None = None,
    provider: Provider | None = None,
    clock: Clock = utc_now,
) -> HealthReport:
    """Build the current health report.

    Both ``GET /api/v1/health`` and ``loadcoach health`` call this one function, which is what
    keeps the two surfaces identical by construction. They differ only in where the connection and
    the provider come from: the route passes the handle and provider the server is already serving
    from; the CLI passes neither and one of each is opened for the check alone.

    Args:
        database: The caller's database handle, or ``None`` to open one for this check alone.
        provider: The caller's provider handle, or ``None`` to build one for this check alone.
        clock: Returns the current instant; injected for deterministic tests.

    Returns:
        The :class:`HealthReport`. The overall status is the worst of the required components
        (``database``) uncapped, joined with the worst of the optional ones (``provider``) capped
        at ``degraded`` — an unreachable provider is never by itself what makes the whole
        application ``unavailable`` (spec §5: LoadCoach starts and serves with no provider).
    """
    components: tuple[HealthComponent, ...] = (
        _database_component(database),
        _provider_component(provider),
    )
    worst = max(
        (
            _SEVERITY[component.status]
            if component.name in _REQUIRED_COMPONENTS
            else min(_SEVERITY[component.status], _DEGRADED_SEVERITY)
            for component in components
        ),
        default=0,
    )
    overall: OverallStatus = "unavailable" if worst >= 2 else "degraded" if worst == 1 else "ok"
    return HealthReport(
        status=overall,
        version=__version__,
        checked_at=to_rfc3339(clock()),
        components=components,
    )
