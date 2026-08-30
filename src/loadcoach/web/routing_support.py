"""loadcoach.web.routing_support — turning application state into routing's injected inputs.

Routing's own functions take values: a :class:`~loadcoach.domain.routing.subject.ProviderFacts`,
a :class:`~loadcoach.services.routing.RoutingPolicy` and a telemetry snapshot. That is what makes
a decision reproducible. This module is the one place that reads them off the live application, so
no route handler has to, and so the web layer's dependency on ModelRack and SweatMeter is a single
named file rather than a scatter of imports across handlers.

Not in the Phase 3 file list. It exists because the alternative — building these three values
inside each handler — puts provider and telemetry construction into route bodies, which CLI
standards §1 and the application's own layering both forbid.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from loadcoach.services.execution import provider_facts_for
from loadcoach.services.machine import machine_fingerprint
from loadcoach.services.routing import RoutingPolicy

if TYPE_CHECKING:
    from fastapi import FastAPI
    from sweatmeter import TelemetrySnapshot

    from loadcoach.config import Settings
    from loadcoach.services.database import Database

__all__ = ["current_snapshot", "provider_facts_for", "routing_policy_for"]


def routing_policy_for(settings: Settings, *, database: Database | None = None) -> RoutingPolicy:
    """Build the routing policy from settings, with the runtime-changeable overrides applied.

    The machine fingerprint is read here rather than passed in because it is a property of the
    process, not of a request: SweatMeter profiles the host once and every decision compares
    imported evidence against that one value (spec §10). With ``database`` given, the routing
    keys a ``PUT /settings`` may have changed are read from the ``settings`` table (api.md §9),
    so ``POST /route`` and the queue's workers apply the same values.
    """
    policy = RoutingPolicy.from_settings(
        routing=settings.routing,
        runtime=settings.runtime,
        telemetry=settings.telemetry,
        evidence=settings.evidence,
        machine_fingerprint=machine_fingerprint(),
    )
    if database is None:
        return policy
    from dataclasses import replace

    from loadcoach.services.settings import read_runtime_settings

    effective = read_runtime_settings(database, settings=settings)
    return replace(
        policy,
        prefer_resident_bonus=float(effective["routing.prefer_resident_bonus"]),
        min_present_weight=float(effective["routing.min_present_weight"]),
        min_confidence=float(effective["routing.min_confidence"]),
        remote_cost_factor=float(effective["routing.remote_cost_factor"]),
    )


def current_snapshot(app: FastAPI) -> TelemetrySnapshot | None:
    """Take one telemetry observation, or return ``None`` when none can be taken.

    ``None`` disables the VRAM and RAM constraints for that decision rather than substituting
    zeros — a machine whose telemetry cannot be read has not reported that it has no memory
    (ADR-0016). The collector is built once and kept on application state: constructing one
    re-probes for an NVML binding every time.

    Args:
        app: The FastAPI application.

    Returns:
        The snapshot, or ``None``.
    """
    collector = getattr(app.state, "telemetry_collector", None)
    if collector is None:
        from sweatmeter import TelemetryCollector

        collector = TelemetryCollector()
        app.state.telemetry_collector = collector
    try:
        return collector.snapshot()
    except OSError:
        return None
