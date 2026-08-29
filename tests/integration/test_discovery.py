"""Tests for loadcoach.services.models.discover_models against a real database."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from modelrack.testing import FakeProvider

from loadcoach.services.database import Database, ensure_ready
from loadcoach.services.models import (
    discover_models,
    import_manual_capability_scores,
    list_registry,
)


def _database(tmp_path: Path) -> Database:
    database = Database.from_url(f"sqlite:///{tmp_path / 'test.sqlite3'}")
    ensure_ready(database, auto_migrate=True)
    return database


def test_discovery_adds_new_models(tmp_path: Path) -> None:
    database = _database(tmp_path)
    try:
        provider = FakeProvider()
        outcome = discover_models(database, provider, now=datetime.now(UTC))
        assert outcome.added == outcome.total
        assert outcome.updated == 0
        entries = list_registry(database)
        assert len(entries) == outcome.total
        assert all(entry.available for entry in entries)
    finally:
        database.close()


def test_discovery_is_idempotent(tmp_path: Path) -> None:
    """dev-plan P2 test list: discovery idempotent."""
    database = _database(tmp_path)
    try:
        provider = FakeProvider()
        first = discover_models(database, provider, now=datetime.now(UTC))
        second = discover_models(database, provider, now=datetime.now(UTC))
        assert second.added == 0
        assert second.total == first.total
        entries_after_first = list_registry(database)
        entries_after_second = list_registry(database)
        assert {e.canonical_id for e in entries_after_first} == {
            e.canonical_id for e in entries_after_second
        }
        assert len(entries_after_second) == len(entries_after_first)
    finally:
        database.close()


def test_unavailable_model_is_flagged_not_deleted(tmp_path: Path) -> None:
    """dev-plan P2 test list: unavailable models flagged with a reason, not deleted."""
    from modelrack.testing import FakeScript

    database = _database(tmp_path)
    try:
        provider = FakeProvider()
        discover_models(database, provider, now=datetime.now(UTC))
        before = list_registry(database)
        assert before
        assert all(entry.available for entry in before)

        # A provider that now reports nothing (script with an empty model catalogue).
        empty_provider = FakeProvider(FakeScript(models=()))
        outcome = discover_models(database, empty_provider, now=datetime.now(UTC))
        assert outcome.unavailable == len(before)

        after = list_registry(database)
        assert len(after) == len(before)  # nothing deleted
        assert all(not entry.available for entry in after)
        assert all(entry.unavailable_reason for entry in after)
    finally:
        database.close()


def test_model_reappearing_is_marked_available_again(tmp_path: Path) -> None:
    from modelrack.testing import FakeScript

    database = _database(tmp_path)
    try:
        provider = FakeProvider()
        discover_models(database, provider, now=datetime.now(UTC))
        empty_provider = FakeProvider(FakeScript(models=()))
        discover_models(database, empty_provider, now=datetime.now(UTC))
        assert all(not entry.available for entry in list_registry(database))

        discover_models(database, provider, now=datetime.now(UTC))
        entries = list_registry(database)
        assert all(entry.available for entry in entries)
        assert all(entry.unavailable_reason is None for entry in entries)
    finally:
        database.close()


def test_discovered_model_persists_declared_capabilities(tmp_path: Path) -> None:
    database = _database(tmp_path)
    try:
        provider = FakeProvider()
        discover_models(database, provider, now=datetime.now(UTC))
        entries = list_registry(database)
        assert any(entry.declared_capabilities for entry in entries)
    finally:
        database.close()


def test_missing_manual_scores_file_is_not_an_error(tmp_path: Path) -> None:
    database = _database(tmp_path)
    try:
        count = import_manual_capability_scores(
            database, path=tmp_path / "does-not-exist.toml", now=datetime.now(UTC)
        )
        assert count == 0
    finally:
        database.close()


def test_manual_score_for_undiscovered_model_is_skipped_not_an_error(tmp_path: Path) -> None:
    scores_file = tmp_path / "manual.toml"
    scores_file.write_text(
        '[[scores]]\ncanonical_id = "ollama/does-not-exist@sha256:'
        + "a" * 64
        + '"\ncapability_id = "coding"\nscore = 0.5\nconfidence = 0.3\n'
    )
    database = _database(tmp_path)
    try:
        count = import_manual_capability_scores(database, path=scores_file, now=datetime.now(UTC))
        assert count == 0
    finally:
        database.close()


def test_manual_score_for_a_discovered_model_is_imported(tmp_path: Path) -> None:
    database = _database(tmp_path)
    try:
        provider = FakeProvider()
        discover_models(database, provider, now=datetime.now(UTC))
        canonical_id = list_registry(database)[0].canonical_id

        scores_file = tmp_path / "manual.toml"
        scores_file.write_text(
            f'[[scores]]\ncanonical_id = "{canonical_id}"\ncapability_id = "coding"\n'
            "score = 0.6\nconfidence = 0.3\n"
        )
        count = import_manual_capability_scores(database, path=scores_file, now=datetime.now(UTC))
        assert count == 1

        from loadcoach.infrastructure.db.models import ModelCapability

        with database.read() as session:
            manual_rows = session.query(ModelCapability).filter_by(source="manual").all()
        assert len(manual_rows) == 1
        assert manual_rows[0].capability_id == "coding"
        assert manual_rows[0].score == 0.6

        # Unaffected: manual scores are a separate source from declared ones.
        entry = list_registry(database)[0]
        assert entry.declared_capabilities
    finally:
        database.close()
