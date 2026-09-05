"""The tool-free request LoadCoach builds is byte-for-byte the one `dfbf2d8` built.

`tools` and per-message `tool_calls` are additive fields inside `/api/v1` (api.md §4, ADR-0075).
"Additive" is a claim about every caller that does **not** use them, and the only form in which it
is a claim rather than a hope is a golden: the `GenerationRequest` handed to the provider, captured
by running LoadCoach as it stood at `dfbf2d8` — the commit before this row — over four bodies that
between them cover the prompt form, the prompt-plus-system form, a full transcript and a
JSON-schema profile.

Field assertions elsewhere check that the new values arrive. This checks that **nothing else
moved**: no key added, none dropped, none renamed, no value nudged.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from baseaicore import SuiteError
from modelrack import GenerationRequest, Message, Role
from modelrack.testing import FakeProvider, FakeScript
from tests.integration.test_generate import RecordingProvider, _context, _model

from loadcoach.services.database import Database, ensure_ready
from loadcoach.services.execution import GenerateRequest, execute
from loadcoach.services.models import discover_models
from loadcoach.services.task_profiles import import_task_profiles, read_task_profiles_file

if TYPE_CHECKING:
    from collections.abc import Mapping

pytestmark = pytest.mark.contract

GOLDEN = Path(__file__).resolve().parents[1] / "fixtures" / "contract"
GOLDEN_FILE = GOLDEN / "generation_request_dfbf2d8.json"
NOW = datetime(2026, 8, 29, 12, 0, 0, tzinfo=UTC)

CASES: Mapping[str, dict[str, Any]] = {
    "prompt_plain": {"task": "general.chat", "prompt": "Say hello."},
    "prompt_with_system": {
        "task": "content.article_draft",
        "system": "You are drafting one section of an article.",
        "prompt": "Write a 600-word section on local inference privacy.",
        "sampling": {"temperature": 0.4, "max_output_tokens": 256},
    },
    "transcript": {
        "task": "general.chat",
        "messages": (
            Message(role=Role.SYSTEM, content="Be terse."),
            Message(role=Role.USER, content="One."),
            Message(role=Role.ASSISTANT, content="Two."),
            Message(role=Role.USER, content="Three."),
        ),
    },
    "json_schema_profile": {
        "task": "structured.extract",
        "prompt": "Extract the fields.",
        "response_format": "json",
    },
}


def _as_json(request: GenerationRequest) -> dict[str, Any]:
    """The capture shape the golden was written in. Every field the request carries."""
    sampling = request.sampling
    profile = request.runtime_profile
    response_format = request.response_format
    return {
        "identity": str(request.identity),
        "messages": [
            {"role": m.role.value, "content": m.content, "tool_call_id": m.tool_call_id}
            for m in request.messages
        ],
        "prompt": request.prompt,
        "runtime_profile": {
            "context_size": profile.context_size,
            "kv_cache_precision": profile.kv_cache_precision,
            "flash_attention": profile.flash_attention,
            "keep_alive": profile.keep_alive,
        },
        "sampling": {
            "temperature": sampling.temperature,
            "top_p": sampling.top_p,
            "top_k": sampling.top_k,
            "seed": sampling.seed,
            "max_output_tokens": sampling.max_output_tokens,
            "stop": list(sampling.stop),
            "repeat_penalty": sampling.repeat_penalty,
        },
        "response_format": None
        if response_format is None
        else {"kind": response_format.kind.value, "schema": response_format.schema},
        "timeout_seconds": request.timeout_seconds,
        "metadata": dict(request.metadata),
    }


def _rebuild(name: str, tmp_path: Path) -> dict[str, Any]:
    fake = FakeProvider(FakeScript(models=(_model(),)))
    provider = RecordingProvider(fake)
    database = Database.from_url(f"sqlite:///{tmp_path / f'{name}.sqlite3'}")
    ensure_ready(database, auto_migrate=True)
    import_task_profiles(database, read_task_profiles_file(), now=NOW)
    discover_models(database, fake, now=NOW)
    try:
        execute(database, GenerateRequest(**CASES[name]), _context(provider))
    except SuiteError:
        # `structured.extract` fails validation against the fake's answer and retries; the request
        # this golden pins is the first one, which was built before any of that happened.
        pass
    finally:
        database.close()
    return _as_json(provider.requests[0])


@pytest.mark.parametrize("name", sorted(CASES))
def test_the_request_is_identical_to_the_dfbf2d8_golden(name: str, tmp_path: Path) -> None:
    golden = json.loads(GOLDEN_FILE.read_text(encoding="utf-8"))[name]

    rebuilt = _rebuild(name, tmp_path)

    assert json.dumps(rebuilt, sort_keys=True) == json.dumps(golden, sort_keys=True), (
        f"{name}: a body with neither 'tools' nor per-message 'tool_calls' no longer builds the "
        "request it built at dfbf2d8. The tool wire is additive within /api/v1 (api.md §4)."
    )


def test_the_golden_carries_no_tools_and_covers_every_input_form() -> None:
    """A golden that had drifted into carrying tools would pass while proving nothing."""
    golden = json.loads(GOLDEN_FILE.read_text(encoding="utf-8"))

    assert set(golden) == set(CASES)
    assert all("tools" not in body for body in golden.values())
    assert all("tool_calls" not in turn for body in golden.values() for turn in body["messages"])
    assert golden["transcript"]["messages"][2]["role"] == "assistant"
    assert golden["json_schema_profile"]["response_format"]["kind"] == "json"


def test_a_tool_free_request_carries_an_empty_tools_tuple(tmp_path: Path) -> None:
    """The golden cannot see `tools` because it predates the field; this asserts what it is now."""
    fake = FakeProvider(FakeScript(models=(_model(),)))
    provider = RecordingProvider(fake)
    database = Database.from_url(f"sqlite:///{tmp_path / 'empty.sqlite3'}")
    ensure_ready(database, auto_migrate=True)
    import_task_profiles(database, read_task_profiles_file(), now=NOW)
    discover_models(database, fake, now=NOW)
    try:
        execute(
            database, GenerateRequest(task="general.chat", prompt="Say hello."), _context(provider)
        )
    finally:
        database.close()
    assert provider.requests[0].tools == ()
