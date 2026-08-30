"""loadcoach.services.telemetry_stream — the sampled telemetry stream behind the bar (api.md §1).

UI standards §3: the telemetry bar is on every page, and an unavailable reading shows an em dash,
never ``0``. This module is the producer: one sampler thread per serving process reads the
SweatMeter collector every ``[telemetry] interval_ms`` and publishes a ``telemetry.sampled``
event on a MirrorWall broker. ``GET /system/telemetry/stream`` replays the latest sample to a new
client and follows the broker from there, so the bar is populated within one frame of connecting
and never shows a stale reading as a current one.

The payload is the generic shape MirrorWall's ``telemetry.js`` reads — host fields, a per-device
``gpus`` list and an ``unavailable_reasons`` map — with ``"unsupported"`` as the wire form of a
reading this machine cannot provide (ADR-0016 rule 4).
"""

from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING, Any

from baseaicore import UNSUPPORTED, is_supported
from baseaicore.timeutil import to_rfc3339
from mirrorwall import Event, EventBroker

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from contextlib import AbstractContextManager

    from mirrorwall import Subscription
    from sweatmeter import TelemetrySnapshot

__all__ = ["TELEMETRY_EVENT", "TELEMETRY_STREAM_ID", "TelemetrySampler", "telemetry_payload"]

logger = logging.getLogger(__name__)

TELEMETRY_EVENT = "telemetry.sampled"
TELEMETRY_STREAM_ID = "telemetry"

_HOST_FIELDS = (
    "cpu_percent",
    "cpu_temperature_c",
    "ram_used_bytes",
    "ram_available_bytes",
    "ram_total_bytes",
)
_GPU_FIELDS = (
    "utilization_percent",
    "temperature_c",
    "power_watts",
    "vram_used_bytes",
    "vram_total_bytes",
)


def _wire(value: object) -> Any:
    """A measurement's wire form: the number, or ``"unsupported"`` (ADR-0016 rule 4)."""
    if value is UNSUPPORTED or not is_supported(value):
        return "unsupported"
    return value


def telemetry_payload(snapshot: TelemetrySnapshot) -> dict[str, Any]:
    """Render a snapshot as the generic payload the telemetry bar reads.

    Args:
        snapshot: One SweatMeter observation.

    Returns:
        ``timestamp``, the host fields, ``gpus`` (one entry per device, in index order) and
        ``unavailable_reasons`` — the producer's own reasons where it gave any, so the bar's em
        dash can say why.
    """
    reasons_source = getattr(snapshot, "unavailable_reasons", None)
    reasons: dict[str, str] = dict(reasons_source) if isinstance(reasons_source, dict) else {}
    payload: dict[str, Any] = {
        "timestamp": to_rfc3339(snapshot.timestamp),
        "gpus": [
            {"index": gpu.index, **{name: _wire(getattr(gpu, name)) for name in _GPU_FIELDS}}
            for gpu in snapshot.gpus
        ],
        "unavailable_reasons": reasons,
    }
    for name in _HOST_FIELDS:
        payload[name] = _wire(getattr(snapshot, name, UNSUPPORTED))
    if not snapshot.gpus:
        payload["unavailable_reasons"].setdefault("gpu", "no GPU reported by SweatMeter")
    return payload


class TelemetrySampler:
    """Samples the collector on an interval and publishes each sample; also the event source.

    Thread-safe and stoppable. ``replay`` hands a new client the latest sample at once (when it
    has not already seen it), and ``subscribe`` follows the broker — the two halves MirrorWall's
    ``sse_response`` needs, so the same object is both producer and source.
    """

    def __init__(
        self,
        sample: Callable[[], TelemetrySnapshot | None],
        *,
        interval_seconds: float,
        broker: EventBroker | None = None,
    ) -> None:
        """Create a sampler over ``sample``, publishing every ``interval_seconds``."""
        self._sample = sample
        self._interval = max(interval_seconds, 0.05)
        self.broker = broker if broker is not None else EventBroker()
        self._latest: Event | None = None
        self._sequence = 0
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def latest(self) -> Event | None:
        """The most recent sample, or ``None`` before the first."""
        with self._lock:
            return self._latest

    def sample_once(self) -> Event | None:
        """Take one sample and publish it. ``None`` when the collector could not read."""
        snapshot = self._sample()
        if snapshot is None:
            return None
        with self._lock:
            self._sequence += 1
            event = Event(
                sequence=self._sequence,
                type=TELEMETRY_EVENT,
                payload={"sequence": self._sequence, "data": telemetry_payload(snapshot)},
            )
            self._latest = event
        self.broker.publish(TELEMETRY_STREAM_ID, event)
        return event

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.sample_once()
            except Exception:  # noqa: BLE001 — a failed sample skips a frame, never the thread
                logger.warning("telemetry.sample_failed", exc_info=True)
            self._stop.wait(self._interval)

    def start(self) -> None:
        """Start the sampler thread (idempotent)."""
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="loadcoach-telemetry", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Stop the sampler thread and wait for it."""
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=5.0)
            self._thread = None

    def replay(self, *, stream_id: str, after_sequence: int, limit: int) -> Sequence[Event]:
        """The latest sample, if the client has not seen it; the bar has no history to replay."""
        latest = self.latest
        if latest is None or latest.sequence <= after_sequence or limit <= 0:
            return []
        return [latest]

    def subscribe(self, *, stream_id: str) -> AbstractContextManager[Subscription]:
        """Follow the broker's telemetry stream."""
        return self.broker.subscribe(stream_id=TELEMETRY_STREAM_ID)
