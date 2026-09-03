"""loadcoach.services.execution — routing, executing, validating and recording one generation.

**The caller's text reaches the provider byte-for-byte.** LoadCoach does not prepend a system
prompt of its own, does not substitute the task profile's wording, and does not rewrite the
request (spec §9). The only prompt it applies is the structured-output corrective instruction it
originates, and that is recorded on the attempt that used it — so a caller whose own provenance
records the hash of what it sent (IdeaPress does) can trust that record. A test asserts the
transcript ModelRack received equals what the caller sent.

**Every call goes through `Provider.stream()`, including the non-streaming endpoint.** A blocking
round trip offers no boundary at which a cancellation token can take effect, so "cancelled within
one chunk" would be unachievable for exactly the requests most likely to be long (API §5).
Assembling internally costs nothing — it is the same NDJSON on the wire either way — and it makes
cancellation, the idle timeout and partial-response preservation uniform across both endpoints. A
provider that declares no streaming records the degradation ``cancellation_deferred_to_completion``
so the limit is visible rather than assumed away.

**Provider time and LoadCoach overhead are measured separately and never added together into one
number.** Overhead is wall time minus the time spent inside the provider call; conflating the two
is the failure mode this phase's plan names by name, and it makes the 15 ms budget meaningless.

**A corrective retry is a new attempt row.** It never edits the previous one: the original
attempt's output, timings and failure are what make the retry explicable.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, ClassVar, cast

from baseaicore import (
    ModelIdentity,
    ProviderKind,
    SuiteError,
    ValidationError,
    is_supported,
    new_id,
)
from modelrack import (
    CancellationToken,
    GenerationCancelled,
    GenerationRequest,
    GenerationResult,
    Message,
    ProviderError,
    ProviderStatus,
    ResponseFormat,
    ResponseFormatKind,
    Role,
    SamplingParameters,
    StreamCompleted,
    StreamFailed,
    ThinkingDelta,
    TokenDelta,
    ToolCallDelta,
)
from sqlalchemy import update

from loadcoach.domain.authorization import Principal, authorize
from loadcoach.domain.routing.constraints import free_vram_by_gpu
from loadcoach.domain.routing.context_budget import estimate_input_tokens
from loadcoach.domain.routing.subject import ProviderFacts, RuntimeOverrides
from loadcoach.domain.validation import (
    ValidationOutcome,
    validate_output,
)
from loadcoach.infrastructure.db.models import Job, JobAttempt, JobEvent
from loadcoach.infrastructure.db.models import Validation as ValidationRow
from loadcoach.services.prompts import render_corrective_retry
from loadcoach.services.routing import (
    RouteRequest,
    RoutingPolicy,
    RoutingResult,
    route,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping, Sequence
    from contextlib import AbstractContextManager
    from pathlib import Path

    from modelrack.provider import Provider
    from sqlalchemy.orm import Session
    from sweatmeter import TelemetrySnapshot

    from loadcoach.domain.circuit_breaker import CircuitBreakers
    from loadcoach.domain.routing.ranking import RankedCandidate
    from loadcoach.domain.routing.subject import ModelFacts, ProviderFacts
    from loadcoach.services.database import Database
    from loadcoach.services.job_events import EventWriter, JobEventSink

__all__ = [
    "AllCandidatesFailed",
    "AttemptOutcome",
    "AttemptRecord",
    "AttemptRefused",
    "ExecutionOutcome",
    "GenerateRequest",
    "ProviderFailed",
    "ReservedJob",
    "StreamChunk",
    "corrective_turns",
    "execute",
    "identity_of",
    "link_decision",
    "load_task_schema",
    "existing_job_for_key",
    "provider_facts_for",
    "reserve_sync_job",
    "run_attempt",
    "sampling_for",
    "stream_execute",
    "write_attempt",
]

logger = logging.getLogger(__name__)

_DEGRADATION_NO_STREAMING = "cancellation_deferred_to_completion"


class ProviderFailed(SuiteError):
    """The provider could not complete the generation, and no fallback remained."""

    code: ClassVar[str] = "PROVIDER_UNAVAILABLE"


class AllCandidatesFailed(SuiteError):
    """Every ranked candidate was tried and every one failed.

    ``details["attempts"]`` carries each attempt with its model and error (api.md §10) — a bare
    "it failed" tells a caller nothing about whether to retry, change the request, or fix a host.
    """

    code: ClassVar[str] = "ALL_CANDIDATES_FAILED"


class AttemptRefused(SuiteError):
    """The attempt row could not be written because the job is no longer this worker's.

    Raised by :func:`write_attempt` when the compare-and-set on ``lease_owner`` finds another
    owner (the lease expired and was reclaimed) — the attempt's result is discarded rather than
    written over the reclaimer's, which is the fence that turns a lease race into a lost attempt
    instead of a corrupted history.
    """

    code: ClassVar[str] = "ATTEMPT_REFUSED"


@dataclass(frozen=True, slots=True)
class GenerateRequest:
    """One ``POST /generate`` body, already validated by the web layer (api.md §4).

    Attributes:
        task: The task profile to route for.
        system: The caller's system turn, or ``None``.
        prompt: The caller's user turn. Mutually exclusive with ``messages``.
        messages: A full transcript. Mutually exclusive with ``prompt``.
        response_format: ``"text"``, ``"json"`` or ``"json_schema"``, overriding the profile.
        sampling: Overrides for the profile's execution parameters.
        overrides: Routing §10's overrides.
        source: The calling application, for the idempotency scope and the job record.
        idempotency_key: Makes a retried POST safe.
        stream: Whether the caller asked for the streaming endpoint. Does **not** change how the
            provider is called — that is always ``stream()`` — only what reaches the caller.
    """

    task: str
    system: str | None = None
    prompt: str | None = None
    messages: tuple[Message, ...] | None = None
    response_format: str | None = None
    sampling: Mapping[str, Any] = field(default_factory=dict)
    overrides: RuntimeOverrides | None = None
    source: str = "anonymous"
    idempotency_key: str | None = None
    stream: bool = False

    def transcript(self) -> tuple[Message, ...]:
        """Return exactly the messages the provider will be sent.

        No system prompt of LoadCoach's own is added here or anywhere else on this path. The
        result is what a byte-for-byte test compares against what the caller supplied.
        """
        if self.messages is not None:
            return self.messages
        turns: list[Message] = []
        if self.system is not None:
            turns.append(Message(role=Role.SYSTEM, content=self.system))
        turns.append(Message(role=Role.USER, content=self.prompt or ""))
        return tuple(turns)


@dataclass(frozen=True, slots=True)
class AttemptRecord:
    """One attempt, exactly as it is stored and reported."""

    attempt: int
    canonical_id: str
    model_id: str | None
    runtime_profile_hash: str
    rank: int
    outcome: str
    started_at: datetime
    completed_at: datetime | None = None
    provider_ms: int | None = None
    ttft_ms: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_write_tokens: int | None = None
    cache_read_tokens: int | None = None
    finish_reason: str | None = None
    error_code: str | None = None
    error_text: str | None = None
    partial_response_hash: str | None = None
    prompt_id: str | None = None
    prompt_version: str | None = None
    prompt_sha256: str | None = None
    validation: ValidationOutcome | None = None
    text: str = ""

    def as_json(self) -> dict[str, Any]:
        """Return the attempt entry the API response carries."""
        return {
            "attempt": self.attempt,
            "model": self.canonical_id,
            "runtime_profile_hash": self.runtime_profile_hash,
            "rank": self.rank,
            "outcome": self.outcome,
            "provider_ms": self.provider_ms,
            "ttft_ms": self.ttft_ms,
            "error_code": self.error_code,
            "prompt_id": self.prompt_id,
            "prompt_version": self.prompt_version,
            "prompt_sha256": self.prompt_sha256,
        }


@dataclass(frozen=True, slots=True)
class ExecutionOutcome:
    """A completed (or failed) execution, ready to serialize and to store."""

    job_id: str
    status: str
    text: str
    structured: Any
    tool_calls: tuple[Any, ...]
    thinking: str | None
    routing: RoutingResult
    selected: RankedCandidate
    attempts: tuple[AttemptRecord, ...]
    validation: ValidationOutcome
    degradations: tuple[str, ...]
    total_ms: int
    provider_ms: int
    overhead_ms: int
    ttft_ms: int | None
    input_tokens: int | None
    output_tokens: int | None
    cache_write_tokens: int | None
    cache_read_tokens: int | None
    thinking_tokens: int | None
    queue_wait_ms: int = 0

    def as_json(self) -> dict[str, Any]:
        """Return the ``POST /generate`` response body (api.md §4)."""
        subject = self.selected.subject
        return {
            "job_id": self.job_id,
            "status": self.status,
            "output": {
                "text": self.text,
                "structured": self.structured,
                "tool_calls": list(self.tool_calls),
            },
            "reasoning": {
                "available": self.thinking is not None,
                "summary": self.thinking,
                "source": "provider" if self.thinking is not None else None,
            },
            "model": {
                "canonical_id": subject.facts.canonical_id,
                "model_ref": subject.facts.model_id,
                "runtime_profile_hash": subject.runtime_profile_hash,
                "served_context": subject.served_context.tokens,
                "served_context_source": subject.served_context.source,
                "target_gpu_index": self.selected.target_gpu_index,
            },
            "routing": {
                "decision_id": self.routing.explanation.decision_id,
                "rank": self.selected.rank,
                "final_score": self.selected.final_score,
                "flags": list(self.routing.explanation.flags),
                "explanation_url": f"/api/v1/jobs/{self.job_id}/explanation",
            },
            "usage": {
                "input_tokens": self.input_tokens,
                "output_tokens": self.output_tokens,
                # ADR-0016 rule 4: an unavailable measurement is the string "unsupported" in
                # JSON, never null and never 0 — and under ADR-0070 a `0` here is a real count,
                # the provider's protocol having reported that nothing was billed to this class.
                # The two are different answers and a consumer's ledger must be able to tell them
                # apart, which is why neither is rendered as the other.
                "cache_write_tokens": self.cache_write_tokens
                if self.cache_write_tokens is not None
                else "unsupported",
                "cache_read_tokens": self.cache_read_tokens
                if self.cache_read_tokens is not None
                else "unsupported",
                "thinking_tokens": self.thinking_tokens
                if self.thinking_tokens is not None
                else "unsupported",
            },
            "timing": {
                "total_ms": self.total_ms,
                "provider_ms": self.provider_ms,
                "loadcoach_overhead_ms": self.overhead_ms,
                "ttft_ms": self.ttft_ms,
                "queue_wait_ms": self.queue_wait_ms,
            },
            "validation": {
                "performed": self.validation.performed,
                "passed": self.validation.passed,
                "attempts": len(self.attempts),
                "checks": self.validation.as_json()["checks"],
            },
            "attempts": [attempt.as_json() for attempt in self.attempts],
            "degradations": list(self.degradations),
        }


@dataclass(frozen=True, slots=True)
class StreamChunk:
    """One thing the executor produced, for the streaming endpoint to frame.

    Attributes:
        kind: ``"token"``, ``"thinking"``, ``"tool_call"``, ``"routing"``, ``"attempt"``,
            ``"result"`` or ``"error"``.
        payload: The chunk's body.
    """

    kind: str
    payload: Mapping[str, Any]


def load_task_schema(schema_ref: str | None, *, schemas_dir: Path) -> dict[str, Any] | None:
    """Load a task profile's JSON Schema, or ``None`` when it declares none.

    Args:
        schema_ref: The profile's ``execution.json_schema_ref``.
        schemas_dir: Directory it resolves against.

    Returns:
        The parsed schema, or ``None``.

    Raises:
        FileNotFoundError: The reference names a file that is not there. Task-profile validation
            already refuses that at startup, so reaching it here means the file was removed since.
    """
    if schema_ref is None:
        return None
    root = schemas_dir.resolve()
    target = (root / schema_ref).resolve()
    if root not in target.parents:
        # A profile's schema reference resolves *inside* the schemas directory, never out of it
        # (Security Standards §14: path traversal). Task-profile validation refuses this at
        # startup; reaching it here means the profile changed since, and it is still refused.
        raise ValidationError(
            f"json_schema_ref {schema_ref!r} resolves outside the schemas directory.",
            details={"json_schema_ref": schema_ref, "schemas_dir": str(root)},
        )
    parsed: dict[str, Any] = json.loads(target.read_text(encoding="utf-8"))
    return parsed


def _sha256(text: str) -> str:
    return f"sha256:{hashlib.sha256(text.encode('utf-8')).hexdigest()}"


def _response_format(kind: str, schema: Mapping[str, Any] | None) -> ResponseFormat | None:
    if kind == "json_schema" and schema is not None:
        return ResponseFormat(kind=ResponseFormatKind.JSON_SCHEMA, schema=dict(schema))
    if kind == "json":
        return ResponseFormat(kind=ResponseFormatKind.JSON)
    return None


def _count(value: object) -> int | None:
    """Read a token count, returning ``None`` — never ``0`` — when it was not reported."""
    return int(value) if is_supported(value) and isinstance(value, (int, float)) else None


def _timing_ms(value: object) -> int | None:
    return int(value) if is_supported(value) and isinstance(value, (int, float)) else None


@dataclass
class _Collected:
    """What one provider call produced, assembled from its stream."""

    text: str = ""
    thinking: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    result: GenerationResult | None = None
    failure: ProviderError | None = None
    ttft_ms: int | None = None
    provider_ms: int = 0


def _run_stream(
    provider: Provider,
    request: GenerationRequest,
    *,
    on_chunk: Callable[[StreamChunk], None] | None,
) -> _Collected:
    """Drive one provider call to completion, timing only the provider's own work.

    The wall clock around this function is provider time; everything else the executor does is
    overhead, and the two are never added into one number.
    """
    collected = _Collected()
    started = time.perf_counter()
    index = 0
    # A provider may deliver a failure as a StreamFailed event *or* raise it — ModelRack promises
    # every method may raise ProviderUnavailable or ProviderTimeout, and an adapter that fails
    # before it opens the stream has no event to deliver. Both paths land in `failure`, so the
    # attempt record is the same shape whichever way the provider expressed it, and whatever was
    # already produced is preserved as the partial response.
    try:
        _drain(provider, request, collected, started, index, on_chunk)
    except ProviderError as exc:
        collected.failure = exc
    collected.provider_ms = int((time.perf_counter() - started) * 1000)
    return collected


def _drain(
    provider: Provider,
    request: GenerationRequest,
    collected: _Collected,
    started: float,
    index: int,
    on_chunk: Callable[[StreamChunk], None] | None,
) -> None:
    """Consume the provider's stream into ``collected``. Raises whatever the provider raises.

    ``started`` is the caller's own mark, so time to first token is measured from the same instant
    provider time is, rather than from a moment inside this function.
    """
    for event in provider.stream(request):
        if isinstance(event, TokenDelta):
            if collected.ttft_ms is None:
                collected.ttft_ms = int((time.perf_counter() - started) * 1000)
            collected.text += event.text
            if on_chunk is not None:
                on_chunk(StreamChunk("token", {"delta": event.text, "index": index}))
            index += 1
        elif isinstance(event, ThinkingDelta):
            collected.thinking += event.text
        elif isinstance(event, ToolCallDelta):
            collected.tool_calls.append(
                {
                    "call_index": event.call_index,
                    "id": event.id,
                    "name": event.name,
                    "arguments_fragment": event.arguments_fragment,
                }
            )
            if on_chunk is not None:
                on_chunk(StreamChunk("tool_call", collected.tool_calls[-1]))
        elif isinstance(event, StreamCompleted):
            collected.result = event.result
        elif isinstance(event, StreamFailed):
            collected.failure = event.error
            if event.partial_text:
                collected.text = event.partial_text


def _validate(
    text: str,
    *,
    validation_policy: Mapping[str, Any],
    schema: Mapping[str, Any] | None,
) -> ValidationOutcome:
    require_schema = bool(validation_policy.get("require_schema"))
    return validate_output(
        text,
        require_valid_json=bool(validation_policy.get("require_valid_json")),
        schema=schema if require_schema else None,
        required_fields=tuple(validation_policy.get("required_fields") or ()),
        max_output_chars=cast("int | None", validation_policy.get("max_output_chars")),
    )


def _problems_text(outcome: ValidationOutcome) -> str:
    lines: list[str] = []
    for check in outcome.failures:
        detail = check.detail
        if "fields" in detail:
            lines.extend(
                f"{item['path']}: {item['problem']}"
                for item in cast("list[dict[str, str]]", detail["fields"])
            )
        elif "missing" in detail:
            lines.extend(f"$.{name}: required but missing" for name in detail["missing"])
        else:
            lines.append(f"{check.kind}: {json.dumps(detail, sort_keys=True, default=str)}")
    return "\n".join(lines) or "the output did not satisfy the required format"


def sampling_for(
    request: GenerateRequest, execution_policy: Mapping[str, Any]
) -> SamplingParameters:
    """The sampling parameters an attempt uses: the request's overrides over the profile's."""
    return SamplingParameters(
        temperature=cast(
            "float | None",
            request.sampling.get("temperature", execution_policy.get("temperature")),
        ),
        max_output_tokens=cast(
            "int | None",
            request.sampling.get("max_output_tokens", execution_policy.get("max_output_tokens")),
        ),
        top_p=cast("float | None", request.sampling.get("top_p")),
        seed=cast("int | None", request.sampling.get("seed")),
    )


@dataclass(frozen=True, slots=True)
class AttemptOutcome:
    """What one attempt produced: its record, and what the caller needs to decide the next step.

    Attributes:
        record: The attempt exactly as it will be stored and reported.
        failure: The provider error, or ``None`` when the provider answered.
        cancelled: Whether ``failure`` is the caller's own cancellation — terminal, never retried.
        validation: The validation outcome, or ``None`` when the provider failed.
        text: The full text produced (partial, on failure).
        result: The provider's result, or ``None`` on failure.
        thinking: Reasoning content the provider returned, or ``""``.
        tool_calls: The tool calls the provider requested.
    """

    record: AttemptRecord
    failure: ProviderError | None
    cancelled: bool
    validation: ValidationOutcome | None
    text: str
    result: GenerationResult | None
    thinking: str
    tool_calls: tuple[dict[str, Any], ...]

    @property
    def passed(self) -> bool:
        """Whether the provider answered and validation did not fail."""
        return self.failure is None and (
            self.validation is None or self.validation.passed is not False
        )


def run_attempt(
    provider: Provider,
    *,
    request: GenerateRequest,
    candidate: RankedCandidate,
    turns: tuple[Message, ...],
    attempt_number: int,
    schema: Mapping[str, Any] | None,
    execution_policy: Mapping[str, Any],
    validation_policy: Mapping[str, Any],
    timeout_seconds: float | None,
    cancel: CancellationToken | None,
    on_chunk: Callable[[StreamChunk], None] | None,
    now: Callable[[], datetime],
    correction: Any = None,
) -> AttemptOutcome:
    """Make exactly one provider call on one candidate and validate what came back.

    The unit the queue worker composes: it decides nothing about retries or fallback — that is
    the caller's policy (queue §7) — and writes nothing; :func:`write_attempt` does that in the
    caller's transaction so the attempt row and the state transition it belongs to commit
    together.

    Args:
        provider: The provider to call.
        request: The caller's request (sampling and response format come from it).
        candidate: The ranked candidate to run.
        turns: The transcript to send — the caller's, or the caller's plus a corrective turn.
        attempt_number: The number this attempt will carry.
        schema: The profile's JSON Schema, or ``None``.
        execution_policy: The profile's ``execution`` block.
        validation_policy: The profile's ``validation`` block.
        timeout_seconds: The per-call provider timeout.
        cancel: The cancellation token, honoured within one chunk.
        on_chunk: Receives each token and tool call as it happens, or ``None``.
        now: The clock.
        correction: The corrective prompt record applied to ``turns``, if any, for the record.

    Returns:
        The :class:`AttemptOutcome`.
    """
    subject = candidate.subject
    base_format = request.response_format or str(execution_policy.get("response_format", "text"))
    started_at = now()
    call = GenerationRequest(
        identity=_identity_of(subject.facts),
        messages=turns,
        runtime_profile=subject.runtime_profile,
        sampling=sampling_for(request, execution_policy),
        response_format=_response_format(base_format, schema),
        timeout_seconds=timeout_seconds,
        cancel=cancel,
    )
    collected = _run_stream(provider, call, on_chunk=on_chunk)
    if collected.failure is not None:
        cancelled = isinstance(collected.failure, GenerationCancelled)
        record = _record(
            attempt_number,
            candidate,
            started_at,
            now(),
            collected,
            outcome="cancelled" if cancelled else _failure_outcome(collected.failure),
            error=collected.failure,
            correction=correction,
        )
        return AttemptOutcome(
            record=record,
            failure=collected.failure,
            cancelled=cancelled,
            validation=None,
            text=collected.text,
            result=None,
            thinking=collected.thinking,
            tool_calls=tuple(collected.tool_calls),
        )
    validation = _validate(collected.text, validation_policy=validation_policy, schema=schema)
    passed = validation.passed is not False
    record = _record(
        attempt_number,
        candidate,
        started_at,
        now(),
        collected,
        outcome="completed" if passed else "validation_failed",
        validation=validation,
        correction=correction,
    )
    return AttemptOutcome(
        record=record,
        failure=None,
        cancelled=False,
        validation=validation,
        text=collected.text,
        result=collected.result,
        thinking=collected.thinking,
        tool_calls=tuple(collected.tool_calls),
    )


def _failure_outcome(error: ProviderError) -> str:
    """Map a provider error onto ``job_attempts.outcome``'s vocabulary (data model §2)."""
    from modelrack import ContextLimitExceeded, ProviderTimeout

    if isinstance(error, ProviderTimeout):
        return "timeout"
    if isinstance(error, ContextLimitExceeded):
        return "context_exceeded"
    return "provider_error"


