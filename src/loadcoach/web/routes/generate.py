"""loadcoach.web.routes.generate — POST /generate and POST /generate/stream (api.md §4).

Both endpoints run the same executor, which always calls the provider through
``Provider.stream()``. The difference between them is what reaches the caller, not how the model
is called — which is what makes cancellation, the idle timeout and partial-response preservation
uniform across the two (api.md §5).

The streaming endpoint is built on :func:`mirrorwall.sse_response`, so LoadCoach inherits the
replay/live handoff, the bounded subscriber queue, the heartbeat and the thread dispatch rather
than reimplementing them. Every frame carries the SetSpec event envelope except ``token``, which
is bare — the one documented exception (ADR-0025 §3), and MirrorWall's own frame formatter is what
enforces it.

Route handlers contain no business logic: each calls one service function and renders.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING, Any, cast

from baseaicore import new_id
from fastapi import APIRouter, Request
from mirrorwall import Event, EventBroker, sse_response
from modelrack import CancellationToken, Message, Role
from pydantic import BaseModel, ConfigDict, Field, model_validator
from setspec import GeneratorInfo

from loadcoach.__about__ import __version__
from loadcoach.domain.routing.subject import RuntimeOverrides
from loadcoach.services.execution import (
    ExecutionContext,
    GenerateRequest,
    StreamChunk,
    stream_execute,
)
from loadcoach.services.task_profiles import DEFAULT_SCHEMAS_DIR
from loadcoach.web.routes.routing import OverridesBody
from loadcoach.web.routing_support import current_snapshot, provider_facts_for, routing_policy_for

if TYPE_CHECKING:
    from collections.abc import Sequence

    from starlette.responses import StreamingResponse

__all__ = ["GENERATOR", "router"]

router = APIRouter(tags=["generation"])

GENERATOR = GeneratorInfo(name="loadcoach", version=__version__)
"""The envelope's generator: this application, not MirrorWall. An envelope's generator is what
makes a document self-describing months later, and MirrorWall did not produce these events."""

_CLIENT_NAME_HEADER = "x-client-name"

_TERMINAL_EVENTS = frozenset({"result", "error"})
"""api.md §4: the terminal event is always ``result`` or ``error``."""

_MAX_RETAINED_STREAMS = 64
"""How many finished streams stay attachable for a reconnect before the oldest is dropped.

Bounded on purpose: the replay log of a finished execution is held in memory so a browser that
reconnects gets its terminal ``result`` frame rather than a second generation, and an unbounded
cache of those would be a memory leak with a nice name. Durable replay from the persisted
``job_events`` is ``GET /jobs/{id}/stream``, which arrives with the queue in Phase 5."""


class MessageBody(BaseModel):
    """One turn of a caller-supplied transcript (api.md §4)."""

    model_config = ConfigDict(extra="forbid")

    role: str = Field(pattern="^(system|user|assistant|tool)$")
    content: str
    tool_call_id: str | None = Field(default=None)


class GenerateBody(BaseModel):
    """``POST /generate``'s request body.

    Exactly one of ``prompt`` (with an optional ``system``) or ``messages`` is supplied; supplying
    both is a ``VALIDATION_ERROR``, because a request that says two different things about what
    the model should see has no correct interpretation.
    """

    model_config = ConfigDict(extra="forbid")

    task: str
    system: str | None = Field(default=None)
    prompt: str | None = Field(default=None)
    messages: list[MessageBody] | None = Field(default=None)
    response_format: str | None = Field(default=None, pattern="^(text|json|json_schema)$")
    sampling: dict[str, Any] = Field(default_factory=dict)
    overrides: OverridesBody | None = Field(default=None)
    idempotency_key: str | None = Field(default=None, max_length=128)

    @model_validator(mode="after")
    def _exactly_one_form(self) -> GenerateBody:
        """Refuse a body that supplies both a prompt and a transcript, or neither."""
        if (self.prompt is None) == (self.messages is None):
            message = "supply exactly one of 'prompt' or 'messages'"
            raise ValueError(message)
        if self.messages is not None and self.system is not None:
            message = "'system' belongs with 'prompt'; put a system turn in 'messages' instead"
            raise ValueError(message)
        return self


def _source(request: Request) -> str:
    """Who is calling, for the idempotency scope and the job record.

    The authenticated token's name where there is one; otherwise the ``X-Client-Name`` header on a
    loopback bind; otherwise ``"anonymous"``. Never caller-asserted when a token exists — that is
    what makes an idempotency key scoped rather than forgeable.
    """
    token_name = getattr(request.state, "token_name", None)
    if isinstance(token_name, str) and token_name:
        return token_name
    header = request.headers.get(_CLIENT_NAME_HEADER, "").strip()
    return header[:64] if header else "anonymous"


def _to_request(body: GenerateBody, *, source: str, stream: bool) -> GenerateRequest:
    from baseaicore import RuntimeProfile

    overrides = body.overrides
    profile_override = (
        None
        if overrides is None or overrides.runtime_profile is None
        else RuntimeProfile(**overrides.runtime_profile.model_dump(exclude_none=True))
    )
    messages: tuple[Message, ...] | None = None
    if body.messages is not None:
        messages = tuple(
            Message(role=Role(turn.role), content=turn.content, tool_call_id=turn.tool_call_id)
            for turn in body.messages
        )
    return GenerateRequest(
        task=body.task,
        system=body.system,
        prompt=body.prompt,
        messages=messages,
        response_format=body.response_format,
        sampling=dict(body.sampling),
        overrides=None
        if overrides is None
        else RuntimeOverrides(
            model=overrides.model,
            runtime_profile=profile_override,
            disallow_fallback=overrides.disallow_fallback,
            require_evidence=overrides.require_evidence,
        ),
        source=source,
        idempotency_key=body.idempotency_key,
        stream=stream,
    )


def _stream_registry(app: Any) -> dict[str, _ExecutionSource]:
    """The application's in-flight and recently-finished streams, keyed by caller and key."""
    registry = getattr(app.state, "generate_streams", None)
    if registry is None:
        registry = {}
        app.state.generate_streams = registry
    return cast("dict[str, _ExecutionSource]", registry)


