"""The adapter registry: a directory, a reviewed manifest, and everything it refuses (ADR-0061).

No provider, no database, no network — the registry *is* the directory, which is the whole point
of the decision these tests hold. Manifests are built through SetSpec's own writer model, never
hand-typed as JSON, so a contract change breaks these tests rather than sliding past them.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from baseaicore import DataClassification, IdentityConfidence
from setspec import GeneratorInfo, SchemaVersion, dump_envelope

from loadcoach.config import Settings
from loadcoach.infrastructure.adapters import (
    DRAFT_SUFFIX,
    MANIFEST_SUFFIX,
    draft_manifests,
    read_directory,
    registrations_from,
)
from loadcoach.services.adapters import (
    AdapterNotFound,
    AdaptersDisabled,
    adapter_overview,
    scan_adapters,
    show_adapter,
)

NOW = datetime(2026, 9, 5, 12, 0, tzinfo=UTC)
BASE_DIGEST = "sha256:" + "b" * 64


def _artifact(directory: Path, name: str, content: bytes = b"lora-weights") -> Path:
    path = directory / f"{name}.gguf"
    path.write_bytes(content)
    return path


def _manifest(
    directory: Path,
    artifact: Path,
    *,
    name: str | None = None,
    digest: str | None = None,
    base_digest: str | None = BASE_DIGEST,
    capabilities: tuple[str, ...] = ("auditing.fact_check",),
    classification: DataClassification = DataClassification.CONFIDENTIAL,
    suffix: str = MANIFEST_SUFFIX,
) -> Path:
    """Write a reviewed manifest for ``artifact``, through SetSpec's own contract."""
    from setspec.model.v1 import AdapterManifestOut

    from loadcoach.infrastructure.adapters.directory import sha256_of

    payload = AdapterManifestOut.model_validate(
        {
            "name": name or artifact.stem,
            "artifact_file": artifact.name,
            "artifact_sha256": digest or sha256_of(artifact),
            "base": {
                "provider_model_name": "qwen3.5:9b",
                "artifact_digest": base_digest,
                "identity_confidence": (
                    IdentityConfidence.DIGEST.value
                    if base_digest
                    else IdentityConfidence.NAME_ONLY.value
                ),
            },
            "declared_capabilities": list(capabilities),
            "data_classification": classification.value,
            "format": "gguf",
            "created_at": NOW.isoformat().replace("+00:00", "Z"),
        }
    )
    path = directory / f"{artifact.stem}{suffix}"
    path.write_text(
        dump_envelope(
            payload,
            schema="model.adapter_manifest",
            version=SchemaVersion(1, 0),
            generator=GeneratorInfo(name="test", version="1.0.0"),
            generated_at=NOW,
        ),
        encoding="utf-8",
    )
    return path


# --------------------------------------------------------------------------------------------
# Reading the directory
# --------------------------------------------------------------------------------------------


def test_a_reviewed_manifest_round_trips_into_an_available_entry(tmp_path: Path) -> None:
    artifact = _artifact(tmp_path, "factcheck")
    _manifest(tmp_path, artifact)

    reading = read_directory(tmp_path)

    (entry,) = reading.entries
    assert entry.available
    assert entry.name == "factcheck"
    assert entry.artifact_path == artifact.resolve()
    assert entry.base_confidence is IdentityConfidence.DIGEST
    assert entry.data_classification is DataClassification.CONFIDENTIAL
    assert entry.declared_capabilities == ("auditing.fact_check",)
    assert reading.invalid == ()
    assert reading.unmanifested == ()


def test_a_renamed_artifact_is_the_same_subject_once_the_manifest_names_it(
    tmp_path: Path,
) -> None:
    """Identity is the hash and the path is a locator (ADR-0061 rule 5).

    Renaming the file with a stale manifest takes the adapter out of service, loudly; renaming it
    *and* the manifest's `artifact_file` keeps the same identity, because nothing about the
    content changed.
    """
    artifact = _artifact(tmp_path, "factcheck")
    _manifest(tmp_path, artifact)
    before = read_directory(tmp_path).entries[0]

    renamed = artifact.rename(tmp_path / "factcheck-v2.gguf")
    stale = read_directory(tmp_path).entries[0]
    assert not stale.available
    assert "not a file" in (stale.unavailable_reason or "")

    (tmp_path / f"factcheck{MANIFEST_SUFFIX}").unlink()
    _manifest(tmp_path, renamed, name="factcheck")
    after = read_directory(tmp_path).entries[0]

    assert after.available
    assert after.artifact_sha256 == before.artifact_sha256


