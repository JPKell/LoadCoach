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
from loadcoach.services.routing import RoutingPolicy

if TYPE_CHECKING:
    from fastapi import FastAPI
    from sweatmeter import TelemetrySnapshot

    from loadcoach.config import Settings

__all__ = ["current_snapshot", "provider_facts_for", "routing_policy_for"]


def routing_policy_for(settings: Settings) -> RoutingPolicy:
    """Build the configured routing policy from settings."""
    return RoutingPolicy.from_settings(
        routing=settings.routing, runtime=settings.runtime, telemetry=settings.telemetry
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