def _context(request: Request) -> ExecutionContext:
    app = request.app
    return ExecutionContext(
        provider=app.state.provider,
        provider_facts=provider_facts_for(app.state.provider),
        policy=routing_policy_for(app.state.settings),
        schemas_dir=DEFAULT_SCHEMAS_DIR,
        snapshot=current_snapshot(app),
        timeout_seconds=app.state.settings.execution.default_timeout_seconds,
    )


@router.post("/generate", summary="Route, execute and validate one generation")
def post_generate(request: Request, body: GenerateBody) -> dict[str, Any]:
    """Execute ``body`` synchronously and return the result with its routing metadata.

    ``def``, not ``async def`` (ADR-0003 §1): this handler touches the database and the provider,
    so Starlette runs it in the worker threadpool rather than on the event loop.

    Errors: ``TASK_PROFILE_NOT_FOUND``, ``NO_ELIGIBLE_MODEL`` (with every candidate and its
    rejection reason), ``ALL_CANDIDATES_FAILED`` (with every attempt and its error).
    """
    from loadcoach.services.execution import execute

    outcome = execute(
        request.app.state.database,
        _to_request(body, source=_source(request), stream=False),
        _context(request),
        cancel=CancellationToken(),
    )
    return outcome.as_json()


class _ExecutionSource:
    """A MirrorWall :class:`~mirrorwall.EventSource` over one in-flight execution.

    The executor is synchronous and runs in its own thread; this object is the bridge MirrorWall's
    ``sse_response`` consumes. ``replay`` returns from the events already produced, so a client
    that reconnects mid-generation resumes rather than restarting — the frames it missed are held
    here, in order, by sequence.
    """

    stream_id: str

    def __init__(self, broker: EventBroker, stream_id: str) -> None:
        """Create the source for one stream."""
        self._broker = broker
        self.stream_id = stream_id
        self._log: list[Event] = []
        self._lock = threading.Lock()
        self._finished = False

    @property
    def finished(self) -> bool:
        """Whether the execution has produced its terminal frame."""
        with self._lock:
            return self._finished

    def publish(self, chunk: StreamChunk) -> None:
        """Append an event to the replay log and fan it out. Called from the executor thread."""
        with self._lock:
            sequence = len(self._log) + 1
            event = Event(sequence=sequence, type=chunk.kind, payload=dict(chunk.payload))
            self._log.append(event)
            if chunk.kind in _TERMINAL_EVENTS:
                self._finished = True
        self._broker.publish(self.stream_id, event)

    def finish(self) -> None:
        """Guarantee a terminal frame, even if the producer died without emitting one."""
        with self._lock:
            if self._finished:
                return
        self.publish(
            StreamChunk(
                "error",
                {
                    "code": "INTERNAL_ERROR",
                    "message": "The execution ended without producing a result.",
                },
            )
        )

    def replay(self, *, stream_id: str, after_sequence: int, limit: int) -> Sequence[Event]:
        """Return up to ``limit`` already-produced events after ``after_sequence``."""
        with self._lock:
            return [event for event in self._log if event.sequence > after_sequence][:limit]

    def subscribe(self, *, stream_id: str) -> Any:
        """Open a live subscription to this execution."""
        return self._broker.subscribe(stream_id=stream_id)