def corrective_turns(
    transcript: tuple[Message, ...],
    *,
    previous_text: str,
    outcome: ValidationOutcome,
    schema: Mapping[str, Any] | None,
) -> tuple[tuple[Message, ...], Any]:
    """Build the transcript for a corrective retry, and the prompt record it applied.

    A corrective retry is a NEW attempt with LoadCoach's own prompt appended to the caller's
    transcript. The caller's turns are never rewritten, only followed (spec §9).

    Args:
        transcript: The caller's turns.
        previous_text: What the failed attempt produced.
        outcome: Why it failed validation.
        schema: The schema the output must satisfy, for the prompt.

    Returns:
        ``(turns, correction)`` — the transcript to send and the rendered prompt record.
    """
    correction = render_corrective_retry(
        problems=_problems_text(outcome),
        schema=json.dumps(schema, indent=2, sort_keys=True) if schema else "{}",
        previous_output=previous_text,
    )
    turns = (
        *transcript,
        Message(role=Role.ASSISTANT, content=previous_text),
        *((Message(role=Role.SYSTEM, content=correction.system),) if correction.system else ()),
        Message(role=Role.USER, content=correction.user),
    )
    return turns, correction


def _make_resident(
    candidate: RankedCandidate,
    *,
    residency: Any | None,
    now: Callable[[], datetime],
    in_use_model_ids: frozenset[str] = frozenset(),
    vram_headroom_bytes: int = 0,
    free_bytes_by_gpu: Mapping[int, int] | None = None,
) -> None:
    """Make the chosen candidate resident before running it, as the queue worker does.

    Args:
        candidate: The candidate about to be attempted.
        residency: The residency tracker, or ``None`` outside a served application.
        now: The clock.
        in_use_model_ids: Models in-flight jobs hold; never evicted.
        vram_headroom_bytes: The per-device reserve.
        free_bytes_by_gpu: Free memory per device, as telemetry reports it.

    The synchronous endpoints previously routed with residency as an *input* — the exception that
    lets an already-loaded model be chosen without re-checking VRAM — and never wrote one. So a
    `/generate` loaded a model into the provider, recorded nothing, and the next request saw an
    empty residency map, could not apply the exception, and was refused ``insufficient_vram`` by
    the memory the previous request was still holding. On a single-GPU machine that makes the
    second stage of any multi-stage workflow fail, whichever task profile it asks for, and the
    queue does not rescue it because routing refuses before a job reaches ``waiting_resources``.

    Failures are swallowed deliberately. Residency is an optimisation and an eviction policy, not
    a precondition: a tracker that cannot record must not stop a generation the provider is
    perfectly able to serve.
    """
    if residency is None or candidate.target_gpu_index is None:
        return
    facts = candidate.subject.facts
    try:
        residency.ensure_loaded(
            model_id=facts.model_id,
            canonical_id=facts.canonical_id,
            identity=identity_of(facts),
            profile=candidate.subject.runtime_profile,
            gpu_index=candidate.target_gpu_index,
            in_use_model_ids=in_use_model_ids,
            required_bytes=candidate.estimated_vram_bytes,
            free_bytes=(free_bytes_by_gpu or {}).get(candidate.target_gpu_index),
            headroom_bytes=vram_headroom_bytes,
            now=now(),
        )
    except Exception:  # noqa: BLE001 — see the docstring: never a precondition
        logger.warning("residency.sync_record_failed", extra={"canonical_id": facts.canonical_id})


