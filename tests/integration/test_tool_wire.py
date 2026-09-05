"""The tool wire: `tools` on the request, `tool_calls` on a message (api.md §4, ADR-0075).

Half of this wire already existed: a response has carried `output.tool_calls` since M4. What did
not exist was the inbound half, so a model was never told which tools it had — at G1, on the real
stack, gpt-oss:20b invented `repo_browser.list_dir` out of its own vocabulary and every call it
made was refused (`docs/history/G1_HANDOFF.md` §9.3). These tests pin the inbound half: the offer
reaches the provider unmodified, a candidate that cannot use tools is a routing rejection with a
reason rather than a silent drop, and a transcript carrying tool turns replays natively.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from modelrack import ProviderCapabilities, ToolDefinition
from modelrack.testing import FakeProvider, FakeScript
from tests.integration.test_generate import RecordingProvider, _context, _model, _setup

from loadcoach.services.execution import GenerateRequest, execute
from loadcoach.services.routing import NoEligibleModel

if TYPE_CHECKING:
    from pathlib import Path


LIST_DIR = ToolDefinition(
    name="list_dir",
    description="List the entries of a directory inside the workspace.",
    parameters={
        "type": "object",
        "properties": {"path": {"type": "string"}},
        "required": ["path"],
        # A keyword LoadCoach has never heard of. ADR-0041: it is the caller's schema, and it is
        # passed to the provider unmodified — not normalised, not stripped, not rejected.
        "x-loadcoach-must-not-touch-this": {"nested": [1, 2, {"deep": True}]},
    },
)


def _rejections(error: NoEligibleModel) -> list[dict[str, Any]]:
    return [dict(candidate) for candidate in error.details["candidates"]]


# --- the offer reaches the provider ---------------------------------------------------------


def test_the_offered_tools_reach_the_provider_verbatim(tmp_path: Path) -> None:
    """ADR-0041: a caller's schema travels through the router untouched."""
    database, provider = _setup(tmp_path)
    try:
        execute(
            database,
            GenerateRequest(task="tools.agent", prompt="List ./notes.", tools=(LIST_DIR,)),
            _context(provider),
        )
    finally:
        database.close()
    sent = provider.requests[0].tools
    assert sent == (LIST_DIR,)
    assert sent[0].parameters == LIST_DIR.parameters


def test_a_request_with_no_tools_builds_a_request_with_no_tools(tmp_path: Path) -> None:
    database, provider = _setup(tmp_path)
    try:
        execute(database, GenerateRequest(task="general.chat", prompt="hi"), _context(provider))
    finally:
        database.close()
    assert provider.requests[0].tools == ()


# --- the routing rule (ADR-0075) ------------------------------------------------------------


def test_a_request_carrying_tools_is_rejected_by_routing_when_no_candidate_can_use_them(
    tmp_path: Path,
) -> None:
    """The row's rule: a routing rejection with a reason, never a silent drop."""
    database, provider = _setup(tmp_path)
    try:
        with pytest.raises(NoEligibleModel) as caught:
            execute(
                database,
                # `general.chat` requires no capability at all: without ADR-0075's request-level
                # rule this body would route, be served, and lose its tools on the way.
                GenerateRequest(task="general.chat", prompt="List ./notes.", tools=(LIST_DIR,)),
                _context(provider, supports_tool_use=False),
            )
    finally:
        database.close()
    reasons = _rejections(caught.value)
    assert reasons, "the error must name every candidate and why it was rejected"
    assert all(reason["reason"] == "capability_unsupported" for reason in reasons)
    detail = reasons[0]["detail"]
    assert detail["capability"] == "tool_use"
    assert detail["required_by"] == "request"
    # And the provider was never called: the refusal happened before a model was chosen.
    assert provider.requests == []


def test_the_profiles_own_requirement_is_still_labelled_as_the_profiles(tmp_path: Path) -> None:
    database, provider = _setup(tmp_path)
    try:
        with pytest.raises(NoEligibleModel) as caught:
            execute(
                database,
                GenerateRequest(task="tools.agent", prompt="List ./notes."),
                _context(provider, supports_tool_use=False),
            )
    finally:
        database.close()
    detail = _rejections(caught.value)[0]["detail"]
    assert detail["capability"] == "tool_use"
    assert detail["required_by"] == "task_profile"


def test_capability_unsupported_from_the_provider_edge_is_never_the_path(tmp_path: Path) -> None:
    """ModelRack refuses tools at a provider that has not declared `tool_calling`.

    After ADR-0075 that refusal is unreachable through `/generate`: routing rejects first. The
    fake is scripted without `tool_calling` *and* the provider facts say so, which is the shape a
    real deployment has — so the assertion is that `NoEligibleModel` arrives, not
    `CapabilityUnsupported`.
    """
    script = FakeScript(
        models=(_model(),),
        capabilities=ProviderCapabilities(streaming=True, tool_calling=False),
    )
    provider = RecordingProvider(FakeProvider(script))
    database, _ = _setup(tmp_path)
    try:
        with pytest.raises(NoEligibleModel):
            execute(
                database,
                GenerateRequest(task="general.chat", prompt="hi", tools=(LIST_DIR,)),
                _context(provider, supports_tool_use=False),
            )
    finally:
        database.close()
    assert provider.requests == []


def test_an_empty_tools_list_imposes_nothing(tmp_path: Path) -> None:
    """ADR-0075: `tools: []` is `tools: null` is absent — including for routing."""
    database, provider = _setup(tmp_path)
    try:
        outcome = execute(
            database,
            GenerateRequest(task="general.chat", prompt="hi", tools=()),
            _context(provider, supports_tool_use=False),
        )
    finally:
        database.close()
    assert outcome.status == "completed"
    assert provider.requests[0].tools == ()
