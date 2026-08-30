"""The telemetry sampler: the bar's payload, ``"unsupported"`` on the wire, replay and follow."""

from __future__ import annotations

from datetime import UTC, datetime

from baseaicore import UNSUPPORTED
from sweatmeter import GpuSample, TelemetrySnapshot

from loadcoach.services.telemetry_stream import (
    TELEMETRY_EVENT,
    TELEMETRY_STREAM_ID,
    TelemetrySampler,
    telemetry_payload,
)

T0 = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)
GIB = 1024**3


def _snapshot(*, gpus: bool = True) -> TelemetrySnapshot:
    return TelemetrySnapshot(
        timestamp=T0,
        cpu_percent=22.5,
        ram_used_bytes=12 * GIB,
        ram_available_bytes=52 * GIB,
        ram_total_bytes=64 * GIB,
        gpus=(
            (
                GpuSample(
                    index=0, vram_total_bytes=48 * GIB, vram_used_bytes=1 * GIB, power_watts=41.0
                ),
            )
            if gpus
            else ()
        ),
    )


def test_payload_carries_numbers_and_unsupported_never_zero() -> None:
    payload = telemetry_payload(_snapshot())
    assert payload["cpu_percent"] == 22.5
    assert payload["ram_used_bytes"] == 12 * GIB and payload["ram_total_bytes"] == 64 * GIB
    assert payload["cpu_temperature_c"] == "unsupported"  # not measured: not 0
    gpu = payload["gpus"][0]
    assert gpu["index"] == 0 and gpu["vram_total_bytes"] == 48 * GIB
    assert gpu["power_watts"] == 41.0
    assert gpu["utilization_percent"] == "unsupported" and gpu["temperature_c"] == "unsupported"
    assert payload["timestamp"] == "2026-08-30T12:00:00.000Z"
    assert "gpu" not in payload["unavailable_reasons"]


def test_payload_names_why_there_is_no_gpu() -> None:
    payload = telemetry_payload(_snapshot(gpus=False))
    assert payload["gpus"] == []
    assert payload["unavailable_reasons"]["gpu"] == "no GPU reported by SweatMeter"
    assert telemetry_payload(_snapshot())["ram_available_bytes"] != UNSUPPORTED


def test_sampler_replays_the_latest_sample_once_and_follows_the_broker() -> None:
    sampler = TelemetrySampler(_snapshot, interval_seconds=60.0)
    assert sampler.latest is None
    assert sampler.replay(stream_id=TELEMETRY_STREAM_ID, after_sequence=0, limit=10) == []
    first = sampler.sample_once()
    assert first is not None and first.sequence == 1 and first.type == TELEMETRY_EVENT
    assert first.payload["sequence"] == 1 and first.payload["data"]["cpu_percent"] == 22.5
    assert sampler.replay(stream_id=TELEMETRY_STREAM_ID, after_sequence=0, limit=10) == [first]
    assert sampler.replay(stream_id=TELEMETRY_STREAM_ID, after_sequence=1, limit=10) == []
    with sampler.subscribe(stream_id=TELEMETRY_STREAM_ID) as subscription:
        second = sampler.sample_once()
        assert second is not None and second.sequence == 2
        assert subscription.poll() == second
        assert subscription.poll() is None
    assert sampler.replay(stream_id=TELEMETRY_STREAM_ID, after_sequence=1, limit=10) == [second]


def test_a_collector_that_cannot_read_produces_no_frame() -> None:
    sampler = TelemetrySampler(lambda: None, interval_seconds=60.0)
    assert sampler.sample_once() is None and sampler.latest is None


def test_the_sampler_thread_starts_samples_and_stops() -> None:
    sampler = TelemetrySampler(_snapshot, interval_seconds=0.05)
    sampler.start()
    sampler.start()  # idempotent
    try:
        deadline = 200
        while sampler.latest is None and deadline:
            deadline -= 1
            import time

            time.sleep(0.01)
        assert sampler.latest is not None
    finally:
        sampler.stop()
    sequence = sampler.latest.sequence if sampler.latest else 0
    import time

    time.sleep(0.12)
    assert (sampler.latest.sequence if sampler.latest else 0) == sequence  # stopped: no new sample
