"""The importer against a real database (dev-plan P6, api.md §7, ADR-0022).

Every test here starts from a SetSpec golden rather than a hand-authored document: the contract
is the goldens, and a bundle typed out in this file would only ever prove that the importer
agrees with itself.
"""

from __future__ import annotations

import copy
import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import pytest
from modelrack.testing import FakeProvider

from loadcoach.infrastructure.db.models import CapabilityEvidence, EvidenceSource, Model
from loadcoach.services.database import Database, ensure_ready
from loadcoach.services.evidence import (
    MAX_PARSE_BYTES,
    EvidenceImportFailed,
    EvidenceSchemaVersionUnsupported,
    import_bundle,
    rebind_evidence,
)
from loadcoach.services.models import discover_models

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

NOW = datetime(2026, 8, 29, 12, 0, tzinfo=UTC)

DIGEST = "sha256:3f3f3f3f3f3f3f3f3f3f3f3f3f3f3f3f3f3f3f3f3f3f3f3f3f3f3f3f3f3f3f3f"


def _database(tmp_path: Path) -> Database:
    database = Database.from_url(f"sqlite:///{tmp_path / 'test.sqlite3'}")
    ensure_ready(database, auto_migrate=True)
    return database


def _add_model(
    database: Database,
    *,
    name: str = "qwen3.5:32b-instruct-q8_0",
    digest: str | None = DIGEST,
    kind: str = "ollama",
) -> str:
    canonical = f"{kind}/{name}@{digest[:19]}" if digest is not None else f"{kind}/{name}@unknown"
    with database.write() as session:
        model = Model(
            provider_kind=kind,
            provider_model_name=name,
            artifact_digest=digest,
            canonical_id=canonical,
            identity_confidence="digest" if digest else "name_only",
            first_seen_at=NOW,
            last_seen_at=NOW,
            available=True,
        )
        session.add(model)
        session.flush()
        return model.id


def _rows(database: Database) -> list[CapabilityEvidence]:
    with database.read() as session:
        return list(session.query(CapabilityEvidence).order_by(CapabilityEvidence.id).all())


def _snapshot(database: Database) -> list[dict[str, Any]]:
    """Every evidence row, column by column, so 'untouched' can be asserted literally."""
    with database.read() as session:
        return [
            {
                column.name: getattr(row, column.name)
                for column in CapabilityEvidence.__table__.columns
            }
            for row in session.query(CapabilityEvidence).order_by(CapabilityEvidence.id).all()
        ]


# --------------------------------------------------------------------------------------------
# The goldens import
# --------------------------------------------------------------------------------------------


def test_the_full_golden_imports_with_every_record_accounted_for(
    tmp_path: Path, golden_bundle: dict[str, Any], wrap_bundle: Callable[..., str]
) -> None:
    database = _database(tmp_path)
    try:
        outcome = import_bundle(database, wrap_bundle(golden_bundle), now=NOW)
        assert outcome.total == 3
        assert outcome.imported == 3
        assert outcome.updated == 0
        assert outcome.rejected == ()
        assert outcome.imported + outcome.updated == outcome.bound + outcome.unmatched + (
            outcome.ambiguous
        )
        assert outcome.source_key == "freeweight-bench-01"
        assert outcome.schema_version == "1.0"
        assert outcome.generated_at is not None
        assert len(_rows(database)) == 3
    finally:
        database.close()


def test_a_re_import_updates_rather_than_duplicating(
    tmp_path: Path, golden_bundle: dict[str, Any], wrap_bundle: Callable[..., str]
) -> None:
    database = _database(tmp_path)
    try:
        document = wrap_bundle(golden_bundle)
        import_bundle(database, document, now=NOW)
        second = import_bundle(database, document, now=NOW + timedelta(hours=1))
        assert second.imported == 0
        assert second.updated == 3
        assert len(_rows(database)) == 3
    finally:
        database.close()


def test_two_policy_versions_of_one_measurement_coexist(
    tmp_path: Path, golden_bundle: dict[str, Any], wrap_bundle: Callable[..., str]
) -> None:
    """ADR-0022 §3: ``policy_version`` is in the key so a policy change is not a collision."""
    database = _database(tmp_path)
    try:
        bundle = copy.deepcopy(golden_bundle)
        second = copy.deepcopy(bundle["evidence"][0])
        second["policy_version"] = "2.0.0"
        second["score"] = 0.91
        bundle["evidence"].append(second)
        outcome = import_bundle(database, wrap_bundle(bundle), now=NOW)
        assert outcome.imported == 4
        assert outcome.rejected == ()
        scores = {
            row.policy_version: row.score
            for row in _rows(database)
            if row.capability_id == "coding.python"
        }
        assert scores == {"1.0.0": 0.83, "2.0.0": 0.91}
    finally:
        database.close()