def test_an_edited_artifact_is_a_different_adapter_and_is_refused(tmp_path: Path) -> None:
    """Fail closed: measurements attached to the old hash must never reach new weights."""
    artifact = _artifact(tmp_path, "factcheck")
    _manifest(tmp_path, artifact)
    artifact.write_bytes(b"different weights entirely")

    (entry,) = read_directory(tmp_path).entries

    assert not entry.available
    assert "hashes to" in (entry.unavailable_reason or "")
    assert registrations_from([entry]) == ()


def test_a_manifest_that_is_not_the_contract_is_reported_not_guessed_at(tmp_path: Path) -> None:
    (tmp_path / f"broken{MANIFEST_SUFFIX}").write_text('{"not": "an envelope"}', encoding="utf-8")

    reading = read_directory(tmp_path)

    assert reading.entries == ()
    ((path, problem),) = reading.invalid
    assert path.name == f"broken{MANIFEST_SUFFIX}"
    assert problem


def test_a_manifest_without_a_classification_is_invalid(tmp_path: Path) -> None:
    """ADR-0065 rule 1: required, no default — an unreviewed manifest cannot claim `public`."""
    artifact = _artifact(tmp_path, "factcheck")
    document = json.loads(_manifest(tmp_path, artifact).read_text(encoding="utf-8"))
    del document["payload"]["data_classification"]
    (tmp_path / f"factcheck{MANIFEST_SUFFIX}").write_text(json.dumps(document), encoding="utf-8")

    reading = read_directory(tmp_path)

    assert reading.entries == ()
    ((_, problem),) = reading.invalid
    assert "data_classification" in problem


def test_a_draft_is_never_read_as_a_registration(tmp_path: Path) -> None:
    """ "The scan drafts, a human keeps" is enforced by the suffix, not by a flag inside."""
    artifact = _artifact(tmp_path, "factcheck")
    _manifest(tmp_path, artifact, suffix=DRAFT_SUFFIX)

    reading = read_directory(tmp_path)

    assert reading.entries == ()
    assert [path.name for path in reading.drafts] == [f"factcheck{DRAFT_SUFFIX}"]


def test_a_missing_directory_is_an_empty_reading_not_an_error(tmp_path: Path) -> None:
    reading = read_directory(tmp_path / "nowhere")
    assert reading.entries == ()
    assert reading.invalid == ()


# --------------------------------------------------------------------------------------------
# The conversion into ModelRack's type
# --------------------------------------------------------------------------------------------


def test_an_available_entry_converts_into_modelracks_registration(tmp_path: Path) -> None:
    """The conversion lives in the application; ModelRack never reads a directory."""
    artifact = _artifact(tmp_path, "factcheck")
    _manifest(tmp_path, artifact)

    (registration,) = registrations_from(read_directory(tmp_path).entries)

    assert registration.name == "factcheck"
    assert registration.artifact_path == artifact.resolve()
    assert registration.base_artifact_digest == BASE_DIGEST
    assert registration.data_classification is DataClassification.CONFIDENTIAL
    assert registration.identity.name == "factcheck"


# --------------------------------------------------------------------------------------------
# Scanning
# --------------------------------------------------------------------------------------------


def test_scan_drafts_a_manifest_a_person_must_still_review(tmp_path: Path) -> None:
    _artifact(tmp_path, "factcheck")

    outcome = draft_manifests(tmp_path, now=NOW)

    (draft,) = outcome.drafted
    assert draft.name == f"factcheck{DRAFT_SUFFIX}"
    payload = json.loads(draft.read_text(encoding="utf-8"))["payload"]
    # Confidential by default, so a reviewed value can only ever be relaxed on purpose.
    assert payload["data_classification"] == DataClassification.CONFIDENTIAL.value
    # A capability claim is a person's assertion, never a scanner's guess.
    assert payload["declared_capabilities"] == []
    # A base named by a scanner is named, not proved.
    assert payload["base"]["artifact_digest"] is None
    assert payload["base"]["identity_confidence"] == IdentityConfidence.NAME_ONLY.value
    # And the draft does not register: the directory still holds no reviewed manifest.
    assert read_directory(tmp_path).entries == ()


def test_scan_never_overwrites_a_manifest_a_person_kept(tmp_path: Path) -> None:
    artifact = _artifact(tmp_path, "factcheck")
    manifest = _manifest(tmp_path, artifact)
    kept = manifest.read_bytes()

    outcome = draft_manifests(tmp_path, now=NOW)

    assert outcome.drafted == ()
    assert [reason for _, reason in outcome.skipped] == ["a reviewed manifest already names it"]
    assert manifest.read_bytes() == kept


