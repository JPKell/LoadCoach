"""A synchronous generation records the model it made resident.

Residency was an **input** to the synchronous endpoints and never an output. `/generate` routed
with the exception that lets an already-loaded model be chosen without re-checking VRAM, loaded a
model into the provider, and recorded nothing. The next request therefore saw an empty residency
map, could not apply the exception, and was refused `insufficient_vram` by the memory the previous
request was still holding.

On a single-GPU machine that fails the second stage of every multi-stage workflow, whichever task
profile it asks for, and the queue does not rescue it: routing refuses before a job ever reaches
`waiting_resources`. Observed against a caller whose stages use different task profiles seconds
apart — the first call succeeded, and every candidate for the second was rejected
`insufficient_vram`, including the model that had just answered and was still loaded.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from loadcoach.domain.routing.subject import ModelFacts
from loadcoach.services.execution import _make_resident  # noqa: PLC2701 — the unit under test


class _Recorder:
    """A residency tracker that records what it was asked to make resident."""

    def __init__(self, *, explode: bool = False) -> None:
        self.calls: list[dict[str, Any]] = []
        self._explode = explode

    def ensure_loaded(self, **kwargs: Any) -> None:
        if self._explode:
            message = "the tracker is unavailable"
            raise RuntimeError(message)
        self.calls.append(kwargs)


_Facts = ModelFacts(
    model_id="01MODEL",
    canonical_id="ollama/gpt-oss:20b@sha256:17052f91a42e0000000000000000000000000000000000000000000000000000",
    provider_kind="ollama",
    provider_model_name="gpt-oss:20b",
    artifact_digest="sha256:17052f91a42e0000000000000000000000000000000000000000000000000000",
)


class _Subject:
    facts = _Facts
    runtime_profile = object()


class _Candidate:
    subject = _Subject()
    target_gpu_index: int | None = 0
    estimated_vram_bytes = 14_483_113_306


def _now() -> datetime:
    return datetime(2026, 9, 1, tzinfo=UTC)


def test_the_chosen_candidate_is_made_resident() -> None:
    """The fix: the synchronous path writes the residency the next request reads."""
    recorder = _Recorder()
    _make_resident(
        _Candidate(),  # type: ignore[arg-type]  # a stand-in for RankedCandidate
        residency=recorder,
        now=_now,
        in_use_model_ids=frozenset(),
        vram_headroom_bytes=536_870_912,
        free_bytes_by_gpu={0: 2_998_927_360},
    )
    assert len(recorder.calls) == 1
    call = recorder.calls[0]
    assert call["canonical_id"] == _Facts.canonical_id
    assert call["gpu_index"] == 0
    assert call["required_bytes"] == 14_483_113_306
    assert call["free_bytes"] == 2_998_927_360
    assert call["headroom_bytes"] == 536_870_912


def test_a_machine_with_no_device_records_nothing() -> None:
    """There is no device to be resident on, and the provider loads on demand."""

    class _NoDevice(_Candidate):
        target_gpu_index = None

    recorder = _Recorder()
    _make_resident(_NoDevice(), residency=recorder, now=_now)  # type: ignore[arg-type]
    assert recorder.calls == []


def test_no_tracker_is_not_an_error() -> None:
    """`residency` is ``None`` outside a served application — in tests and in library use."""
    _make_resident(_Candidate(), residency=None, now=_now)  # type: ignore[arg-type]


def test_a_failing_tracker_does_not_fail_the_generation() -> None:
    """Residency is an optimisation and an eviction policy, not a precondition. A tracker that
    cannot record must not stop a generation the provider is perfectly able to serve."""
    _make_resident(
        _Candidate(),  # type: ignore[arg-type]
        residency=_Recorder(explode=True),
        now=_now,
    )


def test_in_use_models_are_passed_through_so_they_are_never_evicted() -> None:
    """`ensure_loaded` evicts to make room; a model an in-flight job holds must survive that."""
    recorder = _Recorder()
    held = frozenset({"01OTHER"})
    _make_resident(
        _Candidate(),  # type: ignore[arg-type]
        residency=recorder,
        now=_now,
        in_use_model_ids=held,
    )
    assert recorder.calls[0]["in_use_model_ids"] == held


def test_the_attempt_loop_actually_calls_it() -> None:
    """The guard on the guard.

    Every test above exercises `_make_resident` directly, so all of them pass with the call site
    deleted — which is exactly the state the code was in before this fix: the helper's job was
    understood and nobody did it. This walks the source of the loop that must call it.
    """
    import inspect

    from loadcoach.services import execution

    body = inspect.getsource(execution._execute_attempts)  # noqa: SLF001 — the point
    assert "_make_resident(" in body, (
        "the synchronous attempt loop no longer records residency; the next request will be "
        "refused insufficient_vram by memory this one is holding"
    )


def test_the_route_hands_the_tracker_to_the_context() -> None:
    """The other half of the wiring: a tracker the loop never receives is the same defect."""
    import inspect

    from loadcoach.web.routes import generate

    body = inspect.getsource(generate._context)  # noqa: SLF001 — the point
    assert "residency=residency" in body, "the synchronous context no longer carries the tracker"