def test_two_records_on_one_key_are_rejected_not_merged(
    tmp_path: Path, golden_bundle: dict[str, Any], wrap_bundle: Callable[..., str]
) -> None:
    """The 'merging evidence across benchmark versions' failure mode, at import.

    The duplicate differs in suite version and score. A merging importer would leave one row
    holding a number no measurement produced; this one rejects the second by name and keeps the
    first exactly as it arrived.
    """
    database = _database(tmp_path)
    try:
        bundle = copy.deepcopy(golden_bundle)
        duplicate = copy.deepcopy(bundle["evidence"][0])
        duplicate["score"] = 0.11
        duplicate["benchmark_versions"] = {"native.coding": "9.9.9"}
        bundle["evidence"].append(duplicate)
        outcome = import_bundle(database, wrap_bundle(bundle), now=NOW)
        assert outcome.imported == 3
        assert [item.reason for item in outcome.rejected] == ["DUPLICATE_RECORD"]
        assert outcome.rejected[0].capability_id == "coding.python"
        stored = {row.capability_id: row for row in _rows(database)}
        assert stored["coding.python"].score == 0.83
        assert stored["coding.python"].benchmark_versions_json == {
            "external.humaneval": "1.0.0",
            "native.coding": "2.0.1",
        }
    finally:
        database.close()


def test_one_malformed_record_is_rejected_and_the_rest_import(
    tmp_path: Path, golden_bundle: dict[str, Any], wrap_bundle: Callable[..., str]
) -> None:
    database = _database(tmp_path)
    try:
        bundle = copy.deepcopy(golden_bundle)
        bundle["evidence"][0]["score"] = 4.2
        outcome = import_bundle(database, wrap_bundle(bundle), now=NOW)
        assert outcome.imported == 2
        assert [item.reason for item in outcome.rejected] == ["INVALID_RECORD"]
        assert outcome.rejected[0].index == 0
        assert "score" in outcome.rejected[0].detail
        assert {row.capability_id for row in _rows(database)} == {
            "user.noir_tech_voice",
            "reasoning",
        }
    finally:
        database.close()


# --------------------------------------------------------------------------------------------
# Version rejection must not partially parse
# --------------------------------------------------------------------------------------------


def test_an_unsupported_major_is_rejected_naming_both_versions(
    tmp_path: Path, golden_bundle: dict[str, Any], wrap_bundle: Callable[..., str]
) -> None:
    database = _database(tmp_path)
    try:
        with pytest.raises(EvidenceSchemaVersionUnsupported) as caught:
            import_bundle(database, wrap_bundle(golden_bundle, major=2), now=NOW)
        assert caught.value.code == "SCHEMA_VERSION_UNSUPPORTED"
        rendered = str(caught.value) + json.dumps(caught.value.details)
        assert "2.0" in rendered
        assert "1" in rendered
        assert caught.value.details is not None
        assert caught.value.details["accepted_majors"] == [1]
    finally:
        database.close()


def test_a_rejected_bundle_leaves_existing_evidence_byte_identical(
    tmp_path: Path, golden_bundle: dict[str, Any], wrap_bundle: Callable[..., str]
) -> None:
    """The atomicity claim: decide the version before the transaction opens.

    A naive importer that opens a transaction, writes the source row and half the records, then
    discovers the version, passes every other test in this file and fails this one.
    """
    database = _database(tmp_path)
    try:
        import_bundle(database, wrap_bundle(golden_bundle), now=NOW)
        before = _snapshot(database)
        with database.read() as session:
            sources_before = [
                {c.name: getattr(row, c.name) for c in EvidenceSource.__table__.columns}
                for row in session.query(EvidenceSource).all()
            ]

        newer = copy.deepcopy(golden_bundle)
        newer["source_id"] = "freeweight-bench-01"
        for record in newer["evidence"]:
            record["score"] = 0.01
        with pytest.raises(EvidenceSchemaVersionUnsupported):
            import_bundle(database, wrap_bundle(newer, major=2), now=NOW + timedelta(hours=2))

        assert _snapshot(database) == before
        with database.read() as session:
            sources_after = [
                {c.name: getattr(row, c.name) for c in EvidenceSource.__table__.columns}
                for row in session.query(EvidenceSource).all()
            ]
        assert sources_after == sources_before
    finally:
        database.close()


