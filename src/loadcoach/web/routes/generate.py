"""loadcoach.web.routes.generate — POST /generate and POST /generate/stream (api.md §4).

Both endpoints run the same executor, which always calls the provider through
``Provider.stream()``. The difference between them is what reaches the caller, not how the model
is called — which is what makes cancellation, the idle timeout and partial-response preservation
uniform across the two (api.md §5).

**Idempotency is durable (LCX19).** Before anything executes, the job row is reserved: a repeated
``idempotency_key`` from the same caller finds that row through the unique index rather than a
process-local registry, whether the execution is still running or finished long ago — and a key
older than ``queue.idempotency_ttl_hours`` has been released. ``POST /generate`` with a repeated
key returns the original job; ``POST /generate/stream`` attaches to its event stream, replaying the
persisted ``routing`` and terminal ``result``/``error`` frames from ``job_events`` and following
the live broker for anything still being produced. Token frames are fanned out live and never
stored (they would dominate the table for no benefit a reconnecting client can use), so a reconnect
receives the persisted frames it missed and the terminal result, which carries the whole output.

Route handlers contain no business logic: each calls one service function and renders.
"""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime
from functools import partial
from typing import TYPE_CHECKING, Any

import anyio
from baseaicore import SuiteError
from fastapi import APIRouter, Request
from mirrorwall import sse_response
from modelrack import CancellationToken, Message, Role, ToolDefinition
from pydantic import BaseModel, ConfigDict, Field, model_validator
from setspec import GeneratorInfo
from starlette.responses import StreamingResponse

from loadcoach.__about__ import __version__
from loadcoach.domain.authorization import authorize
from loadcoach.domain.queue_state import TERMINAL_STATES
from loadcoach.domain.routing.subject import RuntimeOverrides
from loadcoach.services.execution import (
    ExecutionContext,
    GenerateRequest,
    StreamChunk,
    execute,
    provider_facts_for,
    reserve_sync_job,
)
from loadcoach.services.queue import job_document
from loadcoach.services.task_profiles import DEFAULT_SCHEMAS_DIR
from loadcoach.web.auth import CurrentPrincipal
from loadcoach.web.routes.routing import OverridesBody
from loadcoach.web.routing_support import current_snapshot, routing_policy_for

if TYPE_CHECKING:
    from loadcoach.services.database import Database
    from loadcoach.services.job_events import JobEventSink

__all__ = ["GENERATOR", "router"]

router = APIRouter(tags=["generation"])

GENERATOR = GeneratorInfo(name="loadcoach", version=__version__)
"""The envelope's generator: this application, not MirrorWall. An envelope's generator is what
makes a document self-describing months later, and MirrorWall did not produce these events."""

_CLIENT_NAME_HEADER = "x-client-name"

_TERMINAL_EVENTS = frozenset({"result", "error"})
"""api.md §4: the terminal event is always ``result`` or ``error``."""

_POLL_SECONDS = 0.05


class ToolDefinitionBody(BaseModel):
    """One tool offered to the model (api.md §4).

    ``parameters`` is a JSON Schema object passed to the provider **unmodified**: LoadCoach does
    not validate it, rewrite it or reject a keyword it does not recognise (ADR-0041), and it never
    executes a call — the requested calls come back at ``output.tool_calls`` and running them is
    the caller's decision.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    description: str = Field(default="")
    parameters: dict[str, Any] = Field(default_factory=dict)


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
    tools: list[ToolDefinitionBody] | None = Field(default=None)
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


def source_of(request: Request) -> str:
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


def messages_of(body: GenerateBody) -> tuple[Message, ...] | None:
    """The caller's transcript as ModelRack messages, or ``None`` for the prompt form."""
    if body.messages is None:
        return None
    return tuple(
        Message(role=Role(turn.role), content=turn.content, tool_call_id=turn.tool_call_id)
        for turn in body.messages
    )


def tools_of(body: GenerateBody) -> tuple[ToolDefinition, ...]:
    """The tools the caller offered, as ModelRack definitions (api.md §4).

    ``parameters`` is copied through verbatim — never validated, rewritten or inferred (ADR-0041).
    An empty list and ``None`` produce the same empty tuple, so ``tools: []`` imposes nothing and
    routes exactly as a body with no ``tools`` at all (ADR-0075).
    """
    if not body.tools:
        return ()
    return tuple(
        ToolDefinition(name=tool.name, description=tool.description, parameters=tool.parameters)
        for tool in body.tools
    )