def _drive(
    source: _ExecutionSource,
    database: Any,
    request: GenerateRequest,
    context: ExecutionContext,
    cancel: CancellationToken,
) -> None:
    """Run the execution to completion in a worker thread, publishing each chunk as it happens."""
    try:
        stream_execute(database, request, context, on_chunk=source.publish, cancel=cancel)
    except Exception as exc:  # noqa: BLE001 — the stream's terminal frame is the report
        source.publish(StreamChunk("error", {"code": "INTERNAL_ERROR", "message": str(exc)}))
    finally:
        # A stream whose producer died without a terminal frame would poll for ever; this is the
        # backstop that guarantees one, whatever went wrong above.
        source.finish()


@router.post("/generate/stream", summary="Stream one generation as it is produced")
async def post_generate_stream(request: Request, body: GenerateBody) -> StreamingResponse:
    """Execute ``body`` and stream the routing decision, tokens and terminal result.

    ``async def`` (ADR-0003 §2): this handler only streams. The execution itself runs in a worker
    thread, and every call MirrorWall's ``sse_response`` makes back into the event source is
    dispatched with ``anyio.to_thread.run_sync`` by that package — so nothing here can put a
    blocking call on the event loop.

    Terminal event is always ``result`` or ``error``, and the stream closes on it. Reconnection
    with ``Last-Event-ID`` resumes from the events already produced.
    """
    caller = _source(request)
    key = None if body.idempotency_key is None else f"{caller}:{body.idempotency_key}"
    streams = _stream_registry(request.app)

    existing = streams.get(key) if key is not None else None
    if existing is not None:
        # api.md §4: a repeated idempotency key replays the job's events rather than re-executing.
        # This is also what makes a reconnect work — the browser resends the same key with its
        # `Last-Event-ID`, attaches to the stream it was already reading, and receives exactly
        # what it missed.
        return sse_response(
            existing,
            stream_id=existing.stream_id,
            last_event_id=request.headers.get("last-event-id"),
            generator=GENERATOR,
            heartbeat_seconds=15.0,
            poll_interval_seconds=0.01,
            terminal_events=_TERMINAL_EVENTS,
        )

    stream_id = f"generate-{new_id()}"
    source = _ExecutionSource(EventBroker(), stream_id)
    if key is not None:
        streams[key] = source
        while len(streams) > _MAX_RETAINED_STREAMS:
            streams.pop(next(iter(streams)))
    cancel = CancellationToken()

    worker = threading.Thread(
        target=_drive,
        args=(
            source,
            request.app.state.database,
            _to_request(body, source=caller, stream=True),
            _context(request),
            cancel,
        ),
        name="loadcoach-generate-stream",
        daemon=True,
    )
    worker.start()

    return sse_response(
        source,
        stream_id=stream_id,
        # A POST with no idempotency key is a new execution with its own sequence space, so a
        # `Last-Event-ID` from some other stream would skip every frame this one produces and
        # leave the connection waiting for ever. It is honoured only where it can mean something:
        # against a stream this caller is demonstrably reconnecting to.
        last_event_id=None,
        generator=GENERATOR,
        heartbeat_seconds=15.0,
        poll_interval_seconds=0.01,
        # api.md §4: the terminal event is always `result` or `error`. Naming them here is what
        # closes the connection when the execution is done, rather than polling an event source
        # that will never speak again.
        terminal_events=frozenset({"result", "error"}),
    )