@pytest.mark.parametrize("accepted", [(), (3,), (2, 3)])
def test_the_configured_majors_are_what_decide_acceptance(
    tmp_path: Path,
    golden_bundle: dict[str, Any],
    wrap_bundle: Callable[..., str],
    accepted: tuple[int, ...],
) -> None:
    """Negotiation is LoadCoach's, not a side effect of what SetSpec happens to register.

    A ``1.0`` bundle against a build configured to accept only majors it does not carry must be
    refused. Without this, a test that only rejects ``2.0`` would pass even if
    ``accept_schema_majors`` were ignored entirely — SetSpec's own default registry rejects
    ``2.0`` anyway.
    """
    database = _database(tmp_path)
    try:
        with pytest.raises(EvidenceSchemaVersionUnsupported) as caught:
            import_bundle(
                database,
                wrap_bundle(golden_bundle),
                now=NOW,
                accept_schema_majors=accepted,
            )
        assert caught.value.details is not None
        assert caught.value.details["accepted_majors"] == list(accepted)
        assert "1.0" in str(caught.value) + json.dumps(caught.value.details)
        with database.read() as session:
            assert session.query(EvidenceSource).count() == 0
    finally:
        database.close()


def test_a_newer_minor_within_a_supported_major_is_accepted(
    tmp_path: Path, golden_bundle: dict[str, Any], wrap_bundle: Callable[..., str]
) -> None:
    """ADR-0009 rule 9's reader policy: acceptance is by major, never by exact version."""
    database = _database(tmp_path)
    try:
        outcome = import_bundle(database, wrap_bundle(golden_bundle, minor=7), now=NOW)
        assert outcome.imported == 3
        assert outcome.schema_version == "1.7"
    finally:
        database.close()


def test_a_document_of_another_schema_is_refused(
    tmp_path: Path, wrap_bundle: Callable[..., str]
) -> None:
    database = _database(tmp_path)
    try:
        document = json.loads(wrap_bundle({"source_id": "x", "complete": True, "evidence": []}))
        document["schema"] = "benchmark.result"
        with pytest.raises(EvidenceImportFailed):
            import_bundle(database, json.dumps(document), now=NOW)
        assert _rows(database) == []
    finally:
        database.close()


@pytest.mark.parametrize(
    ("payload", "field"),
    [
        ({"complete": True, "evidence": []}, "source_id"),
        ({"source_id": "s", "evidence": []}, "complete"),
        ({"source_id": "s", "complete": True, "evidence": {}}, "evidence"),
    ],
)
def test_a_malformed_bundle_wrapper_writes_nothing(
    tmp_path: Path,
    wrap_bundle: Callable[..., str],
    payload: dict[str, Any],
    field: str,
) -> None:
    database = _database(tmp_path)
    try:
        with pytest.raises(EvidenceImportFailed) as caught:
            import_bundle(database, wrap_bundle(payload), now=NOW)
        assert caught.value.details is not None
        assert caught.value.details["field"] == field
        with database.read() as session:
            assert session.query(EvidenceSource).count() == 0
    finally:
        database.close()


def test_an_oversize_document_is_refused_before_it_is_parsed(tmp_path: Path) -> None:
    database = _database(tmp_path)
    try:
        # Deliberately not valid JSON: if the size guard ran after parsing, this would fail with
        # a parse error rather than the size error the test asserts on.
        oversize = "{" * (MAX_PARSE_BYTES + 1)
        with pytest.raises(EvidenceImportFailed) as caught:
            import_bundle(database, oversize, now=NOW)
        assert caught.value.details is not None
        assert caught.value.details["max_parse_bytes"] == MAX_PARSE_BYTES
        with database.read() as session:
            assert session.query(EvidenceSource).count() == 0
    finally:
        database.close()


# --------------------------------------------------------------------------------------------
# ADR-0022 §4 — binding, at import and on the next discovery pass
# --------------------------------------------------------------------------------------------


def test_evidence_for_an_undiscovered_model_imports_as_unmatched(
    tmp_path: Path, golden_bundle: dict[str, Any], wrap_bundle: Callable[..., str]
) -> None:
    database = _database(tmp_path)
    try:
        outcome = import_bundle(database, wrap_bundle(golden_bundle), now=NOW)
        assert outcome.bound == 0
        assert outcome.unmatched == 3
        assert {row.match_state for row in _rows(database)} == {"unmatched"}
        assert all(row.model_id is None for row in _rows(database))
    finally:
        database.close()


