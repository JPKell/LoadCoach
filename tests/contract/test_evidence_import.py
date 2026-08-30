"""Contract: LoadCoach reads every published ``benchmark.evidence_bundle`` golden.

Testing standards §8 rule 3, from the consumer's side: *the consumer's test suite asserts that it
can read every golden payload for every supported major version.* The goldens are imported from
the **installed** ``setspec``, never hand-authored here — a bundle typed out in this file would
prove only that the importer agrees with this file.

These tests run with no FreeWeight process, no network and no shared database. That is the point:
the contract is the payload, and if it needed either application running to be checked, it would
not be a contract.
"""

from __future__ import annotations

import copy
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import pytest
from setspec import PUBLISHED_SCHEMAS, SchemaVersion, golden_names, golden_payloads
from setspec.capability.v1 import (
    CapabilityEvidenceIn,
    CapabilityEvidenceOut,
    EvidenceBundleIn,
    EvidenceBundleOut,
)

from loadcoach.domain.evidence_policy import MATCH_STATES
from loadcoach.infrastructure.db.models import CapabilityEvidence
from loadcoach.services.database import Database, ensure_ready
from loadcoach.services.evidence import BUNDLE_SCHEMA, import_bundle

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

pytestmark = pytest.mark.contract

NOW = datetime(2026, 8, 29, 12, 0, tzinfo=UTC)

BUNDLE_VERSIONS = PUBLISHED_SCHEMAS[BUNDLE_SCHEMA]
"""Every version of the bundle this build of SetSpec publishes artifacts for."""


def _database(tmp_path: Path) -> Database:
    database = Database.from_url(f"sqlite:///{tmp_path / 'contract.sqlite3'}")
    ensure_ready(database, auto_migrate=True)
    return database


def _bundle_goldens() -> list[tuple[SchemaVersion, str, dict[str, Any]]]:
    cases: list[tuple[SchemaVersion, str, dict[str, Any]]] = []
    for version in BUNDLE_VERSIONS:
        names = golden_names(BUNDLE_SCHEMA, version)
        payloads = golden_payloads(BUNDLE_SCHEMA, version)
        cases.extend(zip([version] * len(names), names, payloads, strict=True))
    return cases


GOLDEN_CASES = _bundle_goldens()


def test_setspec_publishes_the_bundle_and_the_record_this_build_expects() -> None:
    """The contract's own existence, asserted before anything is imported through it."""
    assert BUNDLE_SCHEMA in PUBLISHED_SCHEMAS
    assert "capability.evidence" in PUBLISHED_SCHEMAS
    assert SchemaVersion(1, 0) in BUNDLE_VERSIONS
    assert GOLDEN_CASES, "setspec publishes no evidence_bundle goldens for this build to read"


@pytest.mark.parametrize(
    ("version", "name", "payload"),
    GOLDEN_CASES,
    ids=[f"{version}-{name}" for version, name, _ in GOLDEN_CASES],
)
def test_every_published_bundle_golden_imports(
    tmp_path: Path,
    wrap_bundle: Callable[..., str],
    version: SchemaVersion,
    name: str,
    payload: dict[str, Any],
) -> None:
    """Every golden, at every published version, is readable by this consumer."""
    database = _database(tmp_path)
    try:
        outcome = import_bundle(
            database,
            wrap_bundle(payload, major=version.major, minor=version.minor),
            now=NOW,
        )
        assert outcome.rejected == (), f"{name} produced rejections: {outcome.rejected}"
        assert outcome.total == len(payload.get("evidence", []))
        assert outcome.imported == outcome.total
        assert outcome.source_key == payload["source_id"]
        assert outcome.complete == payload["complete"]
        with database.read() as session:
            assert session.query(CapabilityEvidence).count() == outcome.total
    finally:
        database.close()


