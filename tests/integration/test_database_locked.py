"""Degradation: SQLite database locked (graceful-degradation.md, row "Database locked (SQLite)").

Documented behaviour: "Retry with backoff to busy_timeout, then E." The retry-then-error is not
this application's own code — it is ``weightsdb.create_engine_for``'s ``BEGIN IMMEDIATE`` +
``busy_timeout`` machinery (spec §7), which turns SQLite contention into a typed
:class:`~weightsdb.StorageBusy` (``STORAGE_BUSY``) once the timeout is exceeded. This is a genuine
lock, not a simulated fault: a real second connection holds a real write transaction open, exactly
the way :func:`weightsdb.tests.unit.test_engine.test_sqlite_busy_timeout_raises_storage_busy`
proves the mechanism itself — this proves it reached through the application, at the boundary
``enqueue`` actually writes through.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import text
from weightsdb import MigrationRunner, StorageBusy, create_engine_for

from loadcoach.config import ExecutionSettings, ProviderSettings, Settings, StorageSettings
from loadcoach.services.database import MIGRATIONS_LOCATION, Database
from loadcoach.services.job_events import JobEventSink
from loadcoach.services.queue import JobSubmission, enqueue, list_jobs
from loadcoach.services.task_profiles import import_task_profiles, read_task_profiles_file

NOW = datetime(2026, 8, 29, 12, 0, 0, tzinfo=UTC)


def test_a_locked_database_refuses_a_new_job_and_the_queue_is_left_untouched(
    tmp_path: Path,
) -> None:
    url = f"sqlite:///{tmp_path / 'locked.sqlite3'}"
    settings = Settings(
        storage=StorageSettings(database_url=url),
        provider=ProviderSettings(kind="fake"),
        execution=ExecutionSettings(),
    )

    # Ready the schema and its one task profile before anyone holds a lock — both are writes.
    setup_engine = create_engine_for(url)
    MigrationRunner(setup_engine, script_location=MIGRATIONS_LOCATION).upgrade(backup=False)
    setup_database = Database(setup_engine)
    import_task_profiles(setup_database, read_task_profiles_file(), now=NOW)
    setup_engine.dispose()

    # A second connection over the same file, holding a real write transaction open — the
    # locking engine's own machinery, not a fault this test injects.
    holder_engine = create_engine_for(url)
    holder = holder_engine.connect()
    holder.execute(text("CREATE TABLE _lock_holder (id INTEGER PRIMARY KEY)"))
    holder.execute(text("INSERT INTO _lock_holder (id) VALUES (1)"))  # opens BEGIN IMMEDIATE

    # A short busy_timeout so the test proves the same property WeightsDB's own unit test does,
    # without waiting out the application's real (much longer) default.
    contender_engine = create_engine_for(url, sqlite_busy_timeout_ms=100)
    database = Database(contender_engine)
    try:
        with pytest.raises(StorageBusy) as excinfo:
            enqueue(
                database,
                JobSubmission(task="general.chat", prompt="never gets in"),
                now=datetime.now(UTC),
                queue_settings=settings.queue,
                execution_settings=settings.execution,
                sink=JobEventSink(),
            )
        assert excinfo.value.details["busy_timeout_ms"] == 100
    finally:
        holder.rollback()
        holder.close()
        holder_engine.dispose()
        contender_engine.dispose()

    # The queue is exactly as it was before the contended attempt — nothing half-written.
    readback_engine = create_engine_for(url)
    assert list_jobs(Database(readback_engine)) == ()
    readback_engine.dispose()