def test_an_exact_identity_binds_at_import(
    tmp_path: Path, golden_bundle: dict[str, Any], wrap_bundle: Callable[..., str]
) -> None:
    database = _database(tmp_path)
    try:
        model_id = _add_model(database)
        outcome = import_bundle(database, wrap_bundle(golden_bundle), now=NOW)
        assert outcome.bound == 2  # the two digest-identified records
        bound = [row for row in _rows(database) if row.match_state == "bound"]
        assert {row.model_id for row in bound} == {model_id}
    finally:
        database.close()


def test_name_only_evidence_against_a_digested_model_is_ambiguous_and_unbound(
    tmp_path: Path, golden_bundle: dict[str, Any], wrap_bundle: Callable[..., str]
) -> None:
    """The third binding case: it never scores, and it is not 'scores with low confidence'."""
    database = _database(tmp_path)
    try:
        _add_model(database, name="qwen3.5:latest", digest=DIGEST)
        outcome = import_bundle(database, wrap_bundle(golden_bundle), now=NOW)
        assert outcome.ambiguous == 1
        ambiguous = [row for row in _rows(database) if row.match_state == "ambiguous_name_only"]
        assert len(ambiguous) == 1
        assert ambiguous[0].capability_id == "reasoning"
        assert ambiguous[0].model_id is None
    finally:
        database.close()


def test_a_digest_in_the_bundle_upgrades_a_local_name_only_row(
    tmp_path: Path, golden_bundle: dict[str, Any], wrap_bundle: Callable[..., str]
) -> None:
    database = _database(tmp_path)
    try:
        model_id = _add_model(database, digest=None)
        outcome = import_bundle(database, wrap_bundle(golden_bundle), now=NOW)
        assert outcome.upgraded_models == 1
        assert outcome.bound == 2
        with database.read() as session:
            model = session.get(Model, model_id)
            assert model is not None
            assert model.artifact_digest == DIGEST
            assert model.identity_confidence == "digest"
    finally:
        database.close()


def test_unmatched_evidence_binds_on_the_next_discovery_pass_with_no_re_import(
    tmp_path: Path, golden_bundle: dict[str, Any], wrap_bundle: Callable[..., str]
) -> None:
    """ADR-0022 §4's re-evaluation, end to end and without touching the importer again."""
    database = _database(tmp_path)
    try:
        bundle = copy.deepcopy(golden_bundle)
        provider = FakeProvider()
        descriptor = provider.list_models(refresh=True)[0]
        identity = descriptor.identity
        for record in bundle["evidence"]:
            record["model"]["provider_kind"] = identity.provider_kind.value
            record["model"]["provider_model_name"] = identity.provider_model_name
            record["model"]["artifact_digest"] = identity.artifact_digest
            record["model"]["canonical_id"] = identity.canonical_id
            record["model"]["identity_confidence"] = identity.identity_confidence.value

        outcome = import_bundle(database, wrap_bundle(bundle), now=NOW)
        assert outcome.unmatched == 3
        assert outcome.bound == 0

        discover_models(database, provider, now=NOW)

        rows = _rows(database)
        assert {row.match_state for row in rows} == {"bound"}
        assert all(row.model_id is not None for row in rows)
    finally:
        database.close()


def test_rebinding_is_idempotent(
    tmp_path: Path, golden_bundle: dict[str, Any], wrap_bundle: Callable[..., str]
) -> None:
    database = _database(tmp_path)
    try:
        _add_model(database)
        import_bundle(database, wrap_bundle(golden_bundle), now=NOW)
        first = rebind_evidence(database)
        second = rebind_evidence(database)
        assert first.bound == 0
        assert second.bound == 0
        assert second.examined == 3
    finally:
        database.close()


# --------------------------------------------------------------------------------------------
# Staleness, provenance and completeness
# --------------------------------------------------------------------------------------------


def test_freshness_uses_measured_at_not_computed_at(
    tmp_path: Path, golden_bundle: dict[str, Any], wrap_bundle: Callable[..., str]
) -> None:
    """A producer that re-aggregates old runs does not gain confidence here.

    Two imports of the same measurement: the second declares a ``computed_at`` of today. If
    freshness read the aggregation time, the row would come back fresh.
    """
    database = _database(tmp_path)
    try:
        bundle = copy.deepcopy(golden_bundle)
        bundle["evidence"] = [bundle["evidence"][0]]
        bundle["evidence"][0]["measured_at"] = "2026-01-01T00:00:00.000Z"
        bundle["evidence"][0]["computed_at"] = "2026-01-02T00:00:00.000Z"
        import_bundle(database, wrap_bundle(bundle), now=NOW)
        (row,) = _rows(database)
        assert row.stale
        assert row.stale_reason == "freshness"

        re_aggregated = copy.deepcopy(bundle)
        re_aggregated["evidence"][0]["computed_at"] = "2026-08-29T00:00:00.000Z"
        import_bundle(database, wrap_bundle(re_aggregated), now=NOW)
        (row,) = _rows(database)
        assert row.stale, "re-aggregating old runs must not make them look fresh"
        assert row.stale_reason == "freshness"
        assert row.confidence == 0.67, "confidence is FreeWeight's number, applied unchanged"
    finally:
        database.close()