@pytest.mark.parametrize(
    ("version", "name", "payload"),
    GOLDEN_CASES,
    ids=[f"{version}-{name}" for version, name, _ in GOLDEN_CASES],
)
def test_every_golden_round_trips_through_the_store_unchanged(
    tmp_path: Path,
    wrap_bundle: Callable[..., str],
    version: SchemaVersion,
    name: str,
    payload: dict[str, Any],
) -> None:
    """What ``GET /evidence`` re-emits is the producer's document, field for field.

    ADR-0025 §2 makes the collection's items ``capability.evidence`` envelopes, and data model §2
    says LoadCoach never edits evidence. Both are one assertion: every record that goes in comes
    back out validating against the same writer model that produced it.
    """
    database = _database(tmp_path)
    try:
        import_bundle(
            database,
            wrap_bundle(payload, major=version.major, minor=version.minor),
            now=NOW,
        )
        with database.read() as session:
            stored = session.query(CapabilityEvidence).order_by(CapabilityEvidence.id).all()
            records = [row.record_json for row in stored]
        for original, kept in zip(payload.get("evidence", []), records, strict=False):
            assert isinstance(kept, dict)
            # The writer model is strict (`extra="forbid"`), so this also proves the store
            # invented no field of its own.
            CapabilityEvidenceOut.model_validate(kept)
            assert kept["capability_id"] == original["capability_id"]
            assert kept["score"] == original["score"]
            assert kept["confidence"] == original["confidence"]
            assert kept["measured_at"] == original["measured_at"]
            assert kept["model"]["canonical_id"] == original["model"]["canonical_id"]
    finally:
        database.close()


def test_the_reader_accepts_a_minor_it_has_never_heard_of() -> None:
    """ADR-0009 rule 4: an unknown field survives a read rather than being destroyed."""
    payload = next(p for _v, _n, p in GOLDEN_CASES if p.get("evidence"))
    record = copy.deepcopy(payload["evidence"][0])
    record["a_field_from_a_later_minor"] = {"nested": [1, 2, 3]}
    parsed = CapabilityEvidenceIn.model_validate(record)
    dumped = parsed.model_dump(mode="json")
    assert dumped["a_field_from_a_later_minor"] == {"nested": [1, 2, 3]}


def test_the_writer_model_refuses_a_field_the_contract_does_not_define() -> None:
    """The other half of rule 4: writers never emit unknown fields."""
    from pydantic import ValidationError as PydanticValidationError

    payload = next(p for _v, _n, p in GOLDEN_CASES if p.get("evidence"))
    record = copy.deepcopy(payload["evidence"][0])
    record["invented"] = True
    with pytest.raises(PydanticValidationError):
        CapabilityEvidenceOut.model_validate(record)


def test_a_bundle_this_consumer_wrote_would_validate_for_the_producer() -> None:
    """The contract in the other direction: the shape LoadCoach reads is one a writer can emit."""
    payload = next(p for _v, _n, p in GOLDEN_CASES if p.get("evidence"))
    written = EvidenceBundleOut.model_validate(payload)
    read_back = EvidenceBundleIn.model_validate(written.model_dump(mode="json"))
    assert len(read_back.evidence) == len(payload["evidence"])


def test_the_match_states_this_consumer_stores_are_the_ones_the_adr_defines() -> None:
    """ADR-0022 §4's three states, pinned so a fourth cannot appear without a contract change."""
    assert MATCH_STATES == frozenset({"bound", "unmatched", "ambiguous_name_only"})


def test_importing_the_goldens_needs_no_freeweight_and_no_shared_database(
    tmp_path: Path, wrap_bundle: Callable[..., str]
) -> None:
    """I4's code half, as a test: nothing here imports ``freeweight`` or opens its database.

    ``.importlinter``'s ``no-other-applications`` contract forbids the import in ``src/``; this
    asserts the same for the path a contract test actually exercises, and that the only database
    touched is the one this test created.
    """
    import sys

    payload = next(p for _v, _n, p in GOLDEN_CASES if p.get("evidence"))
    database_path = tmp_path / "contract.sqlite3"
    database = _database(tmp_path)
    try:
        import_bundle(database, wrap_bundle(payload), now=NOW)
    finally:
        database.close()
    assert "freeweight" not in sys.modules
    assert not any(module.startswith("freeweight.") for module in sys.modules), (
        "the consumer must not import the producer's code"
    )
    assert database_path.is_file()
    assert sorted(p.name for p in tmp_path.glob("*.sqlite3")) == ["contract.sqlite3"]
