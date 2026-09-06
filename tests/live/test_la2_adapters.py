"""LA2's exit, live: three adapters on one base, one base load, one recorded denial.

Marked, not skipped: these run only under ``-m live``, on a machine with a real ``llama-server``,
a base GGUF and at least three LoRA adapter GGUFs for it.

    LCTEST_LLAMACPP_MODELS=~/ai/models/llm
    LCTEST_LLAMACPP_ADAPTERS=~/ai/models/adapters/llm
    LCTEST_LLAMACPP_BASE=Qwen2.5-1.5B-Instruct.Q8_0

The adapter directory the test hands LoadCoach is **built in the scratch directory**, not the one
above: an adapter is registered from a reviewed manifest (ADR-0061), and the reviewed manifests
here are written by the test against the digests it has just read from the registry, so the base
claim is a proof rather than a name. The artifacts are symlinked, because identity is the hash
and the path is only a locator.

What is asserted, and from what:

* **One base load** (I16), from three independent witnesses — the supervisor's server pid answers
  every generation, the provider reports exactly one ``load`` operation started, and no
  generation takes as long as the measured base load did.
* **Every attempt records its subject** (ADR-0080), read back from ``job_attempts``.
* **A recorded classification denial** (I19): the same weights registered a second time by a
  registration **declared** remote, routed under a profile that allows remote — so the refusal is
  the one no flag can fix, and it is a ``routing_candidates`` row, not a governance ledger entry
  (ADR-0079).
"""

from __future__ import annotations

import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import pytest

if TYPE_CHECKING:
    from loadcoach.services.database import Database

pytestmark = pytest.mark.live

MODELS = Path(os.environ.get("LCTEST_LLAMACPP_MODELS", "~/ai/models/llm")).expanduser()
ADAPTERS = Path(os.environ.get("LCTEST_LLAMACPP_ADAPTERS", "~/ai/models/adapters/llm")).expanduser()
BASE = os.environ.get("LCTEST_LLAMACPP_BASE", "Qwen2.5-1.5B-Instruct.Q8_0")

_PROFILES = """
[task_profiles."live.local"]
version = "1.0.0"
description = "A local turn on the small base, for the LA2 journey."
[task_profiles."live.local".weights]
instruction_following = 1.0
[task_profiles."live.local".constraints]
min_context_tokens = 2048
allow_remote_providers = false
[task_profiles."live.local".execution]
temperature = 0.0
max_output_tokens = 32
response_format = "text"
max_attempts = 1
fallback_depth = 0

[task_profiles."live.remote_ok"]
version = "1.0.0"
description = "The same turn, in a deployment that permits egress — I19's setting."
[task_profiles."live.remote_ok".weights]
instruction_following = 1.0
[task_profiles."live.remote_ok".constraints]
min_context_tokens = 2048
allow_remote_providers = true
[task_profiles."live.remote_ok".execution]
temperature = 0.0
max_output_tokens = 32
response_format = "text"
max_attempts = 1
fallback_depth = 0
"""


def _adapter_artifacts() -> list[Path]:
    return sorted(ADAPTERS.glob("*.gguf"))


def _requirements() -> str | None:
    """Say what is missing, or ``None`` when the machine can run this journey."""
    if not MODELS.is_dir():
        return f"no model directory at {MODELS}"
    if not (MODELS / f"{BASE}.gguf").is_file():
        return f"no base {BASE}.gguf in {MODELS}"
    if len(_adapter_artifacts()) < 3:
        return f"fewer than three adapter GGUFs in {ADAPTERS}"
    return None


def _write_manifests(directory: Path, *, base_digest: str, now: datetime) -> list[str]:
    """Symlink each adapter into ``directory`` and write the reviewed manifest beside it."""
    from setspec import GeneratorInfo, SchemaVersion, dump_envelope
    from setspec.model.v1 import AdapterManifestOut

    from loadcoach.infrastructure.adapters.directory import sha256_of

    names: list[str] = []
    for artifact in _adapter_artifacts()[:3]:
        name = artifact.stem.replace(".", "-").replace("_", "-").lower()
        link = directory / f"{name}.gguf"
        link.symlink_to(artifact)
        payload = AdapterManifestOut.model_validate(
            {
                "name": name,
                "artifact_file": link.name,
                "artifact_sha256": sha256_of(link),
                "base": {
                    "provider_model_name": BASE,
                    "artifact_digest": base_digest,
                    "identity_confidence": "digest",
                },
                "declared_capabilities": ["user.house_voice"],
                "data_classification": "confidential",
                "format": "gguf",
                "created_at": now.isoformat().replace("+00:00", "Z"),
            }
        )
        (directory / f"{name}.manifest.json").write_text(
            dump_envelope(
                payload,
                schema="model.adapter_manifest",
                version=SchemaVersion(1, 0),
                generator=GeneratorInfo(name="loadcoach-live", version="1.1.0"),
                generated_at=now,
            ),
            encoding="utf-8",
        )
        names.append(name)
    return names