def _execute_attempts(
    provider: Provider,
    request: GenerateRequest,
    routing: RoutingResult,
    *,
    schema: Mapping[str, Any] | None,
    execution_policy: Mapping[str, Any],
    validation_policy: Mapping[str, Any],
    timeout_seconds: float | None,
    cancel: CancellationToken | None,
    on_chunk: Callable[[StreamChunk], None] | None,
    now: Callable[[], datetime],
    breakers: CircuitBreakers | None = None,
    residency: Any | None = None,
    residency_inputs: Mapping[str, Any] | None = None,
) -> tuple[list[AttemptRecord], RankedCandidate | None, _Collected | None, ValidationOutcome]:
    """The synchronous endpoints' loop: try each ranked candidate, retrying correctively.

    With a ``breakers`` registry, the loop marks and releases a half-open model's probe exactly
    as the worker's does (F3/M5C-3): ``allow_probe`` is asked once per candidate before its
    first attempt, a refusal while another probe is out skips the candidate, and a probe whose
    attempt is cancelled before the provider reports is handed back.
    """
    ranking = routing.explanation.ranking
    candidates = [ranking.primary, *ranking.fallbacks]
    max_attempts = int(cast("int", execution_policy.get("max_attempts", 1)))
    transcript = request.transcript()

    records: list[AttemptRecord] = []
    validation = ValidationOutcome(performed=False, passed=None, checks=())
    attempt_number = 0

    for candidate in candidates:
        if candidate is None:
            continue
        canonical_id = candidate.subject.facts.canonical_id
        _make_resident(candidate, residency=residency, now=now, **(residency_inputs or {}))
        holds_probe = False
        if breakers is not None:
            holds_probe = breakers.allow_probe(canonical_id, now=now())
            if not holds_probe and breakers.probe_busy(canonical_id):
                # Queue §7 admits a single probe, and routing's exclusion check can lose the
                # race to a worker marking it in the gap (the worker-side half is F2/M5C-2):
                # a synchronous request must not become a second, unmarked probe.
                continue
        turns = transcript
        correction: Any = None

        for _ in range(max_attempts):
            attempt_number += 1
            outcome = run_attempt(
                provider,
                request=request,
                candidate=candidate,
                turns=turns,
                attempt_number=attempt_number,
                schema=schema,
                execution_policy=execution_policy,
                validation_policy=validation_policy,
                timeout_seconds=timeout_seconds,
                cancel=cancel,
                on_chunk=on_chunk,
                now=now,
                correction=correction,
            )
            records.append(outcome.record)
            collected = _Collected(
                text=outcome.text,
                thinking=outcome.thinking,
                tool_calls=list(outcome.tool_calls),
                result=outcome.result,
                failure=outcome.failure,
                ttft_ms=outcome.record.ttft_ms,
                provider_ms=outcome.record.provider_ms or 0,
            )
            if outcome.failure is not None:
                if outcome.cancelled:
                    if holds_probe and breakers is not None:
                        # A cancelled probe says nothing about the model: hand it back so the
                        # next job may probe, rather than excluding the model until never.
                        breakers.release_probe(canonical_id)
                    return records, candidate, collected, validation
                break  # this candidate is not working; fall back
            assert outcome.validation is not None  # noqa: S101 — set whenever the provider answered
            validation = outcome.validation
            if outcome.passed:
                return records, candidate, collected, validation
            if attempt_number >= max_attempts * len(candidates):
                break
            turns, correction = corrective_turns(
                transcript, previous_text=outcome.text, outcome=validation, schema=schema
            )

    return records, None, None, validation


