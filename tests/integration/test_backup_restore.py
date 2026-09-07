"""Backup/restore on both dialects (database standards §7, M9_AUDIT.md Group 3, item O4).

Until this file, LoadCoach's only backup/restore coverage was the CLI verb
(``tests/unit/test_cli.py::test_db_upgrade_then_status_then_backup``), and only on SQLite. This
uses ``weightsdb.testing``'s ``temporary_sqlite``/``temporary_postgres`` directly — the same
fixtures the ``db-matrix`` CI job and every other suite package already build their own backup
tests on — so a restore round-trips on SQLite and refuses (naming ``pg_restore``) on PostgreSQL,
which skips honestly when no server is configured locally.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from sqlalchemy import text
from weightsdb import DatabaseError, MigrationRunner
from weightsdb.backup import backup, restore
from weightsdb.testing import temporary_postgres, temporary_sqlite

from loadcoach.services.database import MIGRATIONS_LOCATION


@pytest.mark.parametrize("dialect", ["sqlite", "postgresql"])
def test_backup_round_trips_or_refuses_restore_on_both_dialects(
    dialect: str, tmp_path: Path
) -> None:
    context = temporary_sqlite() if dialect == "sqlite" else temporary_postgres()
    with context as engine:
        MigrationRunner(engine, script_location=MIGRATIONS_LOCATION).upgrade(backup=False)
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO settings (key, value_json, updated_at) "
                    "VALUES ('dialect-row', '1', '2026-08-26 00:00:00')"
                )
            )
        destination = tmp_path / ("backup.sqlite3" if dialect == "sqlite" else "backup.dump")
        if dialect == "postgresql" and shutil.which("pg_dump") is None:
            pytest.skip("pg_dump is not on PATH")
        result = backup(engine, destination)
        assert result.size_bytes > 0

        if dialect == "sqlite":
            with engine.begin() as connection:
                connection.execute(text("DELETE FROM settings WHERE key = 'dialect-row'"))
            restore(engine, destination, confirm=True)
            with engine.connect() as connection:
                assert (
                    connection.execute(
                        text("SELECT value_json FROM settings WHERE key = 'dialect-row'")
                    ).scalar_one()
                    == 1
                )
        else:
            with pytest.raises(DatabaseError, match="pg_restore"):
                restore(engine, destination, confirm=True)
