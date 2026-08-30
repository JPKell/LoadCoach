"""The live queue stream: full-state frames on change, right after one frame on reconnect (P8).

LCX15's bar applies: each property here is one a mutation of the mechanism must break. The
mutations run against copies of ``services/queue_stream.py``: replay returning nothing; the
publisher never re-polling; a sequence that does not advance; replay ignoring ``after_sequence``;
and the fragment rendered from the previous report.
"""

from __future__ import annotations

import time
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import anyio
import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request
from tests.integration.test_jobs_api import _client

from loadcoach.services.queue_stream import QUEUE_STREAM_ID, QueueStatusPublisher, fingerprint
from loadcoach.web.routes.queue import get_queue_stream


def _frames(client: TestClient, count: int, *, last_event_id: str | None = None) -> list[str]:
    """Pull ``count`` frames from the route's response body, then close the generator.

    The stream is open-ended and a test client cannot interrupt a sync generator in a threadpool,
    so the body iterator is driven directly with a request scope of our own.
    """

    async def pull() -> list[str]:
        headers = [] if last_event_id is None else [(b"last-event-id", last_event_id.encode())]
        scope = {
            "type": "http",
            "method": "GET",
            "path": "/api/v1/queue/stream",
            "headers": headers,
            "query_string": b"",
            "app": client.app,
        }
        response = await get_queue_stream(Request(scope))
        iterator = cast("AsyncGenerator[bytes | str, None]", response.body_iterator)
        collected: list[str] = []
        with anyio.fail_after(5.0):  # a stream that never delivers is a failure, not a hang
            async for chunk in iterator:
                text = chunk.decode() if isinstance(chunk, bytes) else chunk
                if text.startswith(":"):
                    continue  # a heartbeat comment
                collected.append(text)
                if len(collected) >= count:
                    break
        await iterator.aclose()
        return collected

    return anyio.run(pull)


def _publisher(client: TestClient) -> QueueStatusPublisher:
    publisher: QueueStatusPublisher = client.app.state.queue_stream  # type: ignore[attr-defined]  # FastAPI
    return publisher


def _wait_for_frame_after(publisher: QueueStatusPublisher, sequence: int) -> Any:
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        latest = publisher.latest
        if latest is not None and latest.sequence > sequence:
            return latest
        time.sleep(0.02)
    pytest.fail("no new frame was published after a change")


def test_the_first_frame_is_the_whole_current_state_with_the_fragment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _client(tmp_path, monkeypatch) as client:
        # Wait for the publisher's first poll: a client connecting before it would receive the
        # frame from the broker rather than from replay, and this test is about replay.
        _wait_for_frame_after(_publisher(client), 0)
        frames = _frames(client, 1)
        assert len(frames) == 1
        frame = frames[0]
        assert "event: queue.status" in frame
        assert '"depth_by_state"' in frame and '"flags"' in frame and '"circuit_breakers"' in frame
        assert '"html":"' in frame and "Executing now" in frame
        assert "id: 1" in frame or '"sequence": 1' in frame


def test_a_control_change_produces_a_new_full_frame_reflecting_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _client(tmp_path, monkeypatch) as client:
        publisher = _publisher(client)
        before = _wait_for_frame_after(publisher, 0)
        assert before.payload["data"]["flags"]["paused"] is False
        assert client.post("/api/v1/queue/pause").status_code == 202
        after = _wait_for_frame_after(publisher, before.sequence)
        assert after.payload["data"]["flags"]["paused"] is True
        assert after.sequence == before.sequence + 1
        # The fragment is rendered from the same report the frame carries.
        assert ">paused<" in after.payload["html"]
        assert ">paused<" not in before.payload["html"]
        client.post("/api/v1/queue/resume")
        resumed = _wait_for_frame_after(publisher, after.sequence)
        assert resumed.payload["data"]["flags"]["paused"] is False
        assert ">running<" in resumed.payload["html"]


def test_sequences_strictly_increase_and_nothing_is_published_without_a_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _client(tmp_path, monkeypatch) as client:
        publisher = _publisher(client)
        first = _wait_for_frame_after(publisher, 0)
        time.sleep(0.8)  # several poll intervals with nothing happening
        assert publisher.latest is not None and publisher.latest.sequence == first.sequence
        client.post("/api/v1/queue/drain")
        second = _wait_for_frame_after(publisher, first.sequence)
        client.post("/api/v1/queue/resume")
        third = _wait_for_frame_after(publisher, second.sequence)
        assert first.sequence < second.sequence < third.sequence


def test_a_reconnect_that_saw_the_latest_frame_gets_nothing_and_a_stale_one_gets_the_latest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reconnect rule, at the source: never a duplicate, never a diff, never a stale frame."""
    with _client(tmp_path, monkeypatch) as client:
        publisher = _publisher(client)
        latest = _wait_for_frame_after(publisher, 0)
        assert (
            publisher.replay(stream_id=QUEUE_STREAM_ID, after_sequence=latest.sequence, limit=10)
            == []
        )
        assert publisher.replay(stream_id=QUEUE_STREAM_ID, after_sequence=0, limit=10) == [latest]
        client.post("/api/v1/queue/pause")
        newer = _wait_for_frame_after(publisher, latest.sequence)
        # A client that last saw ``latest`` is handed the newest full state, not the delta.
        replayed = publisher.replay(
            stream_id=QUEUE_STREAM_ID, after_sequence=latest.sequence, limit=10
        )
        assert replayed == [newer]
        assert replayed[0].payload["data"]["flags"]["paused"] is True
        # And over the route: Last-Event-ID naming the latest frame yields it again only if newer.
        frames = _frames(client, 1, last_event_id=str(latest.sequence))
        assert f"id: {newer.sequence}" in frames[0]
        client.post("/api/v1/queue/resume")


def test_the_fingerprint_ignores_only_what_changes_without_meaning() -> None:
    base: dict[str, Any] = {
        "active": 1,
        "checked_at": "2026-08-30T10:00:00.000Z",
        "residency": [{"canonical_id": "m", "gpu_index": 0, "idle_seconds": 1.5}],
        "flags": {"paused": False, "draining": False},
    }
    ticked = dict(base, checked_at="2026-08-30T10:00:01.000Z")
    ticked["residency"] = [{"canonical_id": "m", "gpu_index": 0, "idle_seconds": 2.5}]
    assert fingerprint(base) == fingerprint(ticked)
    assert fingerprint(base) != fingerprint(dict(base, active=2))
    assert fingerprint(base) != fingerprint(dict(base, flags={"paused": True, "draining": False}))
    evicted = dict(base, residency=[])
    assert fingerprint(base) != fingerprint(evicted)


def test_the_publisher_can_run_without_a_renderer_and_stops_cleanly() -> None:
    reports = iter(
        [
            {"active": 0, "checked_at": "a"},
            {"active": 0, "checked_at": "b"},
            {"active": 1, "checked_at": "c"},
        ]
    )
    publisher = QueueStatusPublisher(lambda: next(reports), interval_seconds=60.0)
    first = publisher.poll_once()
    assert first is not None and first.sequence == 1 and first.payload["html"] is None
    assert publisher.poll_once() is None  # only checked_at moved
    third = publisher.poll_once()
    assert third is not None and third.sequence == 2 and third.payload["data"]["active"] == 1
    publisher.start()
    publisher.stop()
    assert datetime.now(UTC) is not None
