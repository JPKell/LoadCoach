"""loadcoach.services.machine — the one machine LoadCoach knows: its own.

Spec §10: *LoadCoach holds no ``machines`` table. It knows exactly one machine — its own, whose
fingerprint comes from SweatMeter at startup — and it compares imported evidence's
``machine_fingerprint`` against that single value.*

The fingerprint is SweatMeter's, not one derived here, and that is the whole point: FreeWeight
computes its own through the same call, so two applications on one machine agree without either
knowing about the other. A locally-derived fingerprint would never match, and every performance
measurement would be silently excluded as "from another machine".

Not in Phase 6's literal file list. It exists for the reason
:mod:`loadcoach.services.database` does: the web layer, the CLI and the queue worker all need
this value and none of them may import another (``.importlinter``'s ``web-cli-independence``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sweatmeter import TelemetryCollector

__all__ = ["machine_fingerprint", "read_machine_fingerprint", "reset_machine_fingerprint"]

_CACHED: str | None = None
_RESOLVED = False


def read_machine_fingerprint(collector: TelemetryCollector | None = None) -> str | None:
    """Profile this host and return its fingerprint, or ``None`` if it cannot be produced.

    Args:
        collector: The SweatMeter collector to profile through. ``None`` builds one.

    Returns:
        The fingerprint, or ``None``. ``None`` is not a failure to be raised: a machine whose
        static facts cannot be read has not established that it is a *different* machine from
        the one an evidence record names, so routing admits everything rather than refusing it
        all (ADR-0016's distinction, applied to identity).
    """
    try:
        if collector is None:
            from sweatmeter import TelemetryCollector as _Collector

            collector = _Collector()
        fingerprint = collector.machine_profile().machine_fingerprint
    except (OSError, RuntimeError, ValueError):
        return None
    return fingerprint or None


def machine_fingerprint(*, refresh: bool = False) -> str | None:
    """Return this process's machine fingerprint, profiling at most once.

    Args:
        refresh: Re-profile even if a value is cached.

    Returns:
        The fingerprint, or ``None``.
    """
    global _CACHED, _RESOLVED  # noqa: PLW0603 — a deliberate once-per-process cache
    if refresh or not _RESOLVED:
        _CACHED = read_machine_fingerprint()
        _RESOLVED = True
    return _CACHED


def reset_machine_fingerprint() -> None:
    """Forget the cached fingerprint. For tests, which must not inherit another test's machine."""
    global _CACHED, _RESOLVED  # noqa: PLW0603 — the cache above, cleared
    _CACHED = None
    _RESOLVED = False
