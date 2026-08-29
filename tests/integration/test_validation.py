"""Validation: each kind passes and fails correctly, and a corrective retry is its own attempt."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from modelrack.testing import FakeGeneration, FakeScript
from tests.integration.test_generate import NOW, RecordingProvider, _model, _setup

from loadcoach.domain.validation import (
    SchemaUnsupported,
    validate_json,
    validate_length,
    validate_output,
    validate_regex,
    validate_required_fields,
    validate_schema,
)
from loadcoach.services.execution import ExecutionContext, GenerateRequest, execute
from loadcoach.services.task_profiles import DEFAULT_SCHEMAS_DIR

SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["findings", "summary"],
    "additionalProperties": False,
    "properties": {
        "summary": {"type": "string"},
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["path", "severity"],
                "additionalProperties": False,
                "properties": {
                    "path": {"type": "string"},
                    "line": {"type": ["integer", "null"]},
                    "severity": {"type": "string", "enum": ["info", "minor", "major"]},
                },
            },
        },
    },
}

VALID = json.dumps(
    {"summary": "ok", "findings": [{"path": "a.py", "line": 3, "severity": "minor"}]}
)


# --- each kind, passing and failing ---------------------------------------------------------


def test_json_passes_on_json_and_fails_with_the_parse_error() -> None:
    check, parsed = validate_json('{"a": 1}')
    assert check.passed is True
    assert parsed == {"a": 1}

    check, parsed = validate_json("not json at all")
    assert check.passed is False
    assert parsed is None
    assert "output_prefix" in check.detail


def test_schema_passes_on_a_conforming_document() -> None:
    assert validate_schema(json.loads(VALID), SCHEMA).passed is True


@pytest.mark.parametrize(
    ("document", "expected_path"),
    [
        ({"findings": []}, "$.summary"),
        ({"summary": "ok", "findings": [{}]}, "$.findings[0].path"),
        (
            {"summary": "ok", "findings": [{"path": "a", "severity": "fatal"}]},
            "$.findings[0].severity",
        ),
        ({"summary": 1, "findings": []}, "$.summary"),
        ({"summary": "ok", "findings": {}}, "$.findings"),
        ({"summary": "ok", "findings": [], "extra": 1}, "$.extra"),
        (
            {"summary": "ok", "findings": [{"path": "a", "severity": "info", "line": "3"}]},
            "$.findings[0].line",
        ),
    ],
)
def test_schema_fails_and_names_the_field(document: dict[str, Any], expected_path: str) -> None:
    check = validate_schema(document, SCHEMA)
    assert check.passed is False
    paths = [item["path"] for item in check.detail["fields"]]
    assert expected_path in paths, paths


def test_every_failing_field_is_reported_not_only_the_first() -> None:
    """A retry that fixes one problem per round trip takes one round trip per problem."""
    check = validate_schema({}, SCHEMA)
    assert check.passed is False
    assert len(check.detail["fields"]) == 2


def test_a_schema_using_an_unimplemented_keyword_is_refused_not_ignored() -> None:
    """An ignored constraint produces a validation that passed for a reason nobody intended."""
    with pytest.raises(SchemaUnsupported, match="oneOf"):
        validate_schema({}, {"type": "object", "oneOf": [{"type": "string"}]})


def test_required_fields_passes_and_names_what_is_missing() -> None:
    assert validate_required_fields({"a": 1, "b": 2}, ["a", "b"]).passed is True
    check = validate_required_fields({"a": 1}, ["a", "b", "c"])
    assert check.passed is False
    assert check.detail["missing"] == ["b", "c"]
    assert validate_required_fields([1, 2], ["a"]).passed is False


def test_regex_passes_fails_and_survives_a_broken_pattern() -> None:
    assert validate_regex("hello world", r"^hello").passed is True
    assert validate_regex("goodbye", r"^hello").passed is False
    broken = validate_regex("anything", r"([unclosed")
    assert broken.passed is False
    assert "invalid pattern" in broken.detail["problem"]


def test_length_passes_and_fails_with_both_numbers() -> None:
    assert validate_length("abc", maximum_chars=3).passed is True
    check = validate_length("abcd", maximum_chars=3)
    assert check.passed is False
    assert check.detail == {"chars": 4, "max_output_chars": 3}


def test_a_policy_that_asks_for_nothing_is_not_a_policy_that_passed() -> None:
    outcome = validate_output("anything")
    assert outcome.performed is False
    assert outcome.passed is None


def test_schema_checks_are_skipped_when_the_output_is_not_json_at_all() -> None:
    """Reporting "missing required field" about non-JSON text names the wrong problem."""
    outcome = validate_output(
        "I'm afraid I can't do that",
        require_valid_json=True,
        schema=SCHEMA,
        required_fields=("summary",),
    )
    assert outcome.passed is False
    assert [check.kind for check in outcome.checks] == ["json"]


def test_a_full_policy_runs_every_kind_and_reports_each() -> None:
    outcome = validate_output(
        VALID,
        require_valid_json=True,
        schema=SCHEMA,
        required_fields=("summary", "findings"),
        max_output_chars=10_000,
    )
    assert outcome.performed is True
    assert outcome.passed is True
    assert [check.kind for check in outcome.checks] == [
        "json",
        "json_schema",
        "required_fields",
        "length",
    ]


# --- the corrective retry --------------------------------------------------------------------


def _structured_setup(tmp_path: Path, *outputs: str) -> tuple[Any, RecordingProvider]:
    script = FakeScript(
        models=(_model(),),
        generations=tuple(FakeGeneration(text=text) for text in outputs),
        repeat_final_generation=True,
    )
    return _setup(tmp_path, script)


def _structured_context(provider: RecordingProvider) -> ExecutionContext:
    from loadcoach.domain.routing.subject import ProviderFacts
    from loadcoach.services.routing import RoutingPolicy

    return ExecutionContext(
        provider=provider,  # type: ignore[arg-type]  # RecordingProvider delegates to a real FakeProvider
        provider_facts=ProviderFacts(
            healthy=True,
            context_configurable=True,
            supports_tool_use=True,
            supports_structured_output=True,
            supports_streaming=True,
        ),
        policy=RoutingPolicy(),
        schemas_dir=DEFAULT_SCHEMAS_DIR,
        now=lambda: NOW,
    )


def test_a_corrective_retry_is_a_separate_attempt_that_keeps_the_original(
    tmp_path: Path,
) -> None:
    """The named failure mode: a retry that loses the original attempt record."""
    from loadcoach.infrastructure.db.models import JobAttempt, Validation

    bad = json.dumps({"summary": "only a summary"})
    good = json.dumps(
        {
            "summary": "ok",
            "findings": [{"path": "a.py", "line": 3, "severity": "minor", "description": "x"}],
        }
    )
    database, provider = _structured_setup(tmp_path, bad, good)
    try:
        outcome = execute(
            database,
            GenerateRequest(task="code.review", prompt="review this diff"),
            _structured_context(provider),
        )

        assert len(outcome.attempts) == 2
        first, second = outcome.attempts
        assert first.outcome == "validation_failed"
        assert second.outcome == "completed"
        # The original attempt is still on the record, with its own failure and its own output.
        assert first.validation is not None
        assert first.validation.passed is False
        assert first.partial_response_hash is not None
        assert first.prompt_id is None, "the first attempt used no prompt of LoadCoach's own"
        # The retry names the prompt LoadCoach applied, so the job history shows what the model saw.
        assert second.prompt_id == "execution.structured_output.retry"
        assert second.prompt_version == "1.0.0"
        assert second.prompt_sha256 is not None
        assert second.prompt_sha256.startswith("sha256:")

        with database.read() as session:
            rows = (
                session.query(JobAttempt)
                .filter_by(job_id=outcome.job_id)
                .order_by(JobAttempt.attempt)
                .all()
            )
            assert [row.attempt for row in rows] == [1, 2]
            assert rows[0].outcome == "validation_failed"
            assert rows[0].partial_response_hash is not None
            assert rows[1].prompt_id == "execution.structured_output.retry"
            checks = session.query(Validation).all()
            assert any(not check.passed for check in checks)
            assert any(check.passed for check in checks)
    finally:
        database.close()


def test_the_corrective_retry_follows_the_callers_turns_and_never_rewrites_them(
    tmp_path: Path,
) -> None:
    bad = json.dumps({"summary": "only a summary"})
    good = json.dumps(
        {
            "summary": "ok",
            "findings": [{"path": "a.py", "line": 1, "severity": "info", "description": "x"}],
        }
    )
    caller_prompt = "review this diff"
    database, provider = _structured_setup(tmp_path, bad, good)
    try:
        execute(
            database,
            GenerateRequest(task="code.review", prompt=caller_prompt),
            _structured_context(provider),
        )
    finally:
        database.close()

    assert len(provider.requests) == 2
    first, second = (request.messages for request in provider.requests)
    assert [m.content for m in first] == [caller_prompt]
    # The retry starts with the caller's turns, unchanged, and appends rather than substitutes.
    assert second[0].content == caller_prompt
    assert len(second) > len(first)
    correction = second[-1].content
    assert "Your previous answer did not satisfy the required output format." in correction
    assert "$.findings: required but missing" in correction


def test_a_retry_that_still_fails_leaves_every_attempt_recorded(tmp_path: Path) -> None:
    from loadcoach.services.execution import AllCandidatesFailed

    bad = json.dumps({"summary": "still wrong"})
    database, provider = _structured_setup(tmp_path, bad)
    try:
        with pytest.raises(AllCandidatesFailed) as caught:
            execute(
                database,
                GenerateRequest(task="code.review", prompt="review this diff"),
                _structured_context(provider),
            )
        attempts = caught.value.details["attempts"]
        assert len(attempts) >= 2
        assert all(a["outcome"] == "validation_failed" for a in attempts)
    finally:
        database.close()
