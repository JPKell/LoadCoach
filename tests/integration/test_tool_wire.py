"""The tool wire: `tools` on the request, `tool_calls` on a message (api.md §4, ADR-0075).

Half of this wire already existed: a response has carried `output.tool_calls` since M4. What did
not exist was the inbound half, so a model was never told which tools it had — at G1, on the real
stack, gpt-oss:20b invented `repo_browser.list_dir` out of its own vocabulary and every call it
made was refused (`docs/history/G1_HANDOFF.md` §9.3). These tests pin the inbound half: the offer
reaches the provider unmodified, a candidate that cannot use tools is a routing rejection with a
reason rather than a silent drop, and a transcript carrying tool turns replays natively.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest
from baseaicore import ValidationError
from modelrack import Message, ProviderCapabilities, Role, ToolCall, ToolDefinition
from modelrack.testing import FakeProvider, FakeScript
from tests.integration.test_generate import RecordingProvider, _context, _model, _setup

from loadcoach.domain.priority import JobClass
from loadcoach.services.execution import GenerateRequest, execute
from loadcoach.services.queue import JobSubmission
from loadcoach.services.routing import NoEligibleModel
from loadcoach.web.routes.generate import GenerateBody, messages_of, tools_of

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


# --- a transcript that replays natively (gate C) ---------------------------------------------


ASSISTANT_CALL = ToolCall(id="ollama-17052f91-0", name="list_dir", arguments={"path": "./notes"})


def test_an_assistant_turn_with_calls_and_no_content_replays(tmp_path: Path) -> None:
    """The exact turn that broke G1 (`docs/history/G1_HANDOFF.md` §10.4, turn 3 s1).

    Before this, `MessageBody` had no `tool_calls`, so a turn that answered with calls and no text
    could not be put back on the wire at all: ModelRack refuses an assistant message with neither,
    and PromptCadence had to replay it as `[tool_calls]`-prefixed text instead.
    """
    database, provider = _setup(tmp_path)
    transcript = (
        Message(role=Role.USER, content="List ./notes."),
        Message(role=Role.ASSISTANT, content="", tool_calls=(ASSISTANT_CALL,)),
        Message(role=Role.TOOL, content="a.md\nb.md", tool_call_id=ASSISTANT_CALL.id),
    )
    try:
        execute(
            database,
            GenerateRequest(task="tools.agent", messages=transcript, tools=(LIST_DIR,)),
            _context(provider),
        )
    finally:
        database.close()
    assert provider.requests[0].messages == transcript


def test_the_wire_refuses_each_inconsistent_transcript_naming_its_field() -> None:
    """api.md §4's four rules, as `VALIDATION_ERROR`s with a field — never a 500."""
    cases = {
        "messages[0].tool_calls": [
            {
                "role": "user",
                "content": "go",
                "tool_calls": [{"id": "c1", "name": "list_dir", "arguments": {}}],
            }
        ],
        "messages[0].tool_call_id": [{"role": "tool", "content": "result"}],
        "messages[0].content": [{"role": "assistant", "content": ""}],
    }
    for path, messages in cases.items():
        body = GenerateBody.model_validate({"task": "tools.agent", "messages": messages})
        with pytest.raises(ValidationError) as caught:
            messages_of(body)
        assert caught.value.details["fields"][0]["path"] == path, path


def test_a_tool_turn_answering_no_earlier_call_is_refused() -> None:
    """LoadCoach's own rule: an unmatched id is a caller bug, refused at the edge."""
    body = GenerateBody.model_validate(
        {
            "task": "tools.agent",
            "messages": [
                {"role": "user", "content": "List ./notes."},
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [{"id": "call-0", "name": "list_dir", "arguments": {}}],
                },
                {"role": "tool", "content": "a.md", "tool_call_id": "call-9"},
            ],
        }
    )
    with pytest.raises(ValidationError) as caught:
        messages_of(body)
    assert caught.value.details["fields"][0]["path"] == "messages[2].tool_call_id"


def test_a_tool_turn_answering_an_earlier_call_is_accepted() -> None:
    body = GenerateBody.model_validate(
        {
            "task": "tools.agent",
            "messages": [
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [{"id": "call-0", "name": "list_dir", "arguments": {}}],
                },
                {"role": "tool", "content": "a.md", "tool_call_id": "call-0"},
            ],
        }
    )
    turns = messages_of(body)
    assert turns is not None
    assert turns[0].tool_calls[0].id == "call-0"
    assert turns[1].tool_call_id == "call-0"


