"""LoadCoach's own migration 0001, and the WeightsDB two-schemas proof (dev-plan P1 Tests)."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from sqlalchemy import text
from weightsdb import MigrationRunner
from weightsdb.errors import MigrationFailed
from weightsdb.testing import temporary_postgres, temporary_sqlite

from loadcoach.infrastructure.db.models import Base
from loadcoach.services.database import MIGRATIONS_LOCATION

_OTHER_APP_SCRIPT_LOCATION = str(Path(__file__).parent / "_other_app_fixture")
_OTHER_APP_TABLE = "machines"

_BROKEN_REVISION = (
    '"""broken\n\nRevision ID: 0002\nRevises: 0001\n"""\n'
    "from __future__ import annotations\n\n"
    'revision: str = "0002"\n'
    'down_revision: str | None = "0001"\n'
    "branch_labels = None\n"
    "depends_on = None\n\n"
    "def upgrade() -> None:\n"
    "    raise RuntimeError('deliberate failure')\n\n"
    "def downgrade() -> None:\n"
    "    pass\n"
)


def test_fresh_database_migrates_to_head_sqlite() -> None:
    with temporary_sqlite() as engine:
        runner = MigrationRunner(engine, script_location=MIGRATIONS_LOCATION)
        assert runner.current() is None
        outcome = runner.upgrade(backup=False)
        assert outcome.to_revision == "0001"
        assert runner.is_at_head()


def test_fresh_database_migrates_to_head_postgres() -> None:
    with temporary_postgres() as engine:
        runner = MigrationRunner(engine, script_location=MIGRATIONS_LOCATION)
        outcome = runner.upgrade(backup=False)
        assert outcome.to_revision == "0001"
        assert runner.is_at_head()


def test_upgrade_head_twice_is_idempotent() -> None:
    with temporary_sqlite() as engine:
        runner = MigrationRunner(engine, script_location=MIGRATIONS_LOCATION)
        runner.upgrade(backup=False)
        second = runner.upgrade(backup=False)
        assert second.backed_up is False
        assert second.from_revision == second.to_revision == "0001"


def test_check_parity_matches_models_after_upgrade() -> None:
    """models.py and migration 0001 describe the same schema (database standards §5.2)."""
    with temporary_sqlite() as engine:
        runner = MigrationRunner(engine, script_location=MIGRATIONS_LOCATION)
        runner.upgrade(backup=False)
        result = runner.check_parity(Base.metadata)
        assert result.matches, result.diff


def test_check_parity_matches_on_postgresql() -> None:
    with temporary_postgres() as engine:
        runner = MigrationRunner(engine, script_location=MIGRATIONS_LOCATION)
        runner.upgrade(backup=False)
        result = runner.check_parity(Base.metadata)
        assert result.matches, result.diff


def test_two_independent_migration_histories_share_one_database_no_collision() -> None:
    """Gold standard: shared plumbing, zero shared schema, proved side by side.

    A stand-in for a second application's own schema (see ``_other_app_fixture``'s module
    docstring for why it is a stand-in rather than the real FreeWeight package) migrates through
    the *same* WeightsDB plumbing, against the *same* physical database file as LoadCoach's own
    migration, using a distinct ``version_table`` — exercising the specific failure mode named for
    this phase: version-table naming collisions between the two schemas.
    """
    with temporary_sqlite() as engine:
        loadcoach_runner = MigrationRunner(engine, script_location=MIGRATIONS_LOCATION)
        loadcoach_runner.upgrade(backup=False)

        other_runner = MigrationRunner(
            engine,
            script_location=_OTHER_APP_SCRIPT_LOCATION,
            version_table="alembic_version_other_app",
        )
        other_runner.upgrade(backup=False)

        assert loadcoach_runner.is_at_head()
        assert other_runner.is_at_head()

        loadcoach_tables = set(Base.metadata.tables)
        assert _OTHER_APP_TABLE not in loadcoach_tables

        with engine.connect() as connection:
            existing = {
                row[0]
                for row in connection.execute(
                    text("SELECT name FROM sqlite_master WHERE type='table'")
                )
            }
        assert loadcoach_tables <= existing
        assert _OTHER_APP_TABLE in existing
        assert "alembic_version" in existing
        assert "alembic_version_other_app" in existing

        # check_parity is not run here: it diffs the *entire* live schema against the given
        # metadata, and this test deliberately shares one physical file between two schemas —
        # something database standards §1 forbids in any real deployment ("one database per
        # application"). The properties that matter — both histories reach their own head under
        # their own version_table with no collision, and neither's tables appear in the other's
        # metadata — are asserted above.


def test_failed_migration_restores_backup_on_sqlite(tmp_path: Path) -> None:
    """A deliberately failing revision on top of 0001: backup restored, both outcomes reported."""
    broken_dir = tmp_path / "broken_migrations"
    shutil.copytree(MIGRATIONS_LOCATION, broken_dir)
    (broken_dir / "versions" / "0002_broken.py").write_text(_BROKEN_REVISION)

    with temporary_sqlite() as engine:
        runner = MigrationRunner(engine, script_location=str(broken_dir))
        runner.upgrade("0001", backup=False)

        with engine.connect() as connection:
            connection.execute(
                text("INSERT INTO settings (key, updated_at) VALUES ('k', :updated_at)"),
                {"updated_at": "2026-08-29T00:00:00"},
            )
            connection.commit()

        with pytest.raises(MigrationFailed) as excinfo:
            runner.upgrade("0002")
        assert excinfo.value.details["restored"] is True
        assert runner.current() == "0001"

        with engine.connect() as connection:
            rows = connection.execute(text("SELECT key FROM settings")).fetchall()
        assert [row[0] for row in rows] == ["k"]
