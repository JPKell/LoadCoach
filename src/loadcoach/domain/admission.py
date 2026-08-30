"""loadcoach.domain.admission — the admission policy around P3's estimator (queue §5, ADR-0027).

P3 built ``estimate_vram`` and ``device_fits`` as pure functions and deliberately no policy. This
module is that policy, and it is built *around* them rather than instead of them:

* **Reservations.** Above ``max_concurrent_jobs = 1`` the aggregate check is per device: the
  estimates of every job already admitted to or executing on GPU 0 are subtracted from GPU 0's
  free memory before a new candidate is evaluated, never from the machine's total, and never
  summed across devices. A model that is already resident on the device is not reserved again —
  the telemetry snapshot already counts its memory.
* **Evictable residents.** A model that is resident but idle can be unloaded to make room (queue
  §6: "before loading a model that does not fit alongside the resident set, the least-recently-used
  resident model is unloaded"), so its memory counts as available on its device — and only there.
* **Deferral, not failure.** When routing rejects every candidate and at least one rejection is
  resource-shaped, the job waits with the numbers recorded; when no rejection is, nothing will
  ever free and the job fails (ADR-0036 §3).
* **Never optimistic.** An unknown estimate does not fit unless the model is already resident
  (queue §5); a resumption check applies the same rule.

Pure: values in, values out. The snapshot arithmetic returns a new snapshot rather than mutating
the one telemetry produced.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Final

from baseaicore import UNSUPPORTED, is_supported

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from sweatmeter import TelemetrySnapshot

__all__ = [
    "RESOURCE_REJECTION_REASONS",
    "AdmissionVerdict",
    "Reservation",
    "adjust_snapshot",
    "classify_rejections",
    "reserved_bytes_by_device",
    "waiting_job_can_proceed",
]

RESOURCE_REJECTION_REASONS: Final[frozenset[str]] = frozenset(
    {"insufficient_vram", "insufficient_ram"}
)
"""Rejection reasons that a resource change can fix. Anything else is permanent for this job."""


@dataclass(frozen=True, slots=True)
class Reservation:
    """Memory one in-flight job is expected to hold on one device."""

    job_id: str
    gpu_index: int
    bytes: int


def reserved_bytes_by_device(reservations: Iterable[Reservation]) -> dict[int, int]:
    """Sum reservations **within** each device — never across devices (ADR-0027 §2)."""
    totals: dict[int, int] = {}
    for reservation in reservations:
        totals[reservation.gpu_index] = totals.get(reservation.gpu_index, 0) + max(
            reservation.bytes, 0
        )
    return totals


def adjust_snapshot(
    snapshot: TelemetrySnapshot | None,
    *,
    reserved: Mapping[int, int],
    evictable: Mapping[int, int] | None = None,
) -> TelemetrySnapshot | None:
    """Return the snapshot admission should evaluate against, device by device.

    Each device's used memory is raised by what is reserved on it and lowered by what an idle
    resident model on it could give back. A device whose usage telemetry never reported stays
    unreported: a number cannot be derived from an absence (ADR-0016).

    Args:
        snapshot: The telemetry observation, or ``None`` when none could be taken.
        reserved: Bytes reserved per device by in-flight jobs.
        evictable: Bytes held per device by idle resident models that policy may unload.

    Returns:
        A new snapshot, or ``None`` when the input was ``None``.
    """
    if snapshot is None:
        return None
    evictable = evictable or {}
    gpus = []
    for gpu in snapshot.gpus:
        used = gpu.vram_used_bytes
        total = gpu.vram_total_bytes
        if not is_supported(used) or not is_supported(total):
            gpus.append(replace(gpu, vram_used_bytes=UNSUPPORTED))
            continue
        adjusted = int(used) + reserved.get(gpu.index, 0) - evictable.get(gpu.index, 0)
        gpus.append(replace(gpu, vram_used_bytes=min(max(adjusted, 0), int(total))))
    return replace(snapshot, gpus=tuple(gpus))


@dataclass(frozen=True, slots=True)
class AdmissionVerdict:
    """What to do with a job routing could not place.

    Attributes:
        defer: ``True`` to move the job to ``waiting_resources``; ``False`` to fail it.
        required_bytes: The smallest estimate among the resource-rejected candidates, or
            ``None`` when every such estimate was unknown.
        headroom_bytes: The per-device reserve the check applied.
        free_bytes_by_gpu: What each device had free at the time, as the rejection saw it.
        unknown_reasons: Why estimates could not be produced, when they could not.
        candidates: The resource-rejected candidates' canonical IDs.
    """

    defer: bool
    required_bytes: int | None
    headroom_bytes: int | None
    free_bytes_by_gpu: dict[str, int | None]
    unknown_reasons: tuple[str, ...]
    candidates: tuple[str, ...]

    def as_json(self) -> dict[str, Any]:
        """The deferral record an event and a resumption check read."""
        return {
            "required_bytes": self.required_bytes,
            "headroom_bytes": self.headroom_bytes,
            "free_bytes_by_gpu": dict(self.free_bytes_by_gpu),
            "unknown_reasons": list(self.unknown_reasons),
            "candidates": list(self.candidates),
        }


def classify_rejections(rejected: Sequence[Mapping[str, Any]]) -> AdmissionVerdict:
    """Decide between deferral and failure from an explanation's ``rejected`` list.

    Args:
        rejected: Routing §8's rejected entries: each with ``reason``, ``detail`` and
            ``canonical_id``.

    Returns:
        The verdict. ``defer`` is ``True`` when at least one rejection is resource-shaped.
    """
    required: int | None = None
    headroom: int | None = None
    free: dict[str, int | None] = {}
    unknown: list[str] = []
    candidates: list[str] = []
    for item in rejected:
        if item.get("reason") not in RESOURCE_REJECTION_REASONS:
            continue
        candidates.append(str(item.get("canonical_id")))
        detail = item.get("detail") or {}
        estimated = detail.get("estimated_bytes")
        if isinstance(estimated, int):
            required = estimated if required is None else min(required, estimated)
        estimate = detail.get("estimate") or {}
        reason = estimate.get("unknown_reason") if isinstance(estimate, Mapping) else None
        if isinstance(reason, str):
            unknown.append(reason)
        if isinstance(detail.get("headroom_bytes"), int):
            headroom = int(detail["headroom_bytes"])
        by_gpu = detail.get("free_bytes_by_gpu")
        if isinstance(by_gpu, Mapping):
            for index, value in by_gpu.items():
                free[str(index)] = value if isinstance(value, int) else None
    return AdmissionVerdict(
        defer=bool(candidates),
        required_bytes=required,
        headroom_bytes=headroom,
        free_bytes_by_gpu=free,
        unknown_reasons=tuple(dict.fromkeys(unknown)),
        candidates=tuple(candidates),
    )


def waiting_job_can_proceed(
    *,
    required_bytes: int | None,
    headroom_bytes: int,
    free_bytes_by_gpu: Mapping[int, int | None],
    resident_on: frozenset[int],
) -> bool:
    """Whether a deferred job is worth re-queuing now (queue §5's re-evaluation).

    Devices are evaluated independently. A known requirement proceeds when **some** device has
    ``required + headroom`` free; an unknown one proceeds only when the model is already resident
    on a visible device — the same rule admission applies, so resumption cannot be more
    optimistic than admission and cause a claim-defer-claim thrash.

    Args:
        required_bytes: The deferral's recorded requirement, or ``None`` when unknown.
        headroom_bytes: The per-device reserve.
        free_bytes_by_gpu: Effective free memory per device now (reservations applied).
        resident_on: Devices the job's model is currently resident on.
    """
    visible = set(free_bytes_by_gpu)
    if resident_on & visible:
        return True
    if required_bytes is None:
        return False
    return any(
        free is not None and required_bytes + headroom_bytes <= free
        for free in free_bytes_by_gpu.values()
    )
