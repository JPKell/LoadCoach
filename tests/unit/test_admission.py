"""The admission policy's pure pieces (queue §5, ADR-0027)."""

from __future__ import annotations

from datetime import UTC, datetime

from baseaicore import UNSUPPORTED
from sweatmeter import GpuSample, TelemetrySnapshot

from loadcoach.domain.admission import (
    Reservation,
    adjust_snapshot,
    classify_rejections,
    reserved_bytes_by_device,
    waiting_job_can_proceed,
)

GIB = 1024**3


def _snapshot(*gpus: tuple[int, int | None, int | None]) -> TelemetrySnapshot:
    return TelemetrySnapshot(
        timestamp=datetime(2026, 8, 29, tzinfo=UTC),
        gpus=tuple(
            GpuSample(
                index=index,
                vram_total_bytes=UNSUPPORTED if total is None else total,
                vram_used_bytes=UNSUPPORTED if used is None else used,
            )
            for index, total, used in gpus
        ),
    )


def test_reservations_sum_within_a_device_and_never_across() -> None:
    totals = reserved_bytes_by_device(
        [Reservation("a", 0, 3 * GIB), Reservation("b", 0, 2 * GIB), Reservation("c", 1, 4 * GIB)]
    )
    assert totals == {0: 5 * GIB, 1: 4 * GIB}


def test_adjust_snapshot_raises_used_by_reservations_and_lowers_it_by_evictable_per_device() -> (
    None
):
    snapshot = _snapshot((0, 16 * GIB, 4 * GIB), (1, 16 * GIB, 10 * GIB))
    adjusted = adjust_snapshot(snapshot, reserved={0: 5 * GIB}, evictable={1: 6 * GIB})
    assert adjusted is not None
    assert adjusted.gpus[0].vram_used_bytes == 9 * GIB
    assert adjusted.gpus[1].vram_used_bytes == 4 * GIB
    # The original is untouched; the arithmetic is clamped to the device.
    assert snapshot.gpus[0].vram_used_bytes == 4 * GIB
    clamped = adjust_snapshot(snapshot, reserved={0: 40 * GIB}, evictable={1: 40 * GIB})
    assert clamped is not None
    assert clamped.gpus[0].vram_used_bytes == 16 * GIB
    assert clamped.gpus[1].vram_used_bytes == 0


def test_adjust_snapshot_keeps_an_unreported_device_unreported() -> None:
    snapshot = _snapshot((0, 16 * GIB, None))
    adjusted = adjust_snapshot(snapshot, reserved={0: GIB})
    assert adjusted is not None
    assert adjusted.gpus[0].vram_used_bytes is UNSUPPORTED
    assert adjust_snapshot(None, reserved={}) is None


def test_classify_defers_only_when_a_rejection_is_resource_shaped() -> None:
    rejected = [
        {"canonical_id": "m1", "reason": "capability_unsupported", "detail": {}},
        {
            "canonical_id": "m2",
            "reason": "insufficient_vram",
            "detail": {
                "estimated_bytes": 9 * GIB,
                "headroom_bytes": GIB // 2,
                "free_bytes_by_gpu": {"0": 6 * GIB, "1": None},
                "estimate": {"unknown_reason": None},
            },
        },
        {
            "canonical_id": "m3",
            "reason": "insufficient_vram",
            "detail": {
                "estimated_bytes": None,
                "headroom_bytes": GIB // 2,
                "free_bytes_by_gpu": {"0": 6 * GIB},
                "estimate": {"unknown_reason": "size_bytes_unknown"},
            },
        },
    ]
    verdict = classify_rejections(rejected)
    assert verdict.defer is True
    assert verdict.required_bytes == 9 * GIB
    assert verdict.headroom_bytes == GIB // 2
    assert verdict.free_bytes_by_gpu == {"0": 6 * GIB, "1": None}
    assert verdict.unknown_reasons == ("size_bytes_unknown",)
    assert verdict.candidates == ("m2", "m3")
    assert verdict.as_json()["candidates"] == ["m2", "m3"]

    permanent = classify_rejections(rejected[:1])
    assert permanent.defer is False and permanent.required_bytes is None


def test_waiting_job_proceeds_only_by_admissions_own_rule() -> None:
    free = {0: 8 * GIB, 1: 12 * GIB}
    # Some device has room: proceed. Devices are checked independently, never summed.
    assert waiting_job_can_proceed(
        required_bytes=11 * GIB, headroom_bytes=GIB, free_bytes_by_gpu=free, resident_on=frozenset()
    )
    assert not waiting_job_can_proceed(
        required_bytes=15 * GIB, headroom_bytes=GIB, free_bytes_by_gpu=free, resident_on=frozenset()
    )
    # An unknown requirement never proceeds on hope — only on residency.
    assert not waiting_job_can_proceed(
        required_bytes=None, headroom_bytes=GIB, free_bytes_by_gpu=free, resident_on=frozenset()
    )
    assert waiting_job_can_proceed(
        required_bytes=None, headroom_bytes=GIB, free_bytes_by_gpu=free, resident_on=frozenset({1})
    )
    # Resident on a device that is no longer visible does not count.
    assert not waiting_job_can_proceed(
        required_bytes=None, headroom_bytes=GIB, free_bytes_by_gpu=free, resident_on=frozenset({7})
    )
