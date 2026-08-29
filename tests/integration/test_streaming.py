"""POST /generate/stream: frame shapes, ordering, the terminal event, and reconnection.

The frame-shape test is the one that matters most here. Every frame carries the SetSpec event
envelope except `token`, which is bare — and that exception is deliberate, documented and narrow
(ADR-0025 §3). A test that only checked "the token frames parse" would pass for an implementation
that enveloped them too.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
import setspec
from fastapi.testclient import TestClient
from modelrack.testing import FakeFailure, FakeFailureMode, FakeGeneration, FakeProvider, FakeScript
from setspec import SchemaVersion
from tests.integration.test_generate import NOW, _model

from loadcoach.config import load_settings
from loadcoach.services.database import Database, ensure_ready
from loadcoach.services.models import discover_models
from loadcoach.services.task_profiles import import_task_profiles, read_task_profiles_file
from loadcoach.web.app import create_app


@contextmanager
def _client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, script: FakeScript
) -> Iterator[TestClient]:
    """An entered TestClient whose provider is this test's scripted fake.

    The lifespan builds its own provider from settings, which would be a default fake with a
    default script; replacing ``app.state.provider`` after entering is what makes the scripted
    generations reach the executor by the real code path rather than through a patched import.
    """
    url = f"sqlite:///{tmp_path / 'stream.sqlite3'}"
    monkeypatch.setenv("LOADCOACH_STORAGE__DATABASE_URL", url)
    monkeypatch.setenv("LOADCOACH_PROVIDER__KIND", "fake")
    settings = load_settings().settings
    database = Database.from_url(url)
    ensure_ready(database, auto_migrate=True)
    import_task_profiles(database, read_task_profiles_file(), now=NOW)
    discover_models(database, FakeProvider(script), now=NOW)
    database.close()

    app = create_app(settings)
    with TestClient(app, base_url="http://localhost") as client:
        app.state.provider = FakeProvider(script)
        yield client


@pytest.fixture
def script() -> FakeScript:
    return FakeScript(
        models=(_model(),),
        generations=(
            FakeGeneration(
                text="Local inference keeps data on the machine.",
            ),
        ),
        repeat_final_generation=True,
    )


def _frames(text: str) -> list[tuple[str | None, str | None, str | None]]:
    """Split an SSE body into ``(id, event, data)`` triples, ignoring comment frames."""
    frames: list[tuple[str | None, str | None, str | None]] = []
    for block in text.split("\n\n"):
        if not block.strip() or block.lstrip().startswith(":"):
            continue
        identifier = event = data = None
        for line in block.splitlines():
            if line.startswith("id: "):
                identifier = line[4:]
            elif line.startswith("event: "):
                event = line[7:]
            elif line.startswith("data: "):
                data = line[6:]
        frames.append((identifier, event, data))
    return frames


def _stream(client: TestClient, body: dict[str, Any], **kwargs: Any) -> str:
    """Read a whole stream to its natural end.

    Deliberately not "read until I see a result frame and then break": that would pass for an
    implementation whose stream never closes, which is exactly the defect this endpoint had
    before MirrorWall gained `terminal_events`.
    """
    with client.stream("POST", "/api/v1/generate/stream", json=body, **kwargs) as response:
        assert response.status_code == 200, response.read()
        assert response.headers["content-type"].startswith("text/event-stream")
        return "".join(response.iter_text())


def test_the_stream_carries_routing_then_tokens_then_a_terminal_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, script: FakeScript
) -> None:
    with _client(tmp_path, monkeypatch, script) as client:
        body = _stream(client, {"task": "general.chat", "prompt": "Tell me about local inference."})
        frames = _frames(body)
        events = [event for _, event, _ in frames]

        assert events[0] == "routing"
        assert "token" in events
        assert events[-1] == "result"
    # Sequences are strictly increasing, which is what a reconnect resumes from.
    sequences = [int(identifier) for identifier, _, _ in frames if identifier]
    assert sequences == sorted(sequences)
    assert len(sequences) == len(set(sequences))


def test_every_frame_except_token_parses_through_load_envelope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, script: FakeScript
) -> None:
    """ADR-0025 §3, asserted in both directions: enveloped, and the exception is only `token`."""
    with _client(tmp_path, monkeypatch, script) as client:
        body = _stream(client, {"task": "general.chat", "prompt": "hello"})
        frames = _frames(body)
        assert frames

        token_frames = 0
        for _, event, data in frames:
            assert data is not None
            if event == "token":
                token_frames += 1
                parsed = json.loads(data)
                assert set(parsed) == {"delta", "index"}
                with pytest.raises(setspec.ValidationError):
                    setspec.load_envelope(
                        data, expect="event.envelope", supported=[SchemaVersion(1, 0)]
                    )
                continue
            envelope = setspec.load_envelope(
                data, expect="event.envelope", supported=[SchemaVersion(1, 0)]
            )
            assert envelope.generator.name == "loadcoach"
            assert envelope.payload["type"] == event
        assert token_frames >= 1, (
            "no token frame was produced, so the exception was never exercised"
        )


def test_token_deltas_arrive_in_order_and_reassemble_to_the_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, script: FakeScript
) -> None:
    with _client(tmp_path, monkeypatch, script) as client:
        body = _stream(client, {"task": "general.chat", "prompt": "hello"})
        frames = _frames(body)

        deltas = [json.loads(data) for _, event, data in frames if event == "token" and data]
        assert [delta["index"] for delta in deltas] == list(range(len(deltas)))

        result = next(data for _, event, data in frames if event == "result" and data)
        payload = setspec.load_envelope(
            result, expect="event.envelope", supported=[SchemaVersion(1, 0)]
        ).payload
    assert "".join(delta["delta"] for delta in deltas) == payload["output"]["text"]


def test_the_result_frame_names_the_execution_subject(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, script: FakeScript
) -> None:
    """Spec §9: every response names its selected model, profile hash, served context and GPU."""
    with _client(tmp_path, monkeypatch, script) as client:
        body = _stream(client, {"task": "general.chat", "prompt": "hello"})
        result = next(data for _, event, data in _frames(body) if event == "result" and data)
        payload = setspec.load_envelope(
            result, expect="event.envelope", supported=[SchemaVersion(1, 0)]
        ).payload
    model = payload["model"]
    assert model["canonical_id"].startswith("fake/")
    assert model["runtime_profile_hash"]
    assert model["served_context"] > 0
    assert model["served_context_source"] in {"configured", "reported", "assumed"}
    assert "target_gpu_index" in model


def test_a_failing_generation_ends_with_a_terminal_error_frame(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    failing = FakeScript(
        models=(_model(),),
        generations=(FakeGeneration(failure=FakeFailure(mode=FakeFailureMode.UNAVAILABLE)),),
        repeat_final_generation=True,
    )
    with _client(tmp_path, monkeypatch, failing) as client:
        body = _stream(client, {"task": "general.chat", "prompt": "hello"})
        frames = _frames(body)
        assert frames[-1][1] == "error"
        payload = setspec.load_envelope(
            frames[-1][2] or "", expect="event.envelope", supported=[SchemaVersion(1, 0)]
        ).payload
    assert payload["code"] == "ALL_CANDIDATES_FAILED"
    assert payload["attempts"]


def test_a_reconnect_with_the_same_idempotency_key_resumes_without_repeating(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, script: FakeScript
) -> None:
    """Acceptance criterion 2: streaming survives a reconnect.

    A browser that refreshes mid-stream reconnects with the same idempotency key and the
    ``Last-Event-ID`` of the last frame it saw. It must attach to the execution already running
    (api.md §4: a repeated key replays rather than re-executing), and receive exactly the frames
    it missed — no gap, no duplicate, and no second generation.
    """
    with _client(tmp_path, monkeypatch, script) as client:
        body = {"task": "general.chat", "prompt": "hello", "idempotency_key": "01JRECONNECT"}
        first = _frames(_stream(client, body))
        assert first, "the first connection produced nothing"
        # Pretend the browser dropped after the second frame.
        cut_after = 2
        seen = [int(identifier) for identifier, _, _ in first if identifier][:cut_after]

        resumed = _frames(_stream(client, body, headers={"Last-Event-ID": str(seen[-1])}))

    resumed_sequences = [int(identifier) for identifier, _, _ in resumed if identifier]
    all_sequences = [int(identifier) for identifier, _, _ in first if identifier]
    assert resumed_sequences, "the resumed stream produced nothing"
    # Gap-free: it continues exactly where the client stopped.
    assert min(resumed_sequences) == seen[-1] + 1
    # Duplicate-free, and complete: the two halves reassemble into the original stream.
    assert seen + resumed_sequences == all_sequences
    assert resumed[-1][1] == "result", "the reconnect did not receive the terminal frame"


def test_a_reconnect_after_completion_replays_rather_than_re_executing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, script: FakeScript
) -> None:
    with _client(tmp_path, monkeypatch, script) as client:
        body = {"task": "general.chat", "prompt": "hello", "idempotency_key": "01JAGAIN"}
        first = _frames(_stream(client, body))
        again = _frames(_stream(client, body))

    assert [event for _, event, _ in first] == [event for _, event, _ in again]
    first_result = next(data for _, event, data in first if event == "result" and data)
    again_result = next(data for _, event, data in again if event == "result" and data)
    assert first_result is not None
    assert again_result is not None
    first_job = setspec.load_envelope(
        first_result, expect="event.envelope", supported=[SchemaVersion(1, 0)]
    ).payload["job_id"]
    again_job = setspec.load_envelope(
        again_result, expect="event.envelope", supported=[SchemaVersion(1, 0)]
    ).payload["job_id"]
    assert first_job == again_job, "the repeated key executed a second time"


def test_a_stale_last_event_id_on_a_new_execution_does_not_hang_the_stream(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, script: FakeScript
) -> None:
    """A POST with no key is a new execution with its own sequence space.

    Honouring a foreign ``Last-Event-ID`` there would skip every frame the new stream produces
    and leave the connection waiting for a terminal event that had already gone past.
    """
    with _client(tmp_path, monkeypatch, script) as client:
        frames = _frames(
            _stream(
                client,
                {"task": "general.chat", "prompt": "hello"},
                headers={"Last-Event-ID": "99999"},
            )
        )
    assert frames
    assert frames[0][1] == "routing"
    assert frames[-1][1] == "result"


def test_a_malformed_body_is_refused_before_any_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, script: FakeScript
) -> None:
    with _client(tmp_path, monkeypatch, script) as client:
        both = client.post(
            "/api/v1/generate", json={"task": "general.chat", "prompt": "a", "messages": []}
        )
        neither = client.post("/api/v1/generate", json={"task": "general.chat"})
        assert both.status_code == 400
        assert both.json()["error"]["code"] == "VALIDATION_ERROR"
        assert neither.status_code == 400


def test_post_generate_returns_the_documented_body(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, script: FakeScript
) -> None:
    """Acceptance criterion 1, over HTTP."""
    with _client(tmp_path, monkeypatch, script) as client:
        response = client.post(
            "/api/v1/generate",
            json={"task": "content.article_draft", "prompt": "Write about local inference."},
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert set(body) >= {
            "job_id",
            "status",
            "output",
            "reasoning",
            "model",
            "routing",
            "usage",
            "timing",
            "validation",
            "attempts",
            "degradations",
        }
    assert body["output"]["text"]
    assert re.fullmatch(r"[0-9A-HJKMNP-TV-Z]{26}", body["job_id"])
    assert body["timing"]["provider_ms"] >= 0
    assert body["timing"]["loadcoach_overhead_ms"] >= 0