def overrides_of(body: OverridesBody | None) -> RuntimeOverrides | None:
    """Routing §10's overrides from the body, or ``None``."""
    from baseaicore import RuntimeProfile

    if body is None:
        return None
    profile_override = (
        None
        if body.runtime_profile is None
        else RuntimeProfile(**body.runtime_profile.model_dump(exclude_none=True))
    )
    return RuntimeOverrides(
        model=body.model,
        runtime_profile=profile_override,
        disallow_fallback=body.disallow_fallback,
        require_evidence=body.require_evidence,
    )


def _to_request(body: GenerateBody, *, source: str, stream: bool) -> GenerateRequest:
    return GenerateRequest(
        task=body.task,
        system=body.system,
        prompt=body.prompt,
        messages=messages_of(body),
        response_format=body.response_format,
        sampling=dict(body.sampling),
        overrides=overrides_of(body.overrides),
        tools=tools_of(body),
        source=source,
        idempotency_key=body.idempotency_key,
        stream=stream,
    )


def _context(request: Request) -> ExecutionContext:
    app = request.app
    # The queue runtime's breaker registry and residency tracker, so a synchronous request obeys
    # the same circuit breakers and probe discipline as a queued job (F3/M5C-3). The runtime is
    # always present in a served application; ``None`` only outside the lifespan.
    runtime = app.state.queue_runtime
    residency = None if runtime is None else runtime.residency
    return ExecutionContext(
        provider=app.state.provider,
        provider_facts=provider_facts_for(app.state.provider),
        policy=routing_policy_for(app.state.settings, database=app.state.database),
        schemas_dir=DEFAULT_SCHEMAS_DIR,
        snapshot=current_snapshot(app),
        timeout_seconds=app.state.settings.execution.default_timeout_seconds,
        sink=app.state.event_sink,
        breakers=None if runtime is None else runtime.breakers,
        resident_models=None if residency is None else residency.resident_canonical_ids(),
        resident_devices=None if residency is None else residency.resident_devices(),
        # Residency was an *input* here and never an output: a synchronous request routed with
        # the exception for an already-loaded model and then recorded no load of its own. So the
        # next request saw an empty map, could not apply the exception, and was refused
        # `insufficient_vram` by the memory this one is still holding — which on a single-GPU
        # machine fails the second stage of every multi-stage workflow.
        residency=residency,
        in_use_model_ids=frozenset() if runtime is None else runtime.in_use_model_ids(),
        vram_headroom_bytes=(0 if runtime is None else runtime.policy.vram_headroom_bytes),
    )


def _await_terminal(database: Database, job_id: str, *, timeout_seconds: float) -> dict[str, Any]:
    """The original job's document, waited for if its execution is still running."""
    deadline = time.monotonic() + timeout_seconds
    while True:
        document = job_document(database, job_id)
        if document["state"] in {state.value for state in TERMINAL_STATES}:
            return document
        if time.monotonic() >= deadline:
            return document
        time.sleep(_POLL_SECONDS)


@router.post("/generate", summary="Route, execute and validate one generation")
def post_generate(
    request: Request, principal: CurrentPrincipal, body: GenerateBody
) -> dict[str, Any]:
    """Execute ``body`` synchronously and return the result with its routing metadata.

    ``def``, not ``async def`` (ADR-0003 §1): this handler touches the database and the provider,
    so Starlette runs it in the worker threadpool rather than on the event loop.

    A repeated ``idempotency_key`` returns the original job (api.md §4) — its document, waited
    for if it is still running — rather than executing again.

    Errors: ``TASK_PROFILE_NOT_FOUND``, ``NO_ELIGIBLE_MODEL`` (with every candidate and its
    rejection reason), ``ALL_CANDIDATES_FAILED`` (with every attempt and its error).
    """
    authorize(principal, "write")
    app = request.app
    generate_request = _to_request(body, source=source_of(request), stream=False)
    reserved = reserve_sync_job(
        app.state.database,
        generate_request,
        now=datetime.now(UTC),
        ttl_hours=app.state.settings.queue.idempotency_ttl_hours,
    )
    if not reserved.created:
        return _await_terminal(
            app.state.database,
            reserved.job_id,
            timeout_seconds=app.state.settings.execution.default_timeout_seconds,
        )
    outcome = execute(
        app.state.database,
        generate_request,
        _context(request),
        cancel=CancellationToken(),
        job_id=reserved.job_id,
    )
    return outcome.as_json()