def identity_of(facts: ModelFacts) -> ModelIdentity:
    """The exact identity the registry resolved for ``facts`` (see :func:`_identity_of`)."""
    return _identity_of(facts)


def _identity_of(facts: ModelFacts) -> ModelIdentity:
    """Rebuild the exact identity the registry resolved, without asking the provider again.

    Reconstructed from the stored triple rather than re-resolved by name: a tag can be repointed
    between discovery and execution, and re-resolving would run whatever it points at now while
    the job records what it pointed at then (ADR-0008 §identity, ADR-0024 §2).
    """
    return ModelIdentity(
        provider_kind=ProviderKind(facts.provider_kind),
        provider_model_name=facts.provider_model_name,
        artifact_digest=facts.artifact_digest,
    )


def _record(
    attempt: int,
    candidate: RankedCandidate,
    started_at: datetime,
    completed_at: datetime,
    collected: _Collected,
    *,
    outcome: str,
    error: ProviderError | None = None,
    validation: ValidationOutcome | None = None,
    correction: Any = None,
) -> AttemptRecord:
    result = collected.result
    return AttemptRecord(
        attempt=attempt,
        canonical_id=candidate.subject.facts.canonical_id,
        model_id=candidate.subject.facts.model_id,
        runtime_profile_hash=candidate.subject.runtime_profile_hash,
        rank=candidate.rank,
        outcome=outcome,
        started_at=started_at,
        completed_at=completed_at,
        provider_ms=collected.provider_ms,
        ttft_ms=collected.ttft_ms,
        input_tokens=None if result is None else _count(result.usage.tokens.input_tokens),
        output_tokens=None if result is None else _count(result.usage.tokens.output_tokens),
        cache_write_tokens=(
            None if result is None else _count(result.usage.tokens.cache_write_tokens)
        ),
        cache_read_tokens=(
            None if result is None else _count(result.usage.tokens.cache_read_tokens)
        ),
        finish_reason=None if result is None else result.finish_reason.value,
        error_code=None if error is None else type(error).__name__,
        error_text=None if error is None else str(error),
        partial_response_hash=_sha256(collected.text) if collected.text else None,
        prompt_id=None if correction is None else correction.prompt_id,
        prompt_version=None if correction is None else correction.version,
        prompt_sha256=None if correction is None else correction.sha256,
        validation=validation,
        text=collected.text,
    )


