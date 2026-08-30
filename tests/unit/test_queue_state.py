"""The job state machine: every legal transition accepted, every other pair rejected.

Testing standards' state-machine row: every legal transition, every illegal transition rejected,
terminal-state immutability, recovery from each non-terminal state. The table under test is the one
in queue §2 plus ADR-0036's seven edges, and this file enumerates all ``11 x 11`` pairs rather than
spot-checking a few, so an edge added to the code without being added here fails the count.
"""

from __future__ import annotations

import itertools

import pytest

from loadcoach.domain.queue_state import (
    ACTIVE_STATES,
    IN_FLIGHT_STATES,
    LEASE_HOLDING_STATES,
    TERMINAL_STATES,
    TRANSITIONS,
    WAITING_STATES,
    IllegalTransition,
    JobState,
    cancel_target,
    check_transition,
    event_type_for,
    is_legal,
    recovery_target,
    successors,
)

# Queue §2's diagram, transcribed independently of the implementation's table.
_DOCUMENTED_EDGES: set[tuple[str, str]] = {
    ("queued", "leased"),
    ("leased", "admitted"),
    ("leased", "waiting_resources"),
    ("leased", "cancelling"),
    ("waiting_resources", "queued"),
    ("admitted", "executing"),
    ("executing", "validating"),
    ("validating", "completed"),
    ("validating", "retrying"),
    ("executing", "retrying"),
    ("retrying", "admitted"),
    ("executing", "cancelling"),
    ("validating", "cancelling"),
    ("admitted", "cancelling"),
    ("queued", "cancelled"),
    ("waiting_resources", "cancelled"),
    ("cancelling", "cancelled"),
    ("executing", "failed"),
    ("validating", "failed"),
    ("queued", "failed"),
    ("waiting_resources", "failed"),
    ("leased", "queued"),
    ("leased", "failed"),
    ("executing", "queued"),
    # ADR-0036.
    ("admitted", "queued"),
    ("admitted", "failed"),
    ("validating", "queued"),
    ("retrying", "queued"),
    ("retrying", "failed"),
    ("retrying", "cancelling"),
}


def test_the_table_is_exactly_the_documented_edge_set() -> None:
    """Thirty edges, no more and no fewer — the documented table is normative."""
    assert {(a.value, b.value) for a, b in TRANSITIONS} == _DOCUMENTED_EDGES
    assert len(TRANSITIONS) == 30


@pytest.mark.parametrize(("current", "target"), sorted(_DOCUMENTED_EDGES))
def test_every_legal_transition_is_accepted(current: str, target: str) -> None:
    check_transition(JobState(current), JobState(target))  # does not raise
    assert is_legal(JobState(current), JobState(target))


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (a, b)
        for a, b in itertools.product(list(JobState), list(JobState))
        if (a.value, b.value) not in _DOCUMENTED_EDGES
    ],
)
def test_every_other_pair_is_rejected(current: JobState, target: JobState) -> None:
    """All ``11 x 11 - 30 = 91`` unlisted pairs, self-transitions included, raise."""
    assert not is_legal(current, target)
    with pytest.raises(IllegalTransition) as excinfo:
        check_transition(current, target)
    assert excinfo.value.code == "ILLEGAL_TRANSITION"
    assert excinfo.value.details == {"current": current.value, "target": target.value}


@pytest.mark.parametrize("state", sorted(TERMINAL_STATES, key=str))
def test_terminal_states_have_no_successor(state: JobState) -> None:
    assert successors(state) == frozenset()
    assert cancel_target(state) is None
    assert recovery_target(state, idempotent=True) is None
    assert recovery_target(state, idempotent=False) is None


def test_state_sets_partition_the_enum() -> None:
    assert WAITING_STATES | IN_FLIGHT_STATES | TERMINAL_STATES == frozenset(JobState)
    assert not (WAITING_STATES & IN_FLIGHT_STATES)
    assert not (ACTIVE_STATES & TERMINAL_STATES)
    assert LEASE_HOLDING_STATES | {JobState.CANCELLING} == IN_FLIGHT_STATES


@pytest.mark.parametrize("state", sorted(LEASE_HOLDING_STATES, key=str))
def test_recovery_from_every_lease_holding_state_follows_idempotency(state: JobState) -> None:
    """ADR-0036 §1: one rule for leased, admitted, executing, validating and retrying."""
    assert recovery_target(state, idempotent=True) is JobState.QUEUED
    assert recovery_target(state, idempotent=False) is JobState.FAILED
    # And both targets are legal edges, so recovery never needs a transition the table lacks.
    assert is_legal(state, JobState.QUEUED)
    assert is_legal(state, JobState.FAILED)


def test_recovery_completes_a_cancel_and_leaves_waiting_states_alone() -> None:
    assert recovery_target(JobState.CANCELLING, idempotent=True) is JobState.CANCELLED
    assert recovery_target(JobState.QUEUED, idempotent=True) is None
    assert recovery_target(JobState.WAITING_RESOURCES, idempotent=False) is None


def test_cancel_target_by_state() -> None:
    """Queue §8: waiting jobs stop at once; in-flight jobs go through cancelling."""
    assert cancel_target(JobState.QUEUED) is JobState.CANCELLED
    assert cancel_target(JobState.WAITING_RESOURCES) is JobState.CANCELLED
    for state in LEASE_HOLDING_STATES:
        assert cancel_target(state) is JobState.CANCELLING
        assert is_legal(state, JobState.CANCELLING)
    assert cancel_target(JobState.CANCELLING) is None  # idempotent: already on its way


def test_event_type_names_follow_the_api_list() -> None:
    assert event_type_for(JobState.WAITING_RESOURCES) == "job.waiting_resources"
    assert {event_type_for(state) for state in JobState} == {
        f"job.{state.value}" for state in JobState
    }
