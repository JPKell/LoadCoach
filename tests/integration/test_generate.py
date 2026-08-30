"""POST /generate end to end: the result, the timings, the job record, and the prompt passthrough.

The most load-bearing test here is the byte-for-byte one. Spec §9 promises that a caller's text
reaches the provider unmodified, and IdeaPress's own per-attempt provenance records the hash of
what it sent — a record that would be a lie if LoadCoach altered the text. So the transcript
ModelRack actually received is captured and compared against what the caller supplied.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from baseaicore import ModelDescriptor, ModelIdentity
from modelrack import (
    GenerationRequest,
    GenerationResult,
    Message,
    ProviderCapabilities,
    ProviderHealth,
    Role,
    StreamEvent,
)
from modelrack.testing import (
    FakeFailure,
    FakeFailureMode,
    FakeGeneration,
    FakeModel,
    FakeProvider,
    FakeScript,
)

from loadcoach.domain.routing.subject import ProviderFacts
from loadcoach.services.database import Database, ensure_ready
from loadcoach.services.execution import (
    AllCandidatesFailed,
    ExecutionContext,
    GenerateRequest,
    execute,
)
from loadcoach.services.models import discover_models
from loadcoach.services.routing import RoutingPolicy
from loadcoach.services.task_profiles import (
    DEFAULT_SCHEMAS_DIR,
    import_task_profiles,
    read_task_profiles_file,
)

GIB = 1024**3
NOW = datetime(2026, 8, 29, 12, 0, 0, tzinfo=UTC)


class RecordingProvider:
    """A FakeProvider that keeps every request it was handed.

    Not a mock of the provider interface: it delegates every call to the real fake, so the
    transcript captured here is the one that actually travelled through ModelRack's own request
    construction rather than one this test built for itself.
    """

    def __init__(self, fake: FakeProvider) -> None:
        self._fake = fake
        self.requests: list[GenerationRequest] = []

    @property
    def kind(self) -> Any:
        return self._fake.kind

    def health(self) -> ProviderHealth:
        return self._fake.health()

    def capabilities(self) -> ProviderCapabilities:
        return self._fake.capabilities()

    def list_models(self, *, refresh: bool = False) -> Sequence[ModelDescriptor]:
        return self._fake.list_models(refresh=refresh)

    def inspect_model(self, identity: ModelIdentity, *, refresh: bool = False) -> ModelDescriptor:
        return self._fake.inspect_model(identity, refresh=refresh)

    def resolve(self, reference: str, *, refresh: bool = False) -> ModelIdentity:
        return self._fake.resolve(reference, refresh=refresh)

    def generate(self, request: GenerationRequest) -> GenerationResult:
        self.requests.append(request)
        return self._fake.generate(request)

    def stream(self, request: GenerationRequest) -> Iterator[StreamEvent]:
        self.requests.append(request)
        yield from self._fake.stream(request)

    def load(self, identity: ModelIdentity, profile: Any) -> Any:
        return self._fake.load(identity, profile)

    def unload(self, identity: ModelIdentity) -> bool:
        return self._fake.unload(identity)

    def list_resident(self) -> Sequence[Any]:
        return self._fake.list_resident()


def _model(name: str = "alpha:8b", digest: str = "a" * 64, **overrides: Any) -> FakeModel:
    defaults: dict[str, Any] = {
        "family": name.split(":")[0],
        "parameter_count": 8_000_000_000,
        "quantization": "Q8_0",
        "size_bytes": 2 * GIB,
        "max_context": 32768,
        "layers": 32,
        "kv_heads": 8,
        "head_dim": 128,
    }
    defaults.update(overrides)
    return FakeModel(name=name, digest=digest, **defaults)


def _setup(tmp_path: Path, script: FakeScript | None = None) -> tuple[Database, RecordingProvider]:
    fake = FakeProvider(script or FakeScript(models=(_model(),)))
    provider = RecordingProvider(fake)
    database = Database.from_url(f"sqlite:///{tmp_path / 'exec.sqlite3'}")
    ensure_ready(database, auto_migrate=True)
    import_task_profiles(database, read_task_profiles_file(), now=NOW)
    discover_models(database, fake, now=NOW)
    return database, provider


def _context(provider: Any, **facts: Any) -> ExecutionContext:
    defaults: dict[str, Any] = {
        "healthy": True,
        "context_configurable": True,
        "supports_tool_use": True,
        "supports_structured_output": True,
        "supports_streaming": True,
    }
    defaults.update(facts)
    return ExecutionContext(
        provider=provider,
        provider_facts=ProviderFacts(**defaults),
        policy=RoutingPolicy(),
        schemas_dir=DEFAULT_SCHEMAS_DIR,
        now=lambda: NOW,
    )


# --- the promise that matters -------------------------------------------------------------


def test_the_callers_prompt_reaches_the_provider_byte_for_byte(tmp_path: Path) -> None:
    """Spec §9. IdeaPress records the hash of what it sent; that record must not be a lie."""
    database, provider = _setup(tmp_path)
    system = "You are drafting one section of an article. Follow every hard requirement…"
    prompt = "Write a 600-word section on local inference privacy.\n\n{{ not a template }}"
    try:
        execute(
            database,
            GenerateRequest(task="content.article_draft", system=system, prompt=prompt),
            _context(provider),
        )
    finally:
        database.close()

    assert len(provider.requests) == 1
    sent = provider.requests[0].messages
    assert [(m.role, m.content) for m in sent] == [
        (Role.SYSTEM, system),
        (Role.USER, prompt),
    ]
    # Nothing of LoadCoach's own is prepended, appended or substituted.
    assert len(sent) == 2
    assert all("LoadCoach" not in m.content for m in sent)


def test_a_caller_supplied_transcript_is_forwarded_unchanged(tmp_path: Path) -> None:
    database, provider = _setup(tmp_path)
    transcript = (
        Message(role=Role.SYSTEM, content="Be terse."),
        Message(role=Role.USER, content="One."),
        Message(role=Role.ASSISTANT, content="Two."),
        Message(role=Role.USER, content="Three."),
    )
    try:
        execute(
            database,
            GenerateRequest(task="general.chat", messages=transcript),
            _context(provider),
        )
    finally:
        database.close()
    assert provider.requests[0].messages == transcript


def test_the_provider_is_called_with_the_full_identity_not_a_bare_name(tmp_path: Path) -> None:
    """A tag can be repointed between discovery and execution (ADR-0024 §2)."""
    database, provider = _setup(tmp_path)
    try:
        execute(database, GenerateRequest(task="general.chat", prompt="hi"), _context(provider))
    finally:
        database.close()
    identity = provider.requests[0].identity
    assert identity.artifact_digest is not None
    assert identity.artifact_digest.endswith("a" * 12)


# --- the result -----------------------------------------------------------------------------


def test_generate_returns_a_result_with_routing_metadata_and_separated_timings(
    tmp_path: Path,
) -> None:
    """Acceptance criterion 1."""
    database, provider = _setup(tmp_path)
    try:
        outcome = execute(
            database,
            GenerateRequest(
                task="content.article_draft", prompt="Write about local inference privacy."
            ),
            _context(provider),
        )
    finally:
        database.close()

    body = outcome.as_json()
    assert body["status"] == "completed"
    assert body["output"]["text"]
    assert body["model"]["canonical_id"].startswith("fake/alpha:8b@")
    assert body["model"]["runtime_profile_hash"]
    assert body["model"]["served_context"] > 0
    assert body["model"]["served_context_source"] in {"configured", "reported", "assumed"}
    assert body["routing"]["decision_id"]
    assert body["routing"]["explanation_url"].endswith("/explanation")
    assert body["usage"]["input_tokens"] is not None
    assert body["timing"]["provider_ms"] >= 0
    assert body["timing"]["loadcoach_overhead_ms"] >= 0
    # Separated, never combined: two figures that happen to sum to the total.
    assert body["timing"]["total_ms"] >= body["timing"]["provider_ms"]
    assert (
        body["timing"]["total_ms"]
        >= body["timing"]["provider_ms"] + body["timing"]["loadcoach_overhead_ms"] - 1
    )
    assert body["attempts"][0]["outcome"] == "completed"


def test_reasoning_is_absent_unless_the_provider_returned_it(tmp_path: Path) -> None:
    """Spec §9: LoadCoach never synthesizes or infers hidden chain-of-thought."""
    database, provider = _setup(tmp_path)
    try:
        plain = execute(
            database, GenerateRequest(task="general.chat", prompt="hi"), _context(provider)
        )
    finally:
        database.close()
    assert plain.as_json()["reasoning"] == {
        "available": False,
        "summary": None,
        "source": None,
    }

    thinking_script = FakeScript(
        models=(_model(),),
        generations=(FakeGeneration(text="answer", thinking="deliberation"),),
    )
    database, provider = _setup(tmp_path / "b", thinking_script)
    try:
        with_thinking = execute(
            database, GenerateRequest(task="general.chat", prompt="hi"), _context(provider)
        )
    finally:
        database.close()
    reasoning = with_thinking.as_json()["reasoning"]
    assert reasoning["available"] is True
    assert reasoning["summary"] == "deliberation"
    assert reasoning["source"] == "provider"


def test_every_execution_gets_a_job_row_with_its_attempts_and_events(tmp_path: Path) -> None:
    """A synchronous request is still a job, or only half the executions can be debugged."""
    from loadcoach.infrastructure.db.models import Job, JobAttempt, JobEvent, RoutingDecision

    database, provider = _setup(tmp_path)
    try:
        outcome = execute(
            database, GenerateRequest(task="general.chat", prompt="hi"), _context(provider)
        )
        with database.read() as session:
            job = session.get(Job, outcome.job_id)
            assert job is not None
            assert job.state == "completed"
            assert job.selected_model_id is not None
            assert job.runtime_profile_hash
            assert job.served_context_source is not None
            assert job.response_text == outcome.text
            assert job.prompt_hash and job.prompt_hash.startswith("sha256:")

            attempts = session.query(JobAttempt).filter_by(job_id=outcome.job_id).all()
            assert [a.attempt for a in attempts] == [1]
            events = session.query(JobEvent).filter_by(job_id=outcome.job_id).all()
            assert {e.event_type for e in events} >= {"job.executing", "job.completed"}
            assert sorted(e.sequence for e in events) == list(range(1, len(events) + 1))

            decision = session.get(RoutingDecision, outcome.routing.explanation.decision_id)
            assert decision is not None
            assert decision.job_id == outcome.job_id
    finally:
        database.close()


# --- failure paths ---------------------------------------------------------------------------


def test_a_provider_error_is_recorded_and_reported_with_every_attempt(tmp_path: Path) -> None:
    script = FakeScript(
        models=(_model(),),
        generations=(FakeGeneration(failure=FakeFailure(mode=FakeFailureMode.UNAVAILABLE)),),
    )
    database, provider = _setup(tmp_path, script)
    try:
        with pytest.raises(AllCandidatesFailed) as caught:
            execute(database, GenerateRequest(task="general.chat", prompt="hi"), _context(provider))
        attempts = caught.value.details["attempts"]
        assert attempts
        assert attempts[0]["outcome"] == "provider_error"
        assert attempts[0]["error_code"]

        from loadcoach.infrastructure.db.models import Job

        with database.read() as session:
            job = session.get(Job, caught.value.details["job_id"])
            assert job is not None
            assert job.state == "failed"
            assert job.error_code == "ALL_CANDIDATES_FAILED"
    finally:
        database.close()


def test_a_timeout_is_recorded_as_a_provider_error_and_falls_back(tmp_path: Path) -> None:
    script = FakeScript(
        models=(_model(), _model("beta:8b", "b" * 64)),
        generations=(
            FakeGeneration(failure=FakeFailure(mode=FakeFailureMode.TIMEOUT)),
            FakeGeneration(text="the fallback answered"),
        ),
        repeat_final_generation=True,
    )
    database, provider = _setup(tmp_path, script)
    try:
        outcome = execute(
            database, GenerateRequest(task="general.chat", prompt="hi"), _context(provider)
        )
    finally:
        database.close()
    assert outcome.text == "the fallback answered"
    assert len(outcome.attempts) >= 2
    assert outcome.attempts[0].outcome == "timeout"  # data model §2's own outcome vocabulary
    assert outcome.attempts[-1].outcome == "completed"
    # A fallback is never silent: both attempts are on the record, on different models.
    assert outcome.attempts[0].canonical_id != outcome.attempts[-1].canonical_id


def test_a_cancelled_generation_is_terminal_and_never_retried(tmp_path: Path) -> None:
    from modelrack import CancellationToken

    database, provider = _setup(tmp_path)
    token = CancellationToken()
    token.cancel()
    try:
        with pytest.raises(AllCandidatesFailed) as caught:
            execute(
                database,
                GenerateRequest(task="general.chat", prompt="hi"),
                _context(provider),
                cancel=token,
            )
        attempts = caught.value.details["attempts"]
        assert [a["outcome"] for a in attempts] == ["cancelled"]
    finally:
        database.close()


def test_a_provider_that_cannot_stream_records_the_degradation(tmp_path: Path) -> None:
    """api.md §5: the limit is visible rather than assumed away."""
    database, provider = _setup(tmp_path)
    try:
        outcome = execute(
            database,
            GenerateRequest(task="general.chat", prompt="hi"),
            _context(provider, supports_streaming=False),
        )
    finally:
        database.close()
    assert "cancellation_deferred_to_completion" in outcome.degradations


def test_a_streaming_provider_records_no_such_degradation(tmp_path: Path) -> None:
    database, provider = _setup(tmp_path)
    try:
        outcome = execute(
            database, GenerateRequest(task="general.chat", prompt="hi"), _context(provider)
        )
    finally:
        database.close()
    assert outcome.degradations == ()