@dataclass(frozen=True, slots=True)
class ExecutionContext:
    """Everything one execution needs that is not the request itself.

    Bundled so ``execute`` and ``stream_execute`` take the same inputs, and so a test can drive
    the executor without a live application.

    Attributes:
        provider: The provider to call.
        provider_facts: Its declared capabilities, for routing and for the streaming degradation.
        policy: The configured routing policy.
        schemas_dir: Where a task profile's ``json_schema_ref`` resolves.
        snapshot: The telemetry routing's resource constraints read.
        timeout_seconds: The per-call provider timeout.
        now: The clock. Injected, so a job's timestamps are reproducible in a test.
        sink: The job event sink. When given, the execution's frames — ``routing``, then
            ``result`` or ``error`` — are persisted as job events and published after commit,
            which is what lets a reconnecting client replay them from the table (LCX19). Without
            one, the events are written in P4's shape at the end and nothing is published.
        breakers: The serving process's circuit-breaker registry, so the synchronous path routes
            around open breakers and marks a half-open model's probe exactly as the worker does
            (F3/M5C-3). ``None`` — a one-shot process such as the CLI — raises the
            ``breaker_state_unavailable`` flag on the decision instead of silently passing an
            empty exclusion set.
        resident_models: Canonical IDs currently loaded, for the residency tie-break, or
            ``None`` when no residency tracker exists.
        resident_devices: Canonical ID -> devices the model is resident on, or ``None``.
        residency: The residency tracker. Without it a synchronous execution loads a model into
            the provider and records nothing, so the **next** request sees no resident model,
            cannot apply the residency exception, and is refused ``insufficient_vram`` by the
            memory the previous request is still holding. On a single-GPU machine that makes the
            second stage of any workflow fail.
        in_use_model_ids: Models in-flight jobs hold, which are never evicted.
        vram_headroom_bytes: The per-device reserve.
    """

    provider: Provider
    provider_facts: ProviderFacts
    policy: RoutingPolicy
    schemas_dir: Path
    snapshot: TelemetrySnapshot | None = None
    timeout_seconds: float | None = None
    now: Callable[[], datetime] = field(default=lambda: datetime.now(tz=UTC))
    sink: JobEventSink | None = None
    breakers: CircuitBreakers | None = None
    resident_models: frozenset[str] | None = None
    resident_devices: Mapping[str, frozenset[int]] | None = None
    residency: Any | None = None
    """The residency tracker, so a synchronous execution makes its chosen model resident.

    Typed loosely on purpose: :mod:`loadcoach.services.residency` imports from here, so naming
    ``ResidencyService`` would close an import cycle. The only members used are ``ensure_loaded``
    and ``manageable``.
    """
    in_use_model_ids: frozenset[str] = frozenset()
    """Models in-flight jobs are executing on, which residency must never evict."""
    vram_headroom_bytes: int = 0
    """The per-device reserve, for the eviction arithmetic."""


def existing_job_for_key(
    database: Database, *, source: str, key: str, now: datetime
) -> tuple[str, str] | None:
    """Return ``(job_id, state)`` for an unexpired ``(source, key)``, releasing an expired one.

    The durable half of api.md §4's idempotency: keys are scoped per caller, reserved for
    ``queue.idempotency_ttl_hours`` and then released, so a key reused later starts new work.
    """
    from sqlalchemy import select, update

    with database.write() as session:
        row = session.execute(
            select(Job.id, Job.state, Job.idempotency_expires_at).where(
                Job.source == source, Job.idempotency_key == key
            )
        ).first()
        if row is None:
            return None
        job_id, state, expires_at = row
        if expires_at is not None and expires_at <= now:
            session.execute(
                update(Job)
                .where(Job.id == job_id)
                .values(idempotency_key=None, idempotency_expires_at=None)
            )
            return None
        return str(job_id), str(state)


@dataclass(frozen=True, slots=True)
class ReservedJob:
    """The row a synchronous execution runs against, or the earlier one its key names."""

    job_id: str
    created: bool
    state: str


def reserve_sync_job(
    database: Database,
    request: GenerateRequest,
    *,
    now: datetime,
    ttl_hours: float,
) -> ReservedJob:
    """Reserve the job row for a synchronous execution before it starts (LCX19).

    The row exists from the first instant so a concurrent submission with the same key attaches
    to this execution rather than starting a second one — the unique index on
    ``(source, idempotency_key)`` decides the race. The row is ``executing`` with
    ``state_reason = "synchronous"``: a synchronous request never queues.

    Raises:
        TaskProfileNotFound: No such enabled task profile.
    """
    from sqlalchemy.exc import IntegrityError

    from loadcoach.services.routing import load_task_profile

    profile = load_task_profile(database, request.task)
    if request.idempotency_key is not None:
        existing = existing_job_for_key(
            database, source=request.source, key=request.idempotency_key, now=now
        )
        if existing is not None:
            return ReservedJob(job_id=existing[0], created=False, state=existing[1])
    job_id = new_id()
    transcript = request.transcript()
    try:
        with database.write() as session:
            session.add(
                Job(
                    id=job_id,
                    task_profile_id=profile.profile_id,
                    task_profile_version=profile.version,
                    job_class="interactive",
                    base_priority=800,
                    effective_priority=800,
                    source=request.source,
                    state="executing",
                    state_reason="synchronous",
                    idempotency_key=request.idempotency_key,
                    idempotent=request.idempotency_key is not None,
                    idempotency_expires_at=None
                    if request.idempotency_key is None
                    else now + timedelta(hours=ttl_hours),
                    request_json={
                        "task": request.task,
                        "messages": [
                            {
                                "role": m.role.value,
                                "content": m.content,
                                "tool_call_id": m.tool_call_id,
                            }
                            for m in transcript
                        ],
                        "response_format": request.response_format,
                        "sampling": dict(request.sampling),
                        "stream": request.stream,
                    },
                    prompt_hash=_sha256(
                        "\n".join(f"{m.role.value}:{m.content}" for m in transcript)
                    ),
                    attempt=0,
                    max_attempts=1,
                    created_at=now,
                    started_at=now,
                    degradations_json=[],
                )
            )
    except IntegrityError:
        existing = existing_job_for_key(
            database, source=request.source, key=request.idempotency_key or "", now=now
        )
        if existing is None:  # pragma: no cover — the row that won cannot have vanished
            raise
        return ReservedJob(job_id=existing[0], created=False, state=existing[1])
    return ReservedJob(job_id=job_id, created=True, state="executing")


