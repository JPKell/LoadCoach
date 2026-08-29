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
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, ClassVar, cast

from baseaicore import ModelIdentity, ProviderKind, SuiteError, is_supported, new_id
from modelrack import (
    CancellationToken,
    GenerationCancelled,
    GenerationRequest,
    GenerationResult,
    Message,
    ProviderError,
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

from loadcoach.domain.routing.context_budget import estimate_input_tokens
from loadcoach.domain.routing.subject import RuntimeOverrides
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
    from collections.abc import Callable, Mapping, Sequence
    from pathlib import Path

    from modelrack.provider import Provider
    from sqlalchemy.orm import Session
    from sweatmeter import TelemetrySnapshot

    from loadcoach.domain.routing.ranking import RankedCandidate
    from loadcoach.domain.routing.subject import ModelFacts, ProviderFacts
    from loadcoach.services.database import Database

__all__ = [
    "AllCandidatesFailed",
    "AttemptRecord",
    "ExecutionOutcome",
    "GenerateRequest",
    "ProviderFailed",
    "StreamChunk",
    "execute",
    "load_task_schema",
    "stream_execute",
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
    thinking_tokens: int | None

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
                "thinking_tokens": self.thinking_tokens
                if self.thinking_tokens is not None
                else "unsupported",
            },
            "timing": {
                "total_ms": self.total_ms,
                "provider_ms": self.provider_ms,
                "loadcoach_overhead_ms": self.overhead_ms,
                "ttft_ms": self.ttft_ms,
                "queue_wait_ms": 0,
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
    parsed: dict[str, Any] = json.loads((schemas_dir / schema_ref).read_text(encoding="utf-8"))
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
) -> tuple[list[AttemptRecord], RankedCandidate | None, _Collected | None, ValidationOutcome]:
    """Try each ranked candidate, retrying correctively where the profile permits."""
    ranking = routing.explanation.ranking
    candidates = [ranking.primary, *ranking.fallbacks]
    max_attempts = int(cast("int", execution_policy.get("max_attempts", 1)))
    base_format = request.response_format or str(execution_policy.get("response_format", "text"))
    sampling = SamplingParameters(
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
    transcript = request.transcript()

    records: list[AttemptRecord] = []
    outcome = ValidationOutcome(performed=False, passed=None, checks=())
    attempt_number = 0

    for candidate in candidates:
        if candidate is None:
            continue
        subject = candidate.subject
        turns = transcript
        correction: Any = None

        for _ in range(max_attempts):
            attempt_number += 1
            started_at = now()
            call = GenerationRequest(
                identity=_identity_of(subject.facts),
                messages=turns,
                runtime_profile=subject.runtime_profile,
                sampling=sampling,
                response_format=_response_format(base_format, schema),
                timeout_seconds=timeout_seconds,
                cancel=cancel,
            )
            collected = _run_stream(provider, call, on_chunk=on_chunk)

            if collected.failure is not None:
                records.append(
                    _record(
                        attempt_number,
                        candidate,
                        started_at,
                        now(),
                        collected,
                        outcome="cancelled"
                        if isinstance(collected.failure, GenerationCancelled)
                        else "provider_error",
                        error=collected.failure,
                        correction=correction,
                    )
                )
                if isinstance(collected.failure, GenerationCancelled):
                    return records, candidate, collected, outcome
                break  # this candidate is not working; fall back

            outcome = _validate(collected.text, validation_policy=validation_policy, schema=schema)
            passed = outcome.passed is not False
            records.append(
                _record(
                    attempt_number,
                    candidate,
                    started_at,
                    now(),
                    collected,
                    outcome="completed" if passed else "validation_failed",
                    validation=outcome,
                    correction=correction,
                )
            )
            if passed:
                return records, candidate, collected, outcome

            if attempt_number >= max_attempts * len(candidates):
                break
            # A corrective retry: a NEW attempt with LoadCoach's own prompt appended to the
            # caller's transcript. The caller's turns are never rewritten, only followed.
            correction = render_corrective_retry(
                problems=_problems_text(outcome),
                schema=json.dumps(schema, indent=2, sort_keys=True) if schema else "{}",
                previous_output=collected.text,
            )
            turns = (
                *transcript,
                Message(role=Role.ASSISTANT, content=collected.text),
                *(
                    (Message(role=Role.SYSTEM, content=correction.system),)
                    if correction.system
                    else ()
                ),
                Message(role=Role.USER, content=correction.user),
            )

    return records, None, None, outcome


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
    """

    provider: Provider
    provider_facts: ProviderFacts
    policy: RoutingPolicy
    schemas_dir: Path
    snapshot: TelemetrySnapshot | None = None
    timeout_seconds: float | None = None
    now: Callable[[], datetime] = field(default=lambda: datetime.now(tz=UTC))


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
) -> None:
    """Write the job, every attempt, its validations and its events, in one transaction.

    Every execution gets a job row, synchronous or not, so every execution has an explanation and
    a history — the alternative is two classes of execution, only one of which can be debugged.
    """
    selected = outcome.selected if outcome is not None else None
    with database.write() as session:
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
                attempt=len(records),
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
                thinking_tokens=outcome.thinking_tokens if outcome is not None else None,
                validation_passed=outcome.validation.passed if outcome is not None else None,
                degradations_json=list(outcome.degradations) if outcome is not None else [],
                error_code=None if error is None else error.code,
                error_text=None if error is None else error.message,
            )
        )
        session.flush()
        _link_decision(session, routing.explanation.decision_id, job_id)
        for record in records:
            _write_attempt(session, job_id, record, now=now)
        _write_events(session, job_id, records, outcome=outcome, now=now)


def _link_decision(session: Session, decision_id: str, job_id: str) -> None:
    """Point the routing decision at the job it routed, now that the job row exists."""
    from loadcoach.infrastructure.db.models import RoutingDecision

    decision = session.get(RoutingDecision, decision_id)
    if decision is not None:
        decision.job_id = job_id


def _write_attempt(session: Session, job_id: str, record: AttemptRecord, *, now: datetime) -> None:
    attempt_row = JobAttempt(
        job_id=job_id,
        attempt=record.attempt,
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
    if record.validation is None:
        return
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

    Returns:
        The :class:`ExecutionOutcome`.

    Raises:
        TaskProfileNotFound: No such enabled task profile.
        NoEligibleModel: Nothing survived the hard constraints; every candidate and its reason is
            in ``details``.
        AllCandidatesFailed: Every ranked candidate was tried and failed.
        SchemaUnsupported: The profile's schema uses a keyword the validator cannot check.
    """
    started = time.perf_counter()
    now = context.now()
    job_id = new_id()

    caller_text = "".join(message.content for message in request.transcript())
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
        now=now,
    )
    profile = routing.task_profile
    schema = load_task_schema(
        cast("str | None", profile.execution.get("json_schema_ref")),
        schemas_dir=context.schemas_dir,
    )
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
    )

    total_ms = int((time.perf_counter() - started) * 1000)
    provider_ms = sum(record.provider_ms or 0 for record in records)

    if selected is None or collected is None or collected.result is None:
        failure = AllCandidatesFailed(
            "Every candidate was tried and every attempt failed.",
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
    )
    if on_chunk is not None:
        on_chunk(StreamChunk("result", outcome.as_json()))
    return outcome


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
