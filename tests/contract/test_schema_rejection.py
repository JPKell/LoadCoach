"""Contract: an unsupported schema major is rejected, and nothing is partially parsed.

Testing standards §8 rule 3's second half: *the consumer ... rejects the next major with
``SCHEMA_VERSION_UNSUPPORTED``.* Two claims live here and they are different:

* the **rejection** names both versions, so a reader learns whether to upgrade itself or its
  producer; and
* the **atomicity** — a rejected bundle writes nothing at all. That is what "must not partially
  parse" means, and it is asserted the only way it can be: import a good bundle, capture every
  column of every row, offer a rejected one, and compare byte for byte.

No FreeWeight process is involved, and no shared database.
"""

from __future__ import annotations

import copy
import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import pytest
from setspec import PUBLISHED_SCHEMAS, SchemaVersion, golden_payloads

from loadcoach.infrastructure.db.models import CapabilityEvidence, EvidenceSource
from loadcoach.services.database import Database, ensure_ready
from loadcoach.services.evidence import (
    BUNDLE_SCHEMA,
    EvidenceImportFailed,
    EvidenceSchemaVersionUnsupported,
    import_bundle,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

pytestmark = pytest.mark.contract

NOW = datetime(2026, 8, 29, 12, 0, tzinfo=UTC)

SUPPORTED_MAJORS = sorted({version.major for version in PUBLISHED_SCHEMAS[BUNDLE_SCHEMA]})
NEXT_MAJOR = max(SUPPORTED_MAJORS) + 1


def _database(tmp_path: Path) -> Database:
    database = Database.from_url(f"sqlite:///{tmp_path / 'reject.sqlite3'}")
    ensure_ready(database, auto_migrate=True)
    return database


def _populated_golden() -> dict[str, Any]:
    for payload in golden_payloads(BUNDLE_SCHEMA, SchemaVersion(1, 0)):
        if payload.get("evidence"):
            return payload
    message = "setspec ships no evidence_bundle golden carrying records"
    raise AssertionError(message)


def _rows(database: Database) -> list[dict[str, Any]]:
    """Every evidence row, column by column, so "untouched" can be compared literally."""
    with database.read() as session:
        return [
            {c.name: getattr(row, c.name) for c in CapabilityEvidence.__table__.columns}
            for row in session.query(CapabilityEvidence).order_by(CapabilityEvidence.id).all()
        ]


def _sources(database: Database) -> list[dict[str, Any]]:
    with database.read() as session:
        return [
            {c.name: getattr(row, c.name) for c in EvidenceSource.__table__.columns}
            for row in session.query(EvidenceSource).order_by(EvidenceSource.id).all()
        ]


def test_the_next_major_is_rejected_naming_both_versions(
    tmp_path: Path, wrap_bundle: Callable[..., str]
) -> None:
    database = _database(tmp_path)
    try:
        with pytest.raises(EvidenceSchemaVersionUnsupported) as caught:
            import_bundle(database, wrap_bundle(_populated_golden(), major=NEXT_MAJOR), now=NOW)
        error = caught.value
        assert error.code == "SCHEMA_VERSION_UNSUPPORTED"
        rendered = error.message + json.dumps(error.details, default=str)
        assert f"{NEXT_MAJOR}.0" in rendered, "the received version must be named"
        assert error.details is not None
        assert error.details["accepted_majors"] == SUPPORTED_MAJORS, (
            "the supported versions must be named too, or a reader cannot tell which side to fix"
        )
    finally:
        database.close()


@pytest.mark.parametrize("major", [NEXT_MAJOR, NEXT_MAJOR + 4, 99])
def test_a_rejected_bundle_leaves_existing_evidence_byte_identical(
    tmp_path: Path, wrap_bundle: Callable[..., str], major: int
) -> None:
    """The atomicity claim. A half-written import passes every other test and fails this one."""
    database = _database(tmp_path)
    try:
        good = _populated_golden()
        import_bundle(database, wrap_bundle(good), now=NOW)
        rows_before, sources_before = _rows(database), _sources(database)
        assert rows_before, "the good bundle must have written something to compare against"

        hostile = copy.deepcopy(good)
        for record in hostile["evidence"]:
            record["score"] = 0.0
            record["confidence"] = 0.05
        with pytest.raises(EvidenceSchemaVersionUnsupported):
            import_bundle(
                database,
                wrap_bundle(hostile, major=major),
                now=NOW + timedelta(hours=3),
            )

        assert _rows(database) == rows_before
        assert _sources(database) == sources_before
    finally:
        database.close()


def test_a_rejected_bundle_creates_no_source_row_on_a_fresh_install(
    tmp_path: Path, wrap_bundle: Callable[..., str]
) -> None:
    """Nothing at all is written — not even the row recording that a source was seen."""
    database = _database(tmp_path)
    try:
        with pytest.raises(EvidenceSchemaVersionUnsupported):
            import_bundle(database, wrap_bundle(_populated_golden(), major=NEXT_MAJOR), now=NOW)
        assert _rows(database) == []
        assert _sources(database) == []
    finally:
        database.close()


def test_a_document_of_another_schema_is_rejected_rather_than_duck_typed(
    tmp_path: Path, wrap_bundle: Callable[..., str]
) -> None:
    """Two payload types with compatible fields are still two payload types (ADR-0009)."""
    database = _database(tmp_path)
    try:
        document = json.loads(wrap_bundle(_populated_golden()))
        document["schema"] = "benchmark.result"
        with pytest.raises(EvidenceImportFailed):
            import_bundle(database, json.dumps(document), now=NOW)
        assert _rows(database) == []
    finally:
        database.close()


@pytest.mark.parametrize("minor", [0, 1, 7, 99])
def test_every_minor_within_a_supported_major_is_accepted(
    tmp_path: Path, wrap_bundle: Callable[..., str], minor: int
) -> None:
    """ADR-0009 rule 9: acceptance is decided by major, never by exact version."""
    database = _database(tmp_path)
    try:
        outcome = import_bundle(
            database,
            wrap_bundle(_populated_golden(), major=max(SUPPORTED_MAJORS), minor=minor),
            now=NOW,
        )
        assert outcome.imported == outcome.total
        assert outcome.schema_version == f"{max(SUPPORTED_MAJORS)}.{minor}"
    finally:
        database.close()


def test_rejection_does_not_depend_on_the_bundle_being_otherwise_valid(
    tmp_path: Path, wrap_bundle: Callable[..., str]
) -> None:
    """The version is decided before the payload is looked at, so garbage in a future major
    still produces ``SCHEMA_VERSION_UNSUPPORTED`` rather than a validation error about its
    contents. A consumer told "your records are malformed" would fix the wrong thing."""
    database = _database(tmp_path)
    try:
        nonsense = {"source_id": "x", "complete": "not a boolean", "evidence": "not a list"}
        with pytest.raises(EvidenceSchemaVersionUnsupported):
            import_bundle(database, wrap_bundle(nonsense, major=NEXT_MAJOR), now=NOW)
        assert _rows(database) == []
    finally:
        database.close()