def _refresh_reliability(
    database: Database,
    records: Sequence[AttemptRecord],
    *,
    task_profile_id: str,
    now: datetime,
) -> None:
    """Fold the written attempts into ``reliability_stats`` (P7's incremental path).

    After the terminal transaction has committed, never inside it, and at a *fresh* clock
    reading: the request's own ``now`` is its start, and an attempt that completed after it would
    be a sample from the future, which the window rule rightly refuses. One recomputation per
    model the attempts touched; a failure is logged and never reaches the caller, because the
    statistics mirror ``job_attempts`` and the next attempt recomputes them from the same rows.
    """
    from loadcoach.services.reliability import recompute_pair

    for model_id in {record.model_id for record in records if record.model_id is not None}:
        try:
            recompute_pair(database, model_id=model_id, task_profile_id=task_profile_id, now=now)
        except Exception:  # noqa: BLE001 — a statistics mirror must never fail a request
            logger.warning(
                "reliability.recompute_failed", extra={"model_id": model_id}, exc_info=True
            )


def _persist(
    database: Database,
    *,
    job_id: str,
    request: GenerateRequest,
    routing: RoutingResult,
    outcome: ExecutionOutcome | None,
    records: Sequence[AttemptRecord],
    error: SuiteError | None,
    now: datetime,
    sink: JobEventSink | None = None,
    existing: bool = False,
) -> None:
    """Write the job, every attempt, its validations and its events, in one transaction.

    Every execution gets a job row, synchronous or not, so every execution has an explanation and
    a history — the alternative is two classes of execution, only one of which can be debugged.
    With ``existing`` the row was reserved up front and is updated; with a ``sink`` the terminal
    frame (``result`` or ``error``) is persisted as an event and published after commit.
    """
    selected = outcome.selected if outcome is not None else None
    scope: AbstractContextManager[tuple[Session, EventWriter | None]] = (
        sink.write(database) if sink is not None else _plain_write(database)
    )
    with scope as (session, events):
        if existing:
            job = session.get_one(Job, job_id)
            job.task_profile_id = routing.task_profile.profile_id
            job.task_profile_version = routing.task_profile.version
            job.state = "completed" if outcome is not None else "failed"
            job.state_reason = "synchronous" if error is None else error.code
            job.response_hash = _sha256(outcome.text) if outcome is not None else None
            job.response_text = outcome.text if outcome is not None else None
            job.structured_output_json = outcome.structured if outcome is not None else None
            job.tool_calls_json = list(outcome.tool_calls) if outcome is not None else None
            job.reasoning_available = outcome is not None and outcome.thinking is not None
            job.reasoning_summary = outcome.thinking if outcome is not None else None
            job.reasoning_source = (
                "provider" if outcome is not None and outcome.thinking is not None else None
            )
            job.selected_model_id = None if selected is None else selected.subject.facts.model_id
            job.runtime_profile_hash = (
                None if selected is None else selected.subject.runtime_profile_hash
            )
            job.served_context = (
                None if selected is None else selected.subject.served_context.tokens
            )
            job.served_context_source = (
                None if selected is None else selected.subject.served_context.source
            )
            job.target_gpu_index = None if selected is None else selected.target_gpu_index
            job.max_attempts = max(len(records), 1)
            job.completed_at = now
            job.provider_ms = outcome.provider_ms if outcome is not None else None
            job.loadcoach_overhead_ms = outcome.overhead_ms if outcome is not None else None
            job.total_ms = outcome.total_ms if outcome is not None else None
            job.ttft_ms = outcome.ttft_ms if outcome is not None else None
            job.input_tokens = outcome.input_tokens if outcome is not None else None
            job.output_tokens = outcome.output_tokens if outcome is not None else None
            job.cache_write_tokens = outcome.cache_write_tokens if outcome is not None else None
            job.cache_read_tokens = outcome.cache_read_tokens if outcome is not None else None
            job.thinking_tokens = outcome.thinking_tokens if outcome is not None else None
            job.validation_passed = outcome.validation.passed if outcome is not None else None
            job.degradations_json = list(outcome.degradations) if outcome is not None else []
            job.error_code = None if error is None else error.code
            job.error_text = None if error is None else error.message
        else:
            session.add(
                Job(
                    id=job_id,
                    task_profile_id=routing.task_profile.profile_id,
                    task_profile_version=routing.task_profile.version,
                    job_class="normal",
                    source=request.source,
                    state="completed" if outcome is not None else "failed",
                    state_reason=None if error is None else error.code,
                    idempotency_key=request.idempotency_key,
                    idempotent=request.idempotency_key is not None,
                    request_json={
                        "task": request.task,
                        "response_format": request.response_format,
                        "sampling": dict(request.sampling),
                        "stream": request.stream,
                    },
                    prompt_hash=_sha256(
                        "\n".join(f"{m.role.value}:{m.content}" for m in request.transcript())
                    ),
                    response_hash=_sha256(outcome.text) if outcome is not None else None,
                    response_text=outcome.text if outcome is not None else None,
                    structured_output_json=outcome.structured if outcome is not None else None,
                    tool_calls_json=list(outcome.tool_calls) if outcome is not None else None,
                    reasoning_available=outcome is not None and outcome.thinking is not None,
                    reasoning_summary=outcome.thinking if outcome is not None else None,
                    reasoning_source="provider"
                    if outcome is not None and outcome.thinking is not None
                    else None,
                    selected_model_id=None if selected is None else selected.subject.facts.model_id,
                    runtime_profile_hash=(
                        None if selected is None else selected.subject.runtime_profile_hash
                    ),
                    served_context=(
                        None if selected is None else selected.subject.served_context.tokens
                    ),
                    served_context_source=(
                        None if selected is None else selected.subject.served_context.source
                    ),
                    target_gpu_index=None if selected is None else selected.target_gpu_index,
                    attempt=0,
                    max_attempts=max(len(records), 1),
                    created_at=now,
                    started_at=now,
                    completed_at=now,
                    provider_ms=outcome.provider_ms if outcome is not None else None,
                    loadcoach_overhead_ms=outcome.overhead_ms if outcome is not None else None,
                    total_ms=outcome.total_ms if outcome is not None else None,
                    ttft_ms=outcome.ttft_ms if outcome is not None else None,
                    input_tokens=outcome.input_tokens if outcome is not None else None,
                    output_tokens=outcome.output_tokens if outcome is not None else None,
                    cache_write_tokens=(
                        outcome.cache_write_tokens if outcome is not None else None
                    ),
                    cache_read_tokens=outcome.cache_read_tokens if outcome is not None else None,
                    thinking_tokens=outcome.thinking_tokens if outcome is not None else None,
                    validation_passed=outcome.validation.passed if outcome is not None else None,
                    degradations_json=list(outcome.degradations) if outcome is not None else [],
                    error_code=None if error is None else error.code,
                    error_text=None if error is None else error.message,
                )
            )
        session.flush()
        link_decision(session, routing.explanation.decision_id, job_id)
        for record in records:
            write_attempt(session, job_id, record, now=now)
        if events is None:
            _write_events(session, job_id, records, outcome=outcome, now=now)
        elif outcome is not None:
            events.append(job_id, "result", now=now, data=outcome.as_json())
        else:
            assert error is not None  # noqa: S101 — one of the two is always set
            events.append(
                job_id,
                "error",
                now=now,
                data={"code": error.code, "message": error.message, **error.details},
            )