def test_environment_drift_marks_a_row_stale_at_import(
    tmp_path: Path, golden_bundle: dict[str, Any], wrap_bundle: Callable[..., str]
) -> None:
    database = _database(tmp_path)
    try:
        import_bundle(
            database,
            wrap_bundle(golden_bundle),
            now=NOW,
            current_environment={"provider_kind": "ollama", "provider_version": "0.99.0"},
        )
        assert all(
            row.stale_reason == "environment_drift:provider_version" for row in _rows(database)
        )
    finally:
        database.close()


def test_a_complete_bundle_supersedes_rows_it_omits_and_never_deletes_them(
    tmp_path: Path, golden_bundle: dict[str, Any], wrap_bundle: Callable[..., str]
) -> None:
    database = _database(tmp_path)
    try:
        import_bundle(database, wrap_bundle(golden_bundle), now=NOW)
        narrowed = copy.deepcopy(golden_bundle)
        narrowed["evidence"] = [narrowed["evidence"][0]]
        outcome = import_bundle(database, wrap_bundle(narrowed), now=NOW + timedelta(hours=1))
        assert outcome.superseded == 2
        rows = {row.capability_id: row for row in _rows(database)}
        assert len(rows) == 3, "superseded rows are marked, never deleted"
        assert rows["reasoning"].stale_reason == "superseded"
        assert rows["coding.python"].stale_reason != "superseded"
    finally:
        database.close()


def test_an_incremental_bundle_removes_nothing(
    tmp_path: Path, golden_bundle: dict[str, Any], wrap_bundle: Callable[..., str]
) -> None:
    """ADR-0022 §5: only a **complete** bundle lets a consumer observe removals."""
    database = _database(tmp_path)
    try:
        import_bundle(database, wrap_bundle(golden_bundle), now=NOW)
        narrowed = copy.deepcopy(golden_bundle)
        narrowed["complete"] = False
        narrowed["evidence"] = [narrowed["evidence"][0]]
        outcome = import_bundle(database, wrap_bundle(narrowed), now=NOW + timedelta(hours=1))
        assert outcome.superseded == 0
        assert all(row.stale_reason != "superseded" for row in _rows(database))
    finally:
        database.close()


def test_the_source_row_records_the_producers_generated_at_not_our_clock(
    tmp_path: Path, golden_bundle: dict[str, Any], wrap_bundle: Callable[..., str]
) -> None:
    """ADR-0022 §5: the client stores the producer's ``generated_at`` and sends it back."""
    database = _database(tmp_path)
    try:
        produced = datetime(2026, 8, 21, 9, 30, tzinfo=UTC)
        import_bundle(
            database,
            wrap_bundle(golden_bundle, generated_at=produced),
            now=NOW,
            source_kind="freeweight_api",
            url="http://127.0.0.1:8765",
        )
        with database.read() as session:
            (source,) = session.query(EvidenceSource).all()
            assert source.generated_at == produced
            assert source.last_import_at == NOW
            assert source.kind == "freeweight_api"
            assert source.url == "http://127.0.0.1:8765"
            assert source.last_status == "ok"
            assert source.schema_version == "1.0"
    finally:
        database.close()


def test_the_goal_group_and_opaque_fields_are_stored_verbatim(
    tmp_path: Path, golden_bundle: dict[str, Any], wrap_bundle: Callable[..., str]
) -> None:
    database = _database(tmp_path)
    try:
        import_bundle(database, wrap_bundle(golden_bundle), now=NOW)
        rows = {row.capability_id: row for row in _rows(database)}
        goal = rows["user.noir_tech_voice"].goal_json
        assert isinstance(goal, dict)
        assert goal["calibration"]["kappa_w"] == 0.74
        assert goal["calibration"]["n_holdout"] == 18
        assert goal["judge_validity_factor"] == 0.64
        assert rows["coding.python"].source_run_ids_json == ["run_01JC8F2K", "run_01JC8F5Q"]
        assert rows["reasoning"].dispersion is None
        assert rows["reasoning"].dispersion_unavailable_reason is not None
    finally:
        database.close()
