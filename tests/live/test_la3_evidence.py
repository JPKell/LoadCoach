"""I18's consumer half, live: an adapter subject's evidence crosses and changes a decision.

The claim is a **two-application** one, so what crosses is a file and nothing else: this test
imports a `benchmark.evidence_bundle` `1.1` that FreeWeight exported on this machine, through
LoadCoach's own documented path. It never imports `freeweight`, never reads FreeWeight's database,
and FreeWeight never reads LoadCoach's.

    LCTEST_LA3_BUNDLE=<a 1.1 bundle exported by FreeWeight's tests/live/test_la3_adapters.py>
    LCTEST_LLAMACPP_MODELS=~/ai/models/llm
    LCTEST_LLAMACPP_ADAPTERS=~/ai/models/adapters/llm
    LCTEST_LLAMACPP_BASE=Qwen2.5-1.5B-Instruct.Q8_0

What is asserted, and why each one is the thing that was broken:

* **Three records, zero rejections.** Under ADR-0022 §3's original key a base and its adapter
  subjects collapsed to one row and two of three were discarded as duplicates (ADR-0085).
* **Each record binds to its own subject**, adapter by artifact digest, once the directory scan
  has seen the adapters — with no re-import (ADR-0022 §4).
* **A measured adapter subject is selected because of that evidence**, with `benchmark` as the
  signal's source in the explanation, while the sibling nobody measured is rejected
  `adapter_unmeasured` in the same decision (ADR-0081, ADR-0064 rule 3).

The unmeasured adapter is the load-bearing half. A join that attached the base's evidence to every
subject on it would pass every other assertion here and would be exactly the failure ADR-0059 and
ADR-0081 exist to prevent.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from loadcoach.services.database import Database

pytestmark = pytest.mark.live

BUNDLE = Path(os.environ.get("LCTEST_LA3_BUNDLE", "")).expanduser()
MODELS = Path(os.environ.get("LCTEST_LLAMACPP_MODELS", "~/ai/models/llm")).expanduser()
ADAPTERS = Path(os.environ.get("LCTEST_LLAMACPP_ADAPTERS", "~/ai/models/adapters/llm")).expanduser()
BASE = os.environ.get("LCTEST_LLAMACPP_BASE", "Qwen2.5-1.5B-Instruct.Q8_0")

_PROFILE = """
[task_profiles."la3.reliability"]
version = "1.0.0"
description = "Route on measured reliability, which is the capability LA3 measured."
[task_profiles."la3.reliability".weights]
reliability = 1.0
[task_profiles."la3.reliability".constraints]
allow_remote_providers = false
[task_profiles."la3.reliability".execution]
temperature = 0.0
max_output_tokens = 32
response_format = "text"
max_attempts = 1
fallback_depth = 0
"""


def _requirements() -> str | None:
    """Say what is missing, or ``None`` when this machine can run I18's second half."""
    if not BUNDLE.is_file():
        return (
            "LCTEST_LA3_BUNDLE must name a `1.1` bundle exported by FreeWeight's "
            "tests/live/test_la3_adapters.py; it is the only thing that crosses"
        )
    if not (MODELS / f"{BASE}.gguf").is_file():
        return f"no base {BASE}.gguf in {MODELS}"
    if len(sorted(ADAPTERS.glob("*.gguf"))) < 2:
        return f"fewer than two adapter GGUFs in {ADAPTERS}"
    return None


def _subjects_in(document: dict[str, Any]) -> dict[str | None, str]:
    """The adapter name -> artifact digest the bundle carries, ``None`` for the bare base."""
    return {
        (record.get("adapter") or {}).get("name"): (record.get("adapter") or {}).get(
            "artifact_digest", ""
        )
        for record in document["payload"]["evidence"]
    }


