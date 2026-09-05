"""The corrective retry survives an empty answer, and a refused request writes its attempts.

Both halves are G1's, found on the real stack (`docs/history/G1_HANDOFF.md` §9.2). A reasoning
model under JSON mode returns nothing about half the time; `corrective_turns` then appended
`Message(ASSISTANT, content="")`, which ModelRack refuses, and the refusal escaped mid-execution:
`/generate` answered `VALIDATION_ERROR`, the job stayed `executing` until a watchdog or a cancel,
and **its attempts were never written** — which is why the `finish_reason` behind those empty
answers is unrecoverable to this day. The second half is the one that matters more: whatever is
refused, the rows survive.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import pytest
from baseaicore import ValidationError
from modelrack import FinishReason
from modelrack.testing import FakeGeneration, FakeProvider, FakeScript
from tests.integration.test_generate import RecordingProvider, _context, _model, _setup

from loadcoach.domain.validation import ValidationOutcome
from loadcoach.services import execution
from loadcoach.services.execution import (
    ExecutionContext,
    GenerateRequest,
    corrective_turns,
    execute,
    reserve_sync_job,
)
from loadcoach.services.task_profiles import DEFAULT_SCHEMAS_DIR

if TYPE_CHECKING:
    from pathlib import Path

NOW = datetime(2026, 8, 29, 12, 0, 0, tzinfo=UTC)
GOOD = json.dumps(
    {
        "summary": "ok",
        "findings": [{"path": "a.py", "line": 3, "severity": "minor", "description": "x"}],
    }
)


def _empty_then(*outputs: str, tmp_path: Path) -> tuple[Any, RecordingProvider]:
    """A provider that answers with nothing, then with whatever follows."""
    script = FakeScript(
        models=(_model(),),
        generations=tuple(FakeGeneration(text=text) for text in ("", *outputs)),
        repeat_final_generation=True,
    )
    return _setup(tmp_path, script)


# --- (a) the corrective never replays an empty answer ----------------------------------------


def test_corrective_turns_does_not_append_an_empty_assistant_turn() -> None:
    """The unit of G1's crash: an empty previous answer is described, never replayed."""
    turns, correction = corrective_turns(
        (),
        previous_text="",
        outcome=ValidationOutcome(performed=True, passed=False, checks=()),
        schema=None,
    )
    assert all(turn.content for turn in turns), "no turn may be empty; ModelRack refuses one"
    assert [turn.role.value for turn in turns] == ["system", "user"]
    assert "no output at all" in correction.user


def test_corrective_turns_still_replays_a_non_empty_answer() -> None:
    """The happy path is unchanged: the model's own words are put back for it to correct."""
    turns, _ = corrective_turns(
        (),
        previous_text='{"summary": "only a summary"}',
        outcome=ValidationOutcome(performed=True, passed=False, checks=()),
        schema=None,
    )
    assert [turn.role.value for turn in turns] == ["assistant", "system", "user"]
    assert turns[0].content == '{"summary": "only a summary"}'


def test_an_empty_first_answer_no_longer_crashes_the_corrective(tmp_path: Path) -> None:
    """The G1 regression, end to end: empty answer, corrective retry, completed job."""
    database, provider = _empty_then(GOOD, tmp_path=tmp_path)
    try:
        outcome = execute(
            database,
            GenerateRequest(task="code.review", prompt="review this diff"),
            _context(provider),
        )
        assert outcome.status == "completed"
        assert len(outcome.attempts) == 2
        assert outcome.attempts[0].outcome == "validation_failed"
        assert outcome.attempts[1].prompt_id == "execution.structured_output.retry"
    finally:
        database.close()
    # The second request carried no empty turn — the shape ModelRack refuses.
    assert all(message.content or message.tool_calls for message in provider.requests[1].messages)


# --- (b) a refused request fails the job with its attempts written ---------------------------


def test_a_refused_request_fails_the_job_and_writes_its_attempts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Item 3(b). Whatever the refusal, the rows that explain it are committed.

    The refusal is injected at the corrective boundary rather than provoked, because the shape
    that provoked it at G1 is exactly the one the fix above removed. What is asserted is the
    handling: a `failed` job, `VALIDATION_ERROR`, and the first attempt readable with its
    `finish_reason` — the fact G1 could not recover.
    """
    from loadcoach.infrastructure.db.models import Job, JobAttempt

    def refuse(*args: object, **kwargs: object) -> None:
        raise ValidationError(
            "A assistant message must carry content or tool_calls; got neither.",
            details={"field": "content", "role": "assistant"},
        )

    monkeypatch.setattr(execution, "corrective_turns", refuse)
    database, provider = _empty_then(GOOD, tmp_path=tmp_path)
    request = GenerateRequest(task="code.review", prompt="review this diff")
    try:
        reserved = reserve_sync_job(database, request, now=NOW, ttl_hours=24)
        with pytest.raises(ValidationError):
            execute(database, request, _context(provider), job_id=reserved.job_id)

        with database.read() as session:
            job = session.get_one(Job, reserved.job_id)
            assert job.state == "failed", "the job must not be left executing"
            assert job.error_code == "VALIDATION_ERROR"
            assert job.completed_at is not None
            rows = (
                session.query(JobAttempt)
                .filter_by(job_id=reserved.job_id)
                .order_by(JobAttempt.attempt)
                .all()
            )
            assert [row.attempt for row in rows] == [1], "the attempt made must survive"
            assert rows[0].outcome == "validation_failed"
            assert rows[0].finish_reason == FinishReason.STOP.value
    finally:
        database.close()


def test_a_refusal_before_the_first_attempt_still_fails_the_job(tmp_path: Path) -> None:
    """A refusal with no attempts yet: the job fails rather than waiting for a watchdog.

    `timeout_seconds = 0` is a configured value ModelRack refuses on construction, so this reaches
    the same path without patching anything: no provider is called, no attempt row exists, and the
    job is still terminal.
    """
    from loadcoach.infrastructure.db.models import Job, JobAttempt

    fake = FakeProvider(FakeScript(models=(_model(),)))
    provider = RecordingProvider(fake)
    database, _ = _setup(tmp_path)
    base = _context(provider)
    context = ExecutionContext(
        provider=provider,  # type: ignore[arg-type]  # delegates to a real FakeProvider
        provider_facts=base.provider_facts,
        policy=base.policy,
        schemas_dir=DEFAULT_SCHEMAS_DIR,
        timeout_seconds=0,
        now=lambda: NOW,
    )
    request = GenerateRequest(task="general.chat", prompt="hi")
    try:
        reserved = reserve_sync_job(database, request, now=NOW, ttl_hours=24)
        with pytest.raises(ValidationError):
            execute(database, request, context, job_id=reserved.job_id)
        with database.read() as session:
            job = session.get_one(Job, reserved.job_id)
            assert job.state == "failed"
            assert job.error_code == "VALIDATION_ERROR"
            assert session.query(JobAttempt).filter_by(job_id=reserved.job_id).count() == 0
    finally:
        database.close()
    assert provider.requests == [], "the provider is never reached"