def _wired(tmp_path: Path, *, remote: bool = False) -> tuple[Database, Any, Any]:
    """A database, a llama.cpp registration and the adapter directory it was handed."""
    from sqlalchemy import select

    from loadcoach.config import Settings
    from loadcoach.infrastructure.db.models import Model
    from loadcoach.infrastructure.providers.factory import build_registrations
    from loadcoach.services.adapters import sync_adapters
    from loadcoach.services.database import Database, ensure_ready
    from loadcoach.services.models import discover_models
    from loadcoach.services.task_profiles import import_task_profiles, read_task_profiles_file

    now = datetime.now(UTC)
    profiles_path = tmp_path / "task_profiles.toml"
    profiles_path.write_text(_PROFILES, encoding="utf-8")
    directory = tmp_path / "adapters"
    directory.mkdir(exist_ok=True)

    database = Database.from_url(f"sqlite:///{tmp_path / 'la2.sqlite3'}")
    ensure_ready(database, auto_migrate=True)
    import_task_profiles(database, read_task_profiles_file(profiles_path), now=now)

    # Discovery first, with no adapters configured: the base's digest is what the manifests must
    # claim, and claiming it from anywhere else would be a name-only guess dressed up as a proof.
    bare = Settings.model_validate(
        {
            "providers": {
                "allow_remote": True,
                "local": {
                    "kind": "llamacpp",
                    "model_directory": str(MODELS),
                    "state_dir": str(tmp_path / "llamacpp"),
                    "timeout_seconds": 300.0,
                },
            }
        }
    )
    discover_models(database, build_registrations(bare), now=now)
    with database.read() as session:
        base_row = (
            session.execute(select(Model).where(Model.provider_model_name == BASE)).scalars().one()
        )
        base_digest = base_row.artifact_digest
    assert base_digest, "the served base reports no digest"
    _write_manifests(directory, base_digest=base_digest, now=now)

    settings = Settings.model_validate(
        {
            "adapters": {"directory": str(directory)},
            "providers": {
                "allow_remote": True,
                ("hosted" if remote else "local"): {
                    "kind": "llamacpp",
                    "model_directory": str(MODELS),
                    "state_dir": str(tmp_path / "llamacpp"),
                    "timeout_seconds": 300.0,
                    "remote": remote,
                },
            },
        }
    )
    sync_adapters(database, settings, now=now)
    registrations = build_registrations(settings)
    discover_models(database, registrations, now=now)
    return database, registrations[0], settings


def _context(registration: Any, tmp_path: Path) -> Any:
    from loadcoach.services.execution import ExecutionContext, provider_facts_for
    from loadcoach.services.routing import RoutingPolicy
    from loadcoach.services.task_profiles import DEFAULT_SCHEMAS_DIR

    facts = provider_facts_for(
        registration.provider, adapters_registered=registration.adapters_registered
    )
    return ExecutionContext(
        provider=registration.provider,
        provider_facts=facts,
        provider_facts_by_name={registration.name: facts},
        provider_by_name={registration.name: registration.provider},
        policy=RoutingPolicy(),
        schemas_dir=DEFAULT_SCHEMAS_DIR,
        timeout_seconds=300.0,
    )