def test_unparsed_arguments_survive_as_raw_arguments() -> None:
    """A model that sent arguments as a bare string — G1 saw exactly this — stays diagnosable."""
    body = GenerateBody.model_validate(
        {
            "task": "tools.agent",
            "messages": [
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {"id": "c1", "name": "run_command", "arguments": "ls -R ./notes"}
                    ],
                }
            ],
        }
    )
    turns = messages_of(body)
    assert turns is not None
    call = turns[0].tool_calls[0]
    assert call.arguments == {}
    assert call.raw_arguments == "ls -R ./notes"


def test_the_round_trip_from_a_response_back_onto_the_wire(tmp_path: Path) -> None:
    """A response's `output.tool_calls`, assembled, is a valid next-request assistant turn.

    `output.tool_calls` renders the provider's stream as it arrived — one entry per delta — so a
    caller groups the fragments by `id` and concatenates `arguments_fragment` before replaying
    them (api.md §4). This test does exactly that documented grouping and nothing else: no
    reshaping of names, no invention of ids, no rewriting of arguments.
    """
    fragments = [
        {"call_index": 0, "id": "c1", "name": "list_dir", "arguments_fragment": '{"path":'},
        {"call_index": 0, "id": "c1", "name": None, "arguments_fragment": ' "./notes"}'},
    ]
    grouped: dict[str, dict[str, Any]] = {}
    for fragment in fragments:
        entry = grouped.setdefault(
            str(fragment["id"]), {"id": fragment["id"], "name": "", "arguments": ""}
        )
        entry["name"] = entry["name"] or (fragment["name"] or "")
        entry["arguments"] += str(fragment["arguments_fragment"])
    replayed = [
        {"id": e["id"], "name": e["name"], "arguments": json.loads(e["arguments"])}
        for e in grouped.values()
    ]

    body = GenerateBody.model_validate(
        {
            "task": "tools.agent",
            "messages": [
                {"role": "user", "content": "List ./notes."},
                {"role": "assistant", "content": "", "tool_calls": replayed},
                {"role": "tool", "content": "a.md", "tool_call_id": "c1"},
            ],
            "tools": [{"name": "list_dir", "description": "", "parameters": {}}],
        }
    )
    turns = messages_of(body)
    assert turns is not None
    assert turns[1].tool_calls == (
        ToolCall(id="c1", name="list_dir", arguments={"path": "./notes"}),
    )

    database, provider = _setup(tmp_path)
    try:
        execute(
            database,
            GenerateRequest(task="tools.agent", messages=turns, tools=tools_of(body)),
            _context(provider),
        )
    finally:
        database.close()
    assert provider.requests[0].messages == turns


def test_a_queued_job_replays_the_calls_it_was_submitted_with() -> None:
    """`jobs.request_json` round trip: an offer and a tool turn survive the wait (data model §2)."""
    submission = JobSubmission(
        task="tools.agent",
        messages=(
            Message(role=Role.ASSISTANT, content="", tool_calls=(ASSISTANT_CALL,)),
            Message(role=Role.TOOL, content="a.md", tool_call_id=ASSISTANT_CALL.id),
        ),
        tools=(LIST_DIR,),
    )
    rebuilt = JobSubmission.from_request_json(
        submission.as_request_json(),
        job_class=JobClass.NORMAL,
        priority=500,
        max_wait_seconds=None,
        idempotent=True,
        idempotency_key=None,
        source="tests",
    )
    assert rebuilt.transcript() == submission.transcript()
    assert rebuilt.tools == submission.tools


def test_a_job_row_written_before_tool_calls_existed_still_rebuilds() -> None:
    """An old `request_json` has no `tools` key and no `tool_calls`; it must read back as before."""
    rebuilt = JobSubmission.from_request_json(
        {
            "task": "general.chat",
            "messages": [{"role": "user", "content": "hi", "tool_call_id": None}],
            "response_format": None,
            "sampling": {},
            "overrides": None,
            "stream": False,
        },
        job_class=JobClass.NORMAL,
        priority=500,
        max_wait_seconds=None,
        idempotent=True,
        idempotency_key=None,
        source="tests",
    )
    assert rebuilt.tools == ()
    assert rebuilt.transcript() == (Message(role=Role.USER, content="hi"),)