def _wired(tmp_path: Path) -> tuple[Database, Any, Any]:
    """A database, the llama.cpp registration, and an adapter directory naming the real artifacts.

    The manifests are written here against the digests this registry has just read, exactly as
    LA2's journey does: an adapter is registered from a **reviewed** manifest, and the base claim
    has to be a proof rather than a name.
    """
    from setspec import GeneratorInfo, SchemaVersion, dump_envelope
    from setspec.model.v1 import AdapterManifestOut
    from sqlalchemy import select

    from loadcoach.config import Settings
    from loadcoach.infrastructure.adapters.directory import sha256_of
    from loadcoach.infrastructure.db.models import Model
    from loadcoach.infrastructure.providers.factory import build_registrations
    from loadcoach.services.database import Database, ensure_ready
    from loadcoach.services.models import discover_models
    from loadcoach.services.task_profiles import import_task_profiles, read_task_profiles_file

    now = datetime.now(UTC)
    profiles_path = tmp_path / "task_profiles.toml"
    profiles_path.write_text(_PROFILE, encoding="utf-8")
    directory = tmp_path / "adapters"
    directory.mkdir(exist_ok=True)

    database = Database.from_url(f"sqlite:///{tmp_path / 'la3.sqlite3'}")
    ensure_ready(database, auto_migrate=True)
    import_task_profiles(database, read_task_profiles_file(profiles_path), now=now)

    provider_block: dict[str, Any] = {
        "kind": "llamacpp",
        "model_directory": str(MODELS),
        "state_dir": str(tmp_path / "llamacpp"),
        "timeout_seconds": 300.0,
    }
    bare = Settings.model_validate({"providers": {"local": dict(provider_block)}})
    discover_models(database, build_registrations(bare), now=now)
    with database.read() as session:
        base_digest = (
            session.execute(select(Model).where(Model.provider_model_name == BASE))
            .scalars()
            .one()
            .artifact_digest
        )
    assert base_digest, "the served base reports no digest"

    for artifact in sorted(ADAPTERS.glob("*.gguf")):
        # The manifest's name is the adapter's, taken from the artifact's own trailing word, so
        # the names here are the names the exported bundle already carries.
        name = artifact.stem.rsplit("-", 1)[-1].lower()
        target = directory / f"{name}.gguf"
        target.symlink_to(artifact)
        payload = AdapterManifestOut.model_validate(
            {
                "name": name,
                "artifact_file": target.name,
                "artifact_sha256": sha256_of(target),
                "base": {
                    "provider_model_name": BASE,
                    "artifact_digest": base_digest,
                    "identity_confidence": "digest",
                },
                "declared_capabilities": ["reliability"],
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

    settings = Settings.model_validate(
        {
            "adapters": {"directory": str(directory)},
            "providers": {"local": dict(provider_block)},
        }
    )
    return database, settings, build_registrations(settings)


def test_la3_loadcoach_half(tmp_path: Path) -> None:
    """I18 whole: the file crosses, three subjects bind, and one of them changes the decision."""
    missing = _requirements()
    if missing:
        pytest.skip(missing)

    from loadcoach.infrastructure.db.models import CapabilityEvidence
    from loadcoach.services.adapters import sync_adapters
    from loadcoach.services.evidence import import_bundle
    from loadcoach.services.execution import provider_facts_for
    from loadcoach.services.routing import RouteRequest, RoutingPolicy, route

    document = json.loads(BUNDLE.read_text(encoding="utf-8"))
    assert document["schema_version"] == "1.1", "I18 needs an adapter-bearing bundle"
    carried = _subjects_in(document)
    assert None in carried, "the bare base's record must be in the same bundle"
    measured = sorted(name for name in carried if name is not None)
    assert measured, "the bundle carries no adapter subject"

    database, settings, registrations = _wired(tmp_path)
    now = datetime.now(UTC)
    try:
        # (1) The file is the only thing that crosses, through the documented path.
        outcome = import_bundle(database, BUNDLE.read_text(encoding="utf-8"), now=now)
        print(  # noqa: T201 — the evidence
            f"\nimported: {outcome.imported} record(s), {len(outcome.rejected)} rejected"
        )
        assert outcome.rejected == (), [item.detail for item in outcome.rejected]
        assert outcome.imported == len(carried)

        # (2) The adapters arrive, and the records bind on that pass with no re-import.
        sync_adapters(database, settings, now=now)
        with database.read() as session:
            rows = list(session.query(CapabilityEvidence).all())
            bound = {row.adapter_artifact_digest: (row.match_state, row.adapter_id) for row in rows}
        print(  # noqa: T201 — the evidence
            "bound subjects:"
        )
        for digest, (state, adapter_id) in sorted(bound.items()):
            print(  # noqa: T201 — the evidence
                f"  {digest or '(bare base)':<72} {state} adapter_id={adapter_id}"
            )
        assert len(bound) == len(carried)
        assert all(state == "bound" for state, _ in bound.values()), bound
        assert bound[""][1] is None, "the bare base's row binds to no adapter"
        for name in measured:
            assert bound[carried[name]][1] is not None, f"{name} did not bind to its adapter"

        # (3) The decision changes, and the sibling nobody measured is refused in the same run.
        registration = registrations[0]
        facts = provider_facts_for(
            registration.provider, adapters_registered=registration.adapters_registered
        )
        result = route(
            database,
            RouteRequest(task="la3.reliability", estimated_input_tokens=64),
            provider=facts,
            provider_facts_by_name={registration.name: facts},
            policy=RoutingPolicy(),
            now=now,
        )
        payload = result.explanation.payload
        selected = payload["selected"]["subject_canonical_id"]
        print(  # noqa: T201 — the evidence
            f"\nselected: {selected}"
        )
        assert any(f"+{name}@" in selected for name in measured), selected

        primary = result.explanation.ranking.primary
        assert primary is not None
        sources = {
            score.capability_id: score.source for score in primary.fit.capabilities if score.present
        }
        print(  # noqa: T201 — the evidence
            f"the signal that scored it: {sources}"
        )
        assert sources.get("reliability") == "benchmark"

        unmeasured = [
            entry
            for entry in payload["rejected"]
            if entry["reason"] == "adapter_unmeasured"
            and not any(f"+{name}@" in entry["subject_canonical_id"] for name in measured)
        ]
        print(  # noqa: T201 — the evidence
            "rejected, measured nowhere:"
        )
        for entry in unmeasured:
            print(  # noqa: T201 — the evidence
                f"  {entry['subject_canonical_id']}: {entry['reason']}"
            )
        assert unmeasured, "the unmeasured sibling must still be refused by name"
    finally:
        database.close()
