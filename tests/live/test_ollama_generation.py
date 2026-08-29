"""Live generation against a real Ollama, marked and excluded from every default run.

Marked, not skipped: a skipped test reports as run-and-fine, which is the wrong signal for
something that has never touched a real provider. These execute only under ``-m live``, on a
machine that actually has Ollama serving a model.

Set ``LCTEST_OLLAMA_URL`` and ``LCTEST_OLLAMA_MODEL`` to point them at one. The ``LCTEST_``
prefix is deliberate: ``conftest.py`` strips every ``LOADCOACH_*`` variable so a developer's own
configuration cannot leak into a test, and harness configuration must survive that.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from loadcoach.services.database import Database

pytestmark = pytest.mark.live

OLLAMA_URL = os.environ.get("LCTEST_OLLAMA_URL", "http://127.0.0.1:11434")
OLLAMA_MODEL = os.environ.get("LCTEST_OLLAMA_MODEL", "")


def _live_setup(tmp_path: Path) -> tuple[Database, object]:
    from datetime import UTC, datetime

    from loadcoach.config import ProviderSettings
    from loadcoach.infrastructure.providers.factory import build_provider
    from loadcoach.services.database import Database, ensure_ready
    from loadcoach.services.models import discover_models
    from loadcoach.services.task_profiles import import_task_profiles, read_task_profiles_file

    provider = build_provider(
        ProviderSettings(kind="ollama", base_url=OLLAMA_URL, timeout_seconds=120)
    )
    database = Database.from_url(f"sqlite:///{tmp_path / 'live.sqlite3'}")
    ensure_ready(database, auto_migrate=True)
    import_task_profiles(database, read_task_profiles_file(), now=datetime.now(UTC))
    discover_models(database, provider, now=datetime.now(UTC))
    return database, provider


def _context(provider: object) -> object:
    from loadcoach.domain.routing.subject import ProviderFacts
    from loadcoach.services.execution import ExecutionContext
    from loadcoach.services.routing import RoutingPolicy
    from loadcoach.services.task_profiles import DEFAULT_SCHEMAS_DIR
    from loadcoach.web.routing_support import provider_facts_for

    facts: ProviderFacts = provider_facts_for(provider)  # type: ignore[arg-type]  # a live Provider
    return ExecutionContext(
        provider=provider,  # type: ignore[arg-type]  # a live Provider
        provider_facts=facts,
        policy=RoutingPolicy(),
        schemas_dir=DEFAULT_SCHEMAS_DIR,
        timeout_seconds=120.0,
    )


def test_live_generation_returns_real_text(tmp_path: Path) -> None:
    from loadcoach.services.execution import ExecutionContext, GenerateRequest, execute

    database, provider = _live_setup(tmp_path)
    try:
        context = _context(provider)
        assert isinstance(context, ExecutionContext)
        outcome = execute(
            database,
            GenerateRequest(task="general.chat", prompt="Reply with the single word: ready."),
            context,
        )
    finally:
        database.close()

    assert outcome.status == "completed"
    assert outcome.text.strip()
    assert outcome.provider_ms > 0, "a real provider took measurable time"
    assert outcome.input_tokens is not None
    assert outcome.output_tokens is not None
    assert outcome.selected.subject.runtime_profile_hash


def test_live_streaming_produces_ordered_tokens_then_a_result(tmp_path: Path) -> None:
    from loadcoach.services.execution import (
        ExecutionContext,
        GenerateRequest,
        StreamChunk,
        stream_execute,
    )

    database, provider = _live_setup(tmp_path)
    chunks: list[StreamChunk] = []
    try:
        context = _context(provider)
        assert isinstance(context, ExecutionContext)
        stream_execute(
            database,
            GenerateRequest(task="general.chat", prompt="Count from one to five."),
            context,
            on_chunk=chunks.append,
        )
    finally:
        database.close()

    kinds = [chunk.kind for chunk in chunks]
    assert kinds[0] == "routing"
    assert "token" in kinds
    assert kinds[-1] == "result"
    indexes = [chunk.payload["index"] for chunk in chunks if chunk.kind == "token"]
    assert indexes == list(range(len(indexes)))