@contextmanager
def _plain_write(database: Database) -> Iterator[tuple[Session, EventWriter | None]]:
    """A write scope with no event writer — the no-sink path keeps P4's event shape."""
    with database.write() as session:
        yield session, None


def link_decision(session: Session, decision_id: str, job_id: str) -> None:
    """Point the routing decision at the job it routed, now that the job row exists.

    The explanation lives on the decision row and nowhere else; ``GET /jobs/{id}/explanation``
    is a lookup of the decision whose ``job_id`` matches, never a copy.
    """
    from loadcoach.infrastructure.db.models import RoutingDecision

    decision = session.get(RoutingDecision, decision_id)
    if decision is not None:
        decision.job_id = job_id


def write_attempt(
    session: Session,
    job_id: str,
    record: AttemptRecord,
    *,
    now: datetime,
    owner: str | None = None,
) -> int:
    """Write one attempt row — **the only place ``jobs.attempt`` is ever incremented**.

    ``UPDATE jobs SET attempt = attempt + 1 … RETURNING attempt`` and the ``job_attempts`` insert
    happen in the caller's transaction, so every attempt — first, in-lease corrective retry,
    fallback, or post-requeue — draws the next number from one monotonic sequence and
    ``UNIQUE (job_id, attempt)`` holds (ADR-0029 §2). The claim never touches the counter.

    Args:
        session: The open write session.
        job_id: The job.
        record: The attempt. Its own ``attempt`` number must equal the number the counter
            yields — the caller tracked it from the claim's snapshot — or the single-writer
            invariant has been broken somewhere and the write is refused.
        now: The ``validations.created_at`` instant.
        owner: When given, the job must still be leased to this owner (a worker's fence).

    Returns:
        The attempt number written.

    Raises:
        AttemptRefused: The job is not leased to ``owner`` any more, or the counter and the
            record disagree.
    """
    conditions = [Job.id == job_id]
    if owner is not None:
        conditions.append(Job.lease_owner == owner)
    number = session.execute(
        update(Job).where(*conditions).values(attempt=Job.attempt + 1).returning(Job.attempt)
    ).scalar_one_or_none()
    if number is None:
        raise AttemptRefused(
            f"Attempt {record.attempt} of job {job_id} was not written: the job is no longer "
            f"leased to {owner!r}.",
            details={"job_id": job_id, "attempt": record.attempt, "owner": owner},
        )
    if number != record.attempt:
        raise AttemptRefused(
            f"Attempt numbering diverged on job {job_id}: the counter yielded {number} but the "
            f"attempt was recorded as {record.attempt}.",
            details={"job_id": job_id, "attempt": record.attempt, "counter": number},
        )
    attempt_row = JobAttempt(
        job_id=job_id,
        attempt=number,
        model_id=record.model_id,
        runtime_profile_hash=record.runtime_profile_hash,
        rank=record.rank,
        started_at=record.started_at,
        completed_at=record.completed_at,
        outcome=record.outcome,
        provider_ms=record.provider_ms,
        ttft_ms=record.ttft_ms,
        input_tokens=record.input_tokens,
        output_tokens=record.output_tokens,
        cache_write_tokens=record.cache_write_tokens,
        cache_read_tokens=record.cache_read_tokens,
        finish_reason=record.finish_reason,
        error_code=record.error_code,
        error_text=record.error_text,
        partial_response_hash=record.partial_response_hash,
        prompt_id=record.prompt_id,
        prompt_version=record.prompt_version,
        prompt_sha256=record.prompt_sha256,
    )
    session.add(attempt_row)
    session.flush()
    if record.validation is not None:
        for check in record.validation.checks:
            session.add(
                ValidationRow(
                    job_attempt_id=attempt_row.id,
                    kind=check.kind,
                    passed=check.passed,
                    detail_json=check.detail,
                    created_at=now,
                )
            )
    return int(number)


def _write_events(
    session: Session,
    job_id: str,
    records: Sequence[AttemptRecord],
    *,
    outcome: ExecutionOutcome | None,
    now: datetime,
) -> None:
    """Persist the job's event stream, which is what a reconnecting client replays from.

    Token deltas are **not** persisted: one row per token would dominate the database for no
    benefit a reconnecting client can use — it resumes from the last event it saw and receives the
    terminal ``result``, which carries the whole output.
    """
    sequence = 0

    def append(event_type: str, data: Mapping[str, Any], message: str | None = None) -> None:
        nonlocal sequence
        sequence += 1
        session.add(
            JobEvent(
                job_id=job_id,
                sequence=sequence,
                timestamp=now,
                event_type=event_type,
                message=message,
                data_json=dict(data),
            )
        )

    append("job.executing", {})
    for record in records:
        append(
            "job.retrying" if record.attempt > 1 else "job.executing",
            record.as_json(),
            message=f"attempt {record.attempt} on {record.canonical_id}: {record.outcome}",
        )
    if outcome is not None:
        append("job.completed", outcome.as_json())
    else:
        append("job.failed", {})


