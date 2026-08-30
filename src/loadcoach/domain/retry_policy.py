"""loadcoach.domain.retry_policy — queue §7's failure table as a pure decision.

| Failure                    | Behaviour                                                        |
|----------------------------|------------------------------------------------------------------|
| Provider timeout           | Retry the same model up to the per-candidate limit, with backoff |
| Provider connection error  | Immediate fallback (retrying the same dead endpoint is pointless)  |
| Provider protocol error    | Retry once (transient truncation), then fall back                 |
| Structured-output failure  | Corrective retry up to the profile's limit, then fall back        |
| Context limit exceeded     | No retry on the same model; fall back to a larger-context candidate |
| Cancellation               | Terminal; no retry                                               |
| All candidates exhausted   | ``ALL_CANDIDATES_FAILED`` with every attempt and reason recorded  |

Not in the Phase 5 file list verbatim. The worker applies this table and the table is a
decision with no I/O in it, which is the definition of domain code; leaving it inline in the
worker would put the one piece of this phase most worth reading in its own right inside a
four-hundred-line loop.

Backoff is exponential with jitter: ``base x 2^(n-1) x (0.5 + r)`` for the ``n``-th retry on a
candidate and a uniform ``r`` in ``[0, 1)`` the caller supplies — injected, so the simulator's
backoffs are reproducible.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Final

from modelrack import (
    ContextLimitExceeded,
    GenerationCancelled,
    ProviderProtocolError,
    ProviderTimeout,
    ProviderUnavailable,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from modelrack import ProviderError

__all__ = [
    "PROTOCOL_ERROR_RETRIES",
    "Action",
    "Decision",
    "FailureKind",
    "backoff_seconds",
    "classify_failure",
    "next_action",
    "next_candidate_index",
]

PROTOCOL_ERROR_RETRIES: Final = 1
"""Queue §7: a protocol error is retried once on the same model before falling back."""


class FailureKind(StrEnum):
    """Queue §7's rows, as the worker sees them."""

    TIMEOUT = "timeout"
    CONNECTION = "connection"
    PROTOCOL = "protocol"
    VALIDATION = "validation"
    CONTEXT_LIMIT = "context_limit"
    CANCELLED = "cancelled"
    REJECTED = "rejected"


class Action(StrEnum):
    """What the worker does next."""

    RETRY_SAME = "retry_same"
    FALLBACK = "fallback"
    STOP = "stop"


@dataclass(frozen=True, slots=True)
class Decision:
    """The table's answer for one failure.

    Attributes:
        action: What to do.
        reason: The row that decided it, for the event.
        larger_context_only: For a fallback, whether only a candidate serving a larger context
            than the one that failed may be tried (the context-limit row).
    """

    action: Action
    reason: str
    larger_context_only: bool = False


def classify_failure(error: ProviderError) -> FailureKind:
    """Map a ModelRack error onto a row of the table.

    Anything not named by a row — a model the provider no longer has, a capability it refuses, a
    request it rejected — is ``REJECTED``: not transient for this model, so it falls back at once.
    """
    if isinstance(error, GenerationCancelled):
        return FailureKind.CANCELLED
    if isinstance(error, ProviderTimeout):
        return FailureKind.TIMEOUT
    if isinstance(error, ProviderUnavailable):
        return FailureKind.CONNECTION
    if isinstance(error, ProviderProtocolError):
        return FailureKind.PROTOCOL
    if isinstance(error, ContextLimitExceeded):
        return FailureKind.CONTEXT_LIMIT
    return FailureKind.REJECTED


def next_action(
    kind: FailureKind,
    *,
    attempts_on_candidate: int,
    per_candidate_limit: int,
) -> Decision:
    """Decide what follows a failed attempt on the current candidate.

    Args:
        kind: The failure's row.
        attempts_on_candidate: How many attempts this candidate has now had, this failure
            included.
        per_candidate_limit: The profile's ``execution.max_attempts`` — the retry budget on one
            model for timeouts and corrective retries alike.

    Returns:
        The :class:`Decision`. Whether a fallback candidate *exists*, and whether the job's total
        attempt bound has room, are the caller's checks — the table only says what the failure
        deserves.
    """
    if kind is FailureKind.CANCELLED:
        return Decision(Action.STOP, "cancelled")
    if kind is FailureKind.TIMEOUT:
        if attempts_on_candidate < per_candidate_limit:
            return Decision(Action.RETRY_SAME, "timeout")
        return Decision(Action.FALLBACK, "timeout_retries_exhausted")
    if kind is FailureKind.CONNECTION:
        return Decision(Action.FALLBACK, "connection_error")
    if kind is FailureKind.PROTOCOL:
        if attempts_on_candidate <= PROTOCOL_ERROR_RETRIES:
            return Decision(Action.RETRY_SAME, "protocol_error")
        return Decision(Action.FALLBACK, "protocol_error_retries_exhausted")
    if kind is FailureKind.VALIDATION:
        if attempts_on_candidate < per_candidate_limit:
            return Decision(Action.RETRY_SAME, "corrective_retry")
        return Decision(Action.FALLBACK, "validation_retries_exhausted")
    if kind is FailureKind.CONTEXT_LIMIT:
        return Decision(Action.FALLBACK, "context_limit_exceeded", larger_context_only=True)
    return Decision(Action.FALLBACK, "provider_rejected")


def next_candidate_index(
    served_contexts: Sequence[int],
    *,
    current: int,
    larger_context_only: bool,
) -> int | None:
    """Pick the next candidate to fall back to, or ``None`` when there is none.

    Args:
        served_contexts: Each ranked candidate's served context, in rank order.
        current: The index of the candidate that just failed.
        larger_context_only: Skip candidates that do not serve more context than the failed one
            (the context-limit row: a same-sized context would fail the same way).

    Returns:
        The index, or ``None``.
    """
    floor = served_contexts[current] if larger_context_only else None
    for index in range(current + 1, len(served_contexts)):
        if floor is None or served_contexts[index] > floor:
            return index
    return None


def backoff_seconds(base_seconds: float, retry_number: int, *, jitter: float) -> float:
    """Exponential backoff with jitter for the ``retry_number``-th retry (1-based).

    Args:
        base_seconds: ``execution.attempt_backoff_seconds``.
        retry_number: 1 for the first retry on a candidate, 2 for the second, …
        jitter: A uniform draw in ``[0, 1)``, supplied by the caller.

    Returns:
        ``base x 2^(retry_number - 1) x (0.5 + jitter)`` — between half and one-and-a-half
        times the exponential step, never negative.
    """
    if base_seconds <= 0:
        return 0.0
    step = base_seconds * (2.0 ** max(retry_number - 1, 0))
    return float(step * (0.5 + max(min(jitter, 1.0), 0.0)))