def test_three_pinned_adapters_answer_on_one_base_load(tmp_path: Path) -> None:
    """I16 at the LoadCoach boundary, and ADR-0080's provenance on every attempt."""
    missing = _requirements()
    if missing:
        pytest.fail(f"the LA2 journey needs a real llama.cpp setup: {missing}")

    from sqlalchemy import select

    from loadcoach.domain.routing.subject import RuntimeOverrides
    from loadcoach.infrastructure.db.models import JobAttempt
    from loadcoach.services.execution import GenerateRequest, execute

    database, registration, settings = _wired(tmp_path)
    try:
        provider = registration.provider
        assert registration.adapters_registered is True
        states = provider.list_adapters()
        names = sorted(state.adapter.name for state in states)
        assert len(names) == 3, [state.reason for state in states]

        context = _context(registration, tmp_path)
        walls_ms: list[float] = []
        pids: list[int] = []
        answers: list[str] = []
        for name in names:
            started = time.monotonic()
            outcome = execute(
                database,
                GenerateRequest(
                    task="live.local",
                    prompt="In one sentence, what is a KV cache?",
                    sampling={"temperature": 0.0, "max_output_tokens": 32, "seed": 7},
                    overrides=RuntimeOverrides(adapter=name),
                ),
                context,
            )
            walls_ms.append((time.monotonic() - started) * 1000.0)
            answers.append(outcome.text)
            handle = provider.supervisor.handle_for(BASE)
            assert handle is not None
            pids.append(handle.process.pid)

        with database.read() as session:
            attempts = list(
                session.execute(select(JobAttempt).order_by(JobAttempt.started_at)).scalars().all()
            )
        subjects = [attempt.subject_canonical_id for attempt in attempts]
        print(  # noqa: T201 — the evidence
            "\nI16 (LoadCoach): pids="
            + json.dumps(pids)
            + f" wall_ms={[round(value) for value in walls_ms]}"
            + "\nsubjects:\n  "
            + "\n  ".join(subject or "?" for subject in subjects)
            + "\nanswers:\n  "
            + "\n  ".join(answer.strip()[:100] for answer in answers)
        )

        assert len(set(pids)) == 1, "a second server answered: the base was reloaded"
        assert [resident.identity.provider_model_name for resident in provider.list_resident()] == [
            BASE
        ]
        assert len(attempts) == 3
        assert all(attempt.outcome == "completed" for attempt in attempts)
        assert all(attempt.adapter_id is not None for attempt in attempts)
        assert (
            sorted((subject or "").rpartition("+")[2].partition("@")[0] for subject in subjects)
            == names
        )
        assert all(attempt.effective_data_classification == "confidential" for attempt in attempts)
        assert all(answer.strip() for answer in answers)
    finally:
        registration.provider.close()
        database.close()


def test_an_adapter_on_a_remote_registration_leaves_a_recorded_denial(tmp_path: Path) -> None:
    """I19: the denial is a LoadCoach explanation row, not a governance ledger entry (ADR-0079).

    The registration is the *same* server, declared ``remote = true``: what LoadCoach evaluates is
    the declaration (ADR-0055 rule 4), and the point of the refusal is that no flag repairs it —
    the profile here already allows remote, and the candidate is still refused.
    """
    missing = _requirements()
    if missing:
        pytest.fail(f"the LA2 journey needs a real llama.cpp setup: {missing}")

    from sqlalchemy import select

    from loadcoach.infrastructure.db.models import RoutingCandidate
    from loadcoach.services.routing import RouteRequest, RoutingPolicy, route

    database, registration, settings = _wired(tmp_path, remote=True)
    try:
        from loadcoach.services.execution import provider_facts_for

        facts = provider_facts_for(
            registration.provider, adapters_registered=registration.adapters_registered
        )
        route(
            database,
            RouteRequest(task="live.remote_ok", estimated_input_tokens=200),
            provider=facts,
            provider_facts_by_name={registration.name: facts},
            policy=RoutingPolicy(require_adapter_evidence=False),
            now=datetime.now(UTC),
        )

        with database.read() as session:
            rows = list(session.execute(select(RoutingCandidate)).scalars().all())
        denials = [row for row in rows if row.rejection_reason == "adapter_classification_conflict"]
        served = [row for row in rows if not row.rejected]
        print(  # noqa: T201 — the evidence
            f"\nI19: {len(denials)} recorded denials, {len(served)} candidates served\n  "
            + "\n  ".join(
                f"{row.subject_canonical_id}: {json.dumps(row.rejection_detail_json)}"
                for row in denials
            )
        )
        assert len(denials) == 3
        assert all(row.adapter_id is not None for row in denials)
        for row in denials:
            detail = cast("dict[str, Any]", row.rejection_detail_json or {})
            assert detail["adapter_classification"] == "confidential"
            assert detail["effective_classification"] == "confidential"
            assert detail["provider_remote"] is True
        # The bare base is still servable: this refusal is about the adapter, not about egress.
        assert served, "the base itself was refused, so the denial proves nothing about adapters"
    finally:
        registration.provider.close()
        database.close()
