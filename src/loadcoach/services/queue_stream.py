"""loadcoach.services.queue_stream — the live queue page's producer (dev-plan P8).

One publisher thread per serving process polls queue §11's report and publishes a
``queue.status`` frame whenever it changes. Every frame is the **whole** current state — the
report, and the page fragment rendered from that same report — never a diff, so a client that
reconnects after any gap needs exactly one frame to be correct again, and a client that saw the
latest frame is sent nothing (P8's named failure mode is a live page that drifts after a
reconnect). Rendering is injected by the web layer; this module knows nothing about templates.
"""

from __future__ import annotations

import json
import logging
import threading
from typing import TYPE_CHECKING, Any

from mirrorwall import Event, EventBroker

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence
    from contextlib import AbstractContextManager

    from mirrorwall import Subscription

__all__ = ["QUEUE_STATUS_EVENT", "QUEUE_STREAM_ID", "QueueStatusPublisher", "fingerprint"]

logger = logging.getLogger(__name__)

QUEUE_STATUS_EVENT = "queue.status"
QUEUE_STREAM_ID = "queue"

_VOLATILE_KEYS = frozenset({"checked_at"})
_VOLATILE_ROW_KEYS = frozenset({"idle_seconds"})


def fingerprint(report: Mapping[str, Any]) -> str:
    """A stable digest of the report minus what changes on every poll without meaning anything.

    ``checked_at`` moves every second and a resident model's ``idle_seconds`` grows every poll;
    neither is a change a page needs a frame for. Everything else — depth, executions, flags,
    breakers, residency membership, throughput — is.
    """
    trimmed: dict[str, Any] = {}
    for key, value in report.items():
        if key in _VOLATILE_KEYS:
            continue
        if key == "residency" and isinstance(value, list):
            trimmed[key] = [
                {k: v for k, v in row.items() if k not in _VOLATILE_ROW_KEYS} for row in value
            ]
        else:
            trimmed[key] = value
    return json.dumps(trimmed, sort_keys=True, default=str)


class QueueStatusPublisher:
    """Polls the report, publishes full-state frames on change; also the SSE event source."""

    def __init__(
        self,
        report: Callable[[], Mapping[str, Any]],
        *,
        interval_seconds: float,
        render: Callable[[Mapping[str, Any]], str] | None = None,
        broker: EventBroker | None = None,
    ) -> None:
        """Create a publisher over ``report``, polling every ``interval_seconds``.

        Args:
            report: Builds queue §11's report now.
            interval_seconds: The poll cadence.
            render: Turns a report into the page fragment carried beside it, or ``None`` for
                data-only frames.
            broker: The broker to publish on; a fresh one when ``None``.
        """
        self._report = report
        self._interval = max(interval_seconds, 0.05)
        self._render = render
        self.broker = broker if broker is not None else EventBroker()
        self._latest: Event | None = None
        self._fingerprint: str | None = None
        self._sequence = 0
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def latest(self) -> Event | None:
        """The most recent frame, or ``None`` before the first poll."""
        with self._lock:
            return self._latest

    def poll_once(self) -> Event | None:
        """Build the report; publish a frame if it differs from the last one published."""
        report = dict(self._report())
        digest = fingerprint(report)
        with self._lock:
            if digest == self._fingerprint:
                return None
            self._sequence += 1
            self._fingerprint = digest
            event = Event(
                sequence=self._sequence,
                type=QUEUE_STATUS_EVENT,
                payload={
                    "sequence": self._sequence,
                    "data": report,
                    "html": None if self._render is None else self._render(report),
                },
            )
            self._latest = event
        self.broker.publish(QUEUE_STREAM_ID, event)
        return event

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.poll_once()
            except Exception:  # noqa: BLE001 — a failed poll skips a frame, never the thread
                logger.warning("queue_stream.poll_failed", exc_info=True)
            self._stop.wait(self._interval)

    def start(self) -> None:
        """Start the publisher thread (idempotent)."""
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="loadcoach-queue-stream", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        """Stop the publisher thread and wait for it."""
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=5.0)
            self._thread = None

    def replay(self, *, stream_id: str, after_sequence: int, limit: int) -> Sequence[Event]:
        """The latest full-state frame, unless the client has already seen it."""
        latest = self.latest
        if latest is None or latest.sequence <= after_sequence or limit <= 0:
            return []
        return [latest]

    def subscribe(self, *, stream_id: str) -> AbstractContextManager[Subscription]:
        """Follow the broker's queue stream."""
        return self.broker.subscribe(stream_id=QUEUE_STREAM_ID)