def test_scan_does_not_redraft_a_draft_already_waiting(tmp_path: Path) -> None:
    _artifact(tmp_path, "factcheck")
    first = draft_manifests(tmp_path, now=NOW)
    written = first.drafted[0].read_bytes()

    second = draft_manifests(tmp_path, now=NOW)

    assert second.drafted == ()
    assert first.drafted[0].read_bytes() == written


def test_scan_reads_a_peft_base_name_when_one_is_beside_the_artifact(tmp_path: Path) -> None:
    _artifact(tmp_path, "factcheck")
    (tmp_path / "adapter_config.json").write_text(
        json.dumps({"base_model_name_or_path": "Qwen/Qwen3.5-9B"}), encoding="utf-8"
    )

    (draft,) = draft_manifests(tmp_path, now=NOW).drafted

    payload = json.loads(draft.read_text(encoding="utf-8"))["payload"]
    assert payload["base"]["provider_model_name"] == "Qwen/Qwen3.5-9B"
    assert payload["base"]["identity_confidence"] == IdentityConfidence.NAME_ONLY.value


def test_a_drafted_manifest_becomes_a_registration_when_it_is_kept(tmp_path: Path) -> None:
    """The whole adoption workflow: drop the file, scan, review, rename, registered."""
    _artifact(tmp_path, "factcheck")
    (draft,) = draft_manifests(tmp_path, now=NOW).drafted

    document = json.loads(draft.read_text(encoding="utf-8"))
    document["payload"]["base"]["provider_model_name"] = "qwen3.5:9b"
    document["payload"]["declared_capabilities"] = ["auditing.fact_check"]
    kept = tmp_path / f"factcheck{MANIFEST_SUFFIX}"
    kept.write_text(json.dumps(document), encoding="utf-8")
    draft.unlink()

    (entry,) = read_directory(tmp_path).entries

    assert entry.available
    assert entry.base_confidence is IdentityConfidence.NAME_ONLY
    assert entry.declared_capabilities == ("auditing.fact_check",)


def test_scan_against_a_missing_directory_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        draft_manifests(tmp_path / "nowhere", now=NOW)


# --------------------------------------------------------------------------------------------
# The service: off by default, and honest about what it holds
# --------------------------------------------------------------------------------------------


def _settings(directory: Path | None) -> Settings:
    payload: dict[str, Any] = {"provider": {"kind": "fake"}}
    if directory is not None:
        payload["adapters"] = {"directory": str(directory)}
    return Settings.model_validate(payload)


def test_the_feature_is_off_until_a_directory_is_configured() -> None:
    """ADR-0061 rule 2: empty means off, and off says which key turns it on."""
    with pytest.raises(AdaptersDisabled, match=r"\[adapters\] directory"):
        adapter_overview(_settings(None))
    with pytest.raises(AdaptersDisabled):
        scan_adapters(_settings(None))


def test_the_overview_reports_the_directory_with_no_provider_at_all(tmp_path: Path) -> None:
    artifact = _artifact(tmp_path, "factcheck")
    _manifest(tmp_path, artifact)
    _artifact(tmp_path, "unmanifested")

    overview = adapter_overview(_settings(tmp_path))

    (view,) = overview.adapters
    assert view.entry.name == "factcheck"
    assert view.registered_on == ()
    assert [path.name for path in overview.unmanifested] == ["unmanifested.gguf"]


def test_show_refuses_a_name_only_a_draft_carries(tmp_path: Path) -> None:
    artifact = _artifact(tmp_path, "factcheck")
    _manifest(tmp_path, artifact, suffix=DRAFT_SUFFIX)

    with pytest.raises(AdapterNotFound, match="factcheck"):
        show_adapter("factcheck", _settings(tmp_path))


def test_a_provider_that_cannot_hot_swap_is_offered_nothing(tmp_path: Path) -> None:
    """The local-only invariant by construction: no `adapter_hot_swap`, no adapters."""
    from loadcoach.infrastructure.providers.factory import build_registrations

    artifact = _artifact(tmp_path, "factcheck")
    _manifest(tmp_path, artifact)

    (registration,) = build_registrations(_settings(tmp_path))

    assert registration.provider.capabilities().adapter_hot_swap is False
    overview = adapter_overview(_settings(tmp_path), (registration,))
    (view,) = overview.adapters
    assert view.registered_on == ()
    assert view.provider_notes == ()
