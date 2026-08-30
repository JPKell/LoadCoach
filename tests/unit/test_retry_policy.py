"""Queue §7's failure table, row by row, and the backoff arithmetic."""

from __future__ import annotations

import pytest
from modelrack import (
    CapabilityUnsupported,
    ContextLimitExceeded,
    GenerationCancelled,
    ModelNotFound,
    ProviderProtocolError,
    ProviderRejected,
    ProviderTimeout,
    ProviderUnavailable,
)

from loadcoach.domain.retry_policy import (
    Action,
    FailureKind,
    backoff_seconds,
    classify_failure,
    next_action,
    next_candidate_index,
)


def test_every_modelrack_error_maps_to_exactly_one_row() -> None:
    assert classify_failure(ProviderTimeout("t")) is FailureKind.TIMEOUT
    assert classify_failure(ProviderUnavailable("u")) is FailureKind.CONNECTION
    assert classify_failure(ProviderProtocolError("p")) is FailureKind.PROTOCOL
    assert classify_failure(ContextLimitExceeded("c")) is FailureKind.CONTEXT_LIMIT
    assert classify_failure(GenerationCancelled("x")) is FailureKind.CANCELLED
    for permanent in (ModelNotFound("m"), CapabilityUnsupported("c"), ProviderRejected("r")):
        assert classify_failure(permanent) is FailureKind.REJECTED


def test_timeout_retries_the_same_model_up_to_the_limit_then_falls_back() -> None:
    assert (
        next_action(FailureKind.TIMEOUT, attempts_on_candidate=1, per_candidate_limit=3).action
        is Action.RETRY_SAME
    )
    assert (
        next_action(FailureKind.TIMEOUT, attempts_on_candidate=2, per_candidate_limit=3).action
        is Action.RETRY_SAME
    )
    third = next_action(FailureKind.TIMEOUT, attempts_on_candidate=3, per_candidate_limit=3)
    assert third.action is Action.FALLBACK and third.reason == "timeout_retries_exhausted"


def test_connection_error_falls_back_at_once() -> None:
    decision = next_action(FailureKind.CONNECTION, attempts_on_candidate=1, per_candidate_limit=3)
    assert decision.action is Action.FALLBACK and decision.reason == "connection_error"


def test_protocol_error_retries_exactly_once() -> None:
    assert (
        next_action(FailureKind.PROTOCOL, attempts_on_candidate=1, per_candidate_limit=3).action
        is Action.RETRY_SAME
    )
    assert (
        next_action(FailureKind.PROTOCOL, attempts_on_candidate=2, per_candidate_limit=3).action
        is Action.FALLBACK
    )


def test_validation_failure_retries_correctively_up_to_the_profile_limit() -> None:
    assert (
        next_action(FailureKind.VALIDATION, attempts_on_candidate=1, per_candidate_limit=2).reason
        == "corrective_retry"
    )
    assert (
        next_action(FailureKind.VALIDATION, attempts_on_candidate=2, per_candidate_limit=2).action
        is Action.FALLBACK
    )


def test_context_limit_never_retries_the_same_model_and_wants_a_larger_context() -> None:
    decision = next_action(
        FailureKind.CONTEXT_LIMIT, attempts_on_candidate=1, per_candidate_limit=3
    )
    assert decision.action is Action.FALLBACK and decision.larger_context_only is True
    # Candidates serving 8k, 8k, 32k: from the first, only the third qualifies.
    assert next_candidate_index([8192, 8192, 32768], current=0, larger_context_only=True) == 2
    assert next_candidate_index([8192, 8192, 4096], current=0, larger_context_only=True) is None
    assert next_candidate_index([8192, 8192, 4096], current=0, larger_context_only=False) == 1
    assert next_candidate_index([8192], current=0, larger_context_only=False) is None


def test_cancellation_is_terminal() -> None:
    assert (
        next_action(FailureKind.CANCELLED, attempts_on_candidate=1, per_candidate_limit=3).action
        is Action.STOP
    )


def test_backoff_is_exponential_with_bounded_jitter() -> None:
    assert backoff_seconds(2.0, 1, jitter=0.0) == 1.0
    assert backoff_seconds(2.0, 1, jitter=0.5) == 2.0
    assert backoff_seconds(2.0, 2, jitter=0.999) == pytest.approx(4.0 * 1.499)
    assert backoff_seconds(2.0, 3, jitter=0.5) == 8.0
    assert backoff_seconds(0.0, 5, jitter=0.5) == 0.0
    assert backoff_seconds(2.0, 1, jitter=7.0) == 3.0  # jitter clamped into [0, 1]