def execute(
    database: Database,
    request: GenerateRequest,
    context: ExecutionContext,
    *,
    cancel: CancellationToken | None = None,
    on_chunk: Callable[[StreamChunk], None] | None = None,
    job_id: str | None = None,
    principal: Principal | None = None,
) -> ExecutionOutcome:
    """Route, execute, validate and record one generation.

    Args:
        database: The application's database handle.
        request: The caller's request.
        context: The provider, policy and clock this execution runs against.
        cancel: A cancellation token, honoured within one chunk because the provider is always
            called through ``stream()``.
        on_chunk: Called with each token, tool call and stage as it happens. The streaming
            endpoint supplies one; ``POST /generate`` does not, and the two paths are otherwise
            identical.
        job_id: The row :func:`reserve_sync_job` created, or ``None`` to create the row at the
            end (the direct, no-reservation path a test drives).

    Returns:
        The :class:`ExecutionOutcome`.

    Raises:
        TaskProfileNotFound: No such enabled task profile.
        NoEligibleModel: Nothing survived the hard constraints; every candidate and its reason is
            in ``details``.
        AllCandidatesFailed: Every ranked candidate was tried and failed.
        SchemaUnsupported: The profile's schema uses a keyword the validator cannot check.
    """
    authorize(principal, "write")
    started = time.perf_counter()
    now = context.now()
    existing = job_id is not None
    job_id = job_id if job_id is not None else new_id()

    caller_text = "".join(message.content for message in request.transcript())
    try:
        routing = route(
            database,
            RouteRequest(
                task=request.task,
                estimated_input_tokens=estimate_input_tokens(caller_text),
                max_output_tokens=cast("int | None", request.sampling.get("max_output_tokens")),
                overrides=request.overrides or RuntimeOverrides(),
            ),
            provider=context.provider_facts,
            policy=context.policy,
            snapshot=context.snapshot,
            resident_models=context.resident_models or frozenset(),
            open_circuit_breakers=(
                None if context.breakers is None else context.breakers.excluded()
            ),
            circuit_breaker_details=(
                None if context.breakers is None else context.breakers.details()
            ),
            resident_devices=context.resident_devices,
            now=now,
        )
    except SuiteError as exc:
        if existing:
            _fail_reserved(database, job_id, exc, now=now, sink=context.sink)
        raise
    profile = routing.task_profile
    schema = load_task_schema(
        cast("str | None", profile.execution.get("json_schema_ref")),
        schemas_dir=context.schemas_dir,
    )
    if context.sink is not None and existing:
        # Persisted and published: a client that reconnects gets it from the table, and a live
        # subscriber that attached before routing finished gets it from the broker.
        with context.sink.write(database) as (session, events):
            link_decision(session, routing.explanation.decision_id, job_id)
            events.append(job_id, "routing", now=now, data=routing.explanation.payload)
    if on_chunk is not None:
        on_chunk(StreamChunk("routing", routing.explanation.payload))

    degradations: list[str] = []
    if not context.provider_facts.supports_streaming:
        degradations.append(_DEGRADATION_NO_STREAMING)

    records, selected, collected, validation = _execute_attempts(
        context.provider,
        request,
        routing,
        schema=schema,
        execution_policy=profile.execution,
        validation_policy=profile.validation,
        timeout_seconds=context.timeout_seconds,
        cancel=cancel,
        on_chunk=on_chunk,
        now=context.now,
        breakers=context.breakers,
        residency=context.residency,
        residency_inputs={
            "in_use_model_ids": context.in_use_model_ids,
            "vram_headroom_bytes": context.vram_headroom_bytes,
            "free_bytes_by_gpu": (
                free_vram_by_gpu(context.snapshot) if context.snapshot is not None else {}
            ),
        },
    )

    total_ms = int((time.perf_counter() - started) * 1000)
    provider_ms = sum(record.provider_ms or 0 for record in records)

    if selected is None or collected is None or collected.result is None:
        failure = AllCandidatesFailed(
            "Every ranked candidate was skipped: a circuit-breaker probe is already in flight "
            "on each."
            if not records
            else "Every candidate was tried and every attempt failed.",
            details={
                "job_id": job_id,
                "decision_id": routing.explanation.decision_id,
                "attempts": [record.as_json() for record in records],
            },
        )
        _persist(
            database,
            job_id=job_id,
            request=request,
            routing=routing,
            outcome=None,
            records=records,
            error=failure,
            now=now,
            sink=context.sink,
            existing=existing,
        )
        _refresh_reliability(
            database, records, task_profile_id=routing.task_profile.profile_id, now=context.now()
        )
        raise failure

    result = collected.result
    outcome = ExecutionOutcome(
        job_id=job_id,
        status="completed",
        text=collected.text,
        structured=validation.parsed if validation.performed else None,
        tool_calls=tuple(collected.tool_calls),
        thinking=collected.thinking or None,
        routing=routing,
        selected=selected,
        attempts=tuple(records),
        validation=validation,
        degradations=tuple(degradations),
        total_ms=total_ms,
        provider_ms=provider_ms,
        # Overhead is what LoadCoach itself spent: routing, validation, assembly and persistence.
        # It is wall time minus provider time, never a share of one combined figure.
        overhead_ms=max(total_ms - provider_ms, 0),
        ttft_ms=records[-1].ttft_ms if records else None,
        input_tokens=_count(result.usage.tokens.input_tokens),
        output_tokens=_count(result.usage.tokens.output_tokens),
        cache_write_tokens=_count(result.usage.tokens.cache_write_tokens),
        cache_read_tokens=_count(result.usage.tokens.cache_read_tokens),
        thinking_tokens=_count(result.usage.thinking_tokens),
    )
    _persist(
        database,
        job_id=job_id,
        request=request,
        routing=routing,
        outcome=outcome,
        records=records,
        error=None,
        now=now,
        sink=context.sink,
        existing=existing,
    )
    _refresh_reliability(
        database, records, task_profile_id=routing.task_profile.profile_id, now=context.now()
    )
    if on_chunk is not None:
        on_chunk(StreamChunk("result", outcome.as_json()))
    return outcome


def _fail_reserved(
    database: Database,
    job_id: str,
    error: SuiteError,
    *,
    now: datetime,
    sink: JobEventSink | None,
) -> None:
    """A reserved row whose execution failed before any attempt: mark it, persist the error."""
    scope: AbstractContextManager[tuple[Session, EventWriter | None]] = (
        sink.write(database) if sink is not None else _plain_write(database)
    )
    with scope as (session, events):
        job = session.get_one(Job, job_id)
        job.state = "failed"
        job.state_reason = error.code
        job.error_code = error.code
        job.error_text = error.message
        job.completed_at = now
        decision_id = error.details.get("decision_id")
        if isinstance(decision_id, str):
            link_decision(session, decision_id, job_id)
        if events is not None:
            events.append(
                job_id,
                "error",
                now=now,
                data={"code": error.code, "message": error.message, **error.details},
            )


def stream_execute(
    database: Database,
    request: GenerateRequest,
    context: ExecutionContext,
    *,
    on_chunk: Callable[[StreamChunk], None],
    cancel: CancellationToken | None = None,
) -> None:
    """Run :func:`execute`, handing each chunk to ``on_chunk`` **as the provider produces it**.

    The callback is the stream. An earlier version of this function collected the chunks into a
    list and returned an iterator over it, which is not streaming at all: every token arrived at
    once, after the generation had already finished. A caller that wants a list can append to one.

    Synchronous, like everything below the HTTP edge (ADR-0003 §3): the web layer runs this in a
    worker thread and fans the chunks out to the SSE response.

    Args:
        database: The application's database handle.
        request: The caller's request.
        context: The provider, policy and clock this execution runs against.
        on_chunk: Called with each routing decision, token, tool call and terminal frame.
        cancel: A cancellation token, honoured within one chunk.
    """
    try:
        execute(database, request, context, cancel=cancel, on_chunk=on_chunk)
    except SuiteError as exc:
        on_chunk(StreamChunk("error", {"code": exc.code, "message": exc.message, **exc.details}))


def provider_facts_for(provider: Provider | None) -> ProviderFacts:
    """Read the provider's declared capabilities into routing's own value type.

    A provider that cannot be reached at all reports ``healthy=False`` rather than raising: with
    no healthy provider every candidate is rejected by ``model_unavailable``, which is a routing
    answer with reasons attached, not a server error. Lives here rather than in the web layer
    because the queue worker needs it too, and ``services`` cannot import ``web``.

    Args:
        provider: The application's provider handle, or ``None`` when none is configured.

    Returns:
        The facts routing's constraint filter reads.
    """
    if provider is None:
        return ProviderFacts(healthy=False)
    try:
        capabilities = provider.capabilities()
        health = provider.health()
    except ProviderError:
        return ProviderFacts(healthy=False)
    return ProviderFacts(
        # DEGRADED still serves requests, so it is not "unavailable"; only UNAVAILABLE removes
        # every candidate from routing.
        healthy=health.status is not ProviderStatus.UNAVAILABLE,
        context_configurable=capabilities.context_configurable,
        supports_tool_use=capabilities.tool_calling,
        supports_structured_output=capabilities.structured_output,
        supports_streaming=capabilities.streaming,
        is_remote=health.is_remote,
    )