def _drive(
    sink: JobEventSink,
    database: Database,
    job_id: str,
    request: GenerateRequest,
    context: ExecutionContext,
    cancel: CancellationToken,
) -> None:
    """Run the execution to completion in a worker thread, fanning out each chunk as it happens.

    ``routing`` and the terminal frame are persisted and published by the executor through the
    sink; tokens and tool calls are fanned out live from here. Whatever goes wrong, the stream
    ends with a terminal frame: a persisted ``error`` is the backstop for a producer that died
    without one.
    """

    def publish(chunk: StreamChunk) -> None:
        if chunk.kind == "token":
            sink.publish_token(job_id, chunk.payload)
        elif chunk.kind == "tool_call":
            sink.publish_live(job_id, "tool_call", chunk.payload)

    try:
        execute(database, request, context, cancel=cancel, on_chunk=publish, job_id=job_id)
    except SuiteError:
        pass  # persisted as the terminal ``error`` frame by the executor
    except Exception as exc:  # noqa: BLE001 — the stream's terminal frame is the report
        _backstop(sink, database, job_id, f"The execution failed unexpectedly: {exc}")
    finally:
        _backstop(sink, database, job_id, "The execution ended without producing a result.")


def _backstop(sink: JobEventSink, database: Database, job_id: str, message: str) -> None:
    """Persist a terminal ``error`` frame if the job has none — never a second one."""
    from sqlalchemy import select

    from loadcoach.infrastructure.db.models import Job, JobEvent

    with sink.write(database) as (session, events):
        terminal = session.execute(
            select(JobEvent.id).where(
                JobEvent.job_id == job_id, JobEvent.event_type.in_(sorted(_TERMINAL_EVENTS))
            )
        ).first()
        if terminal is not None:
            return
        job = session.get(Job, job_id)
        if job is not None and job.state not in {state.value for state in TERMINAL_STATES}:
            job.state = "failed"
            job.state_reason = "INTERNAL_ERROR"
            job.error_code = "INTERNAL_ERROR"
            job.error_text = message
            job.completed_at = datetime.now(UTC)
        events.append(
            job_id,
            "error",
            now=datetime.now(UTC),
            data={"code": "INTERNAL_ERROR", "message": message},
        )


@router.post("/generate/stream", summary="Stream one generation as it is produced")
async def post_generate_stream(
    request: Request, principal: CurrentPrincipal, body: GenerateBody
) -> StreamingResponse:
    """Execute ``body`` and stream the routing decision, tokens and terminal result.

    ``async def`` (ADR-0003 §2): this handler only streams. The reservation is one database
    write dispatched to the threadpool; the execution itself runs in a worker thread; and every
    call MirrorWall's ``sse_response`` makes back into the event source is dispatched with
    ``anyio.to_thread.run_sync`` by that package — so nothing here can put a blocking call on
    the event loop.

    Terminal event is always ``result`` or ``error``, and the stream closes on it. A repeated
    ``idempotency_key`` attaches to the job's stream (api.md §4) and ``Last-Event-ID`` is
    honoured there, against the persisted events; a POST with no key is a new execution with
    its own sequence space, where a foreign ``Last-Event-ID`` would skip every frame this one
    produces, so it is ignored.
    """
    authorize(principal, "write")
    app = request.app
    generate_request = _to_request(body, source=source_of(request), stream=True)
    reserved = await anyio.to_thread.run_sync(
        partial(
            reserve_sync_job,
            app.state.database,
            generate_request,
            now=datetime.now(UTC),
            ttl_hours=app.state.settings.queue.idempotency_ttl_hours,
        )
    )
    sink: JobEventSink = app.state.event_sink
    source = sink.source(app.state.database, reserved.job_id)
    last_event_id: str | None = None
    if reserved.created:
        worker = threading.Thread(
            target=_drive,
            args=(
                sink,
                app.state.database,
                reserved.job_id,
                generate_request,
                _context(request),
                CancellationToken(),
            ),
            name="loadcoach-generate-stream",
            daemon=True,
        )
        worker.start()
    else:
        last_event_id = request.headers.get("last-event-id")
    return sse_response(
        source,
        stream_id=reserved.job_id,
        last_event_id=last_event_id,
        generator=GENERATOR,
        heartbeat_seconds=15.0,
        # 2 ms, not the 10 ms this stream shipped with: the SSE loop sleeps this long whenever
        # the subscription is momentarily empty, so the poll quantizes token delivery. Measured
        # over a real socket at 10 ms the added gap p95 was ~10 ms against spec §15's 5 ms
        # budget (F12/M5C-12); at 2 ms it is within budget. A condition wake-up would need
        # MirrorWall's Subscription to become awaitable, which ADR-0003 §5-6 rules out for now.
        poll_interval_seconds=0.002,
        terminal_events=_TERMINAL_EVENTS,
    )
