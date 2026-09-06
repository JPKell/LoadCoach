"""A caller's declared classification joins the adapter's (ADR-0065 rule 2).

The caller half of the join. Until LoadCoach 1.1's H3 amendment nothing ever set
``ConstraintInputs.caller_data_classification``, so every rejection detail honestly showed
``caller_classification: null`` and every attempt's ``effective_data_classification`` was the
adapter's own value. IdeaPress is the first caller with a classification to declare.

What is asserted here: the join is a ``max()`` over the ordered vocabulary, it reaches both places
classification is recorded, an absent declaration behaves exactly as it did before the field
existed, and the field never widens anything.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

import pytest
from sqlalchemy import select
from tests.integration.test_adapter_execution import (  # noqa: F401 — `wired` is a fixture
    AdapterCapableProvider,
    _context,
    wired,
)
from tests.integration.test_adapter_subjects import (
    NOW,
    _candidates,
    _facts,
    _remote_database,
    _settings,
    _write_adapter,
)

from loadcoach.domain.routing.constraints import join_classification
from loadcoach.domain.routing.subject import RuntimeOverrides
from loadcoach.infrastructure.db.models import JobAttempt
from loadcoach.services.adapters import sync_adapters
from loadcoach.services.execution import GenerateRequest, execute
from loadcoach.services.routing import RouteRequest, RoutingPolicy, route

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.parametrize(
    ("caller", "adapter", "expected"),
    [
        (None, "confidential", "confidential"),
        (None, "public", "public"),
        ("public", "internal", "internal"),
        ("internal", "public", "internal"),
        ("confidential", "public", "confidential"),
        ("internal", "internal", "internal"),
        ("nonsense", "internal", "internal"),
    ],
)
def test_the_join_is_max_over_the_ordered_vocabulary(
    caller: str | None, adapter: str, expected: str
) -> None:
    """A declaration can raise the effective classification and can never lower it.

    An unreadable declaration contributes nothing rather than being guessed at, which is the
    fail-closed direction: the adapter's own classification still governs.
    """
    assert join_classification(caller, adapter) == expected


def test_a_caller_classification_is_recorded_on_the_attempt(wired: Any) -> None:  # noqa: F811
    """The join, not the adapter's own value, is what ``effective_data_classification`` holds."""
    database, provider = wired

    execute(
        database,
        GenerateRequest(
            task="general.chat",
            prompt="say something",
            overrides=RuntimeOverrides(adapter="terse"),
            data_classification="confidential",
        ),
        _context(provider),
    )

    with database.read() as session:
        (attempt,) = session.execute(select(JobAttempt)).scalars().all()
    assert attempt.adapter_data_classification == "confidential"
    assert attempt.effective_data_classification == "confidential"


def test_a_lower_caller_classification_leaves_the_adapters_value_standing(wired: Any) -> None:  # noqa: F811
    """`public` joined with `confidential` is `confidential`.

    A caller cannot declare its way down.
    """
    database, provider = wired

    execute(
        database,
        GenerateRequest(
            task="general.chat",
            prompt="say something",
            overrides=RuntimeOverrides(adapter="terse"),
            data_classification="public",
        ),
        _context(provider),
    )

    with database.read() as session:
        (attempt,) = session.execute(select(JobAttempt)).scalars().all()
    assert attempt.effective_data_classification == "confidential"


def test_a_request_without_the_field_behaves_exactly_as_1_1_prepared_it(wired: Any) -> None:  # noqa: F811
    """The compatibility claim: every 1.0 caller sends no field and nothing about it moved."""
    database, provider = wired

    execute(
        database,
        GenerateRequest(
            task="general.chat",
            prompt="say something",
            overrides=RuntimeOverrides(adapter="terse"),
        ),
        _context(provider),
    )

    with database.read() as session:
        (attempt,) = session.execute(select(JobAttempt)).scalars().all()
    assert attempt.adapter_data_classification == "confidential"
    assert attempt.effective_data_classification == "confidential"


def test_a_bare_attempt_still_records_no_classification_at_all(wired: Any) -> None:  # noqa: F811
    """No adapter, no adapter egress to have an opinion about.

    The caller's own value alone is not one.
    """
    database, provider = wired

    execute(
        database,
        GenerateRequest(
            task="general.chat", prompt="say something", data_classification="confidential"
        ),
        _context(provider),
    )

    with database.read() as session:
        (attempt,) = session.execute(select(JobAttempt)).scalars().all()
    assert attempt.adapter_data_classification is None
    assert attempt.effective_data_classification is None


def test_the_rejection_detail_carries_all_three_classification_fields(tmp_path: Path) -> None:
    """I19's denial with both halves populated — `caller_classification` is no longer `null`."""
    directory = tmp_path / "adapters"
    directory.mkdir()
    _write_adapter(directory, "house_voice")
    handle = _remote_database(tmp_path)
    try:
        sync_adapters(handle, _settings(directory), now=NOW)
        route(
            handle,
            RouteRequest(
                task="tools.agent.remote_cheap",
                estimated_input_tokens=100,
                data_classification="internal",
            ),
            provider=_facts(adapter_hot_swap=True, adapters_registered=True, is_remote=True),
            policy=RoutingPolicy(require_adapter_evidence=False),
            now=NOW,
        )

        (row,) = [row for row in _candidates(handle) if row.adapter_id is not None]
        assert row.rejection_reason == "adapter_classification_conflict"
        detail = cast("dict[str, Any]", row.rejection_detail_json or {})
        assert detail["caller_classification"] == "internal"
        assert detail["adapter_classification"] == "confidential"
        assert detail["effective_classification"] == "confidential"
    finally:
        handle.close()


def test_the_wire_refuses_a_classification_outside_the_vocabulary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A misspelled declaration is refused, never treated as no declaration at all.

    Ignoring it is the one direction that can only *lower* the effective classification, which is
    the whole failure mode ADR-0046 fixed by defaulting closed.
    """
    from tests.integration.test_jobs_api import _client

    with _client(tmp_path, monkeypatch) as client:
        response = client.post(
            "/api/v1/generate",
            json={"task": "general.chat", "prompt": "hi", "data_classification": "secret"},
        )
        assert response.status_code == 400, response.text
        assert "public, internal, confidential" in response.text


def test_a_queued_job_carries_its_pin_and_its_classification_across_the_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A queued submission is rebuilt from ``jobs.request_json`` and from nothing else.

    Anything the round trip drops is dropped silently between submission and execution. For
    ``overrides.adapter`` that is the bare-base fallback ADR-0064 rule 4 forbids, on the path
    IdeaPress submits its long stages through.
    """
    from loadcoach.services.queue import JobSubmission

    submitted = JobSubmission(
        task="general.chat",
        prompt="hello",
        overrides=RuntimeOverrides(adapter="terse", model="fake/m", ignore_residency=True),
        data_classification="internal",
    )

    rebuilt = JobSubmission.from_request_json(
        submitted.as_request_json(),
        job_class=submitted.job_class,
        priority=0,
        max_wait_seconds=None,
        idempotent=True,
        idempotency_key=None,
        source="ideapress",
    )

    assert rebuilt.overrides is not None
    assert rebuilt.overrides.adapter == "terse"
    assert rebuilt.overrides.model == "fake/m"
    assert rebuilt.overrides.ignore_residency is True
    assert rebuilt.data_classification == "internal"
    assert rebuilt.to_generate_request().data_classification == "internal"
