"""The runtime-changeable registry and which layer wins.

Two properties, and the second is the one a reviewer should read first:

* A stored row this build cannot read falls back to configuration instead of raising: a row
  written by another version must not stop this one from serving.
* **The environment beats a stored row** (configuration standards §7, ADR-0100), for every key
  in the registry including the two control flags, and a row that does nothing is reported as
  shadowed rather than dropped or applied.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from weightsdb import MigrationRunner, upsert
from weightsdb.testing import temporary_sqlite

from loadcoach.config import ConfigurationError, Settings, env_var_for, load_settings
from loadcoach.infrastructure.db.models import Setting
from loadcoach.services.database import MIGRATIONS_LOCATION, Database
from loadcoach.services.settings import (
    RUNTIME_SETTINGS,
    read_runtime_settings,
    runtime_settings_document,
    shadowing_source,
    write_runtime_settings,
)

_NOW = datetime(2026, 9, 7, 12, 0, tzinfo=UTC)
_ADMIN = None  # `authorize(None, …)` is the un-authenticated path services take in tests


@pytest.fixture
def database() -> Iterator[Database]:
    with temporary_sqlite() as engine:
        MigrationRunner(engine, script_location=MIGRATIONS_LOCATION).upgrade(backup=False)
        yield Database(engine)


@pytest.fixture
def settings() -> Settings:
    return Settings()


def _store(database: Database, key: str, value: object) -> None:
    """Write a row directly, bypassing validation — what another version might have left."""
    with database.write() as session:
        upsert(
            session,
            Setting,
            values={"key": key, "value_json": value, "updated_at": _NOW},
            index_elements=["key"],
        )


def test_shadowing_source_names_the_variable_or_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    assert shadowing_source("storage.content_retention_hours") is None
    monkeypatch.setenv("LOADCOACH_STORAGE__CONTENT_RETENTION_HOURS", "48")
    assert shadowing_source("storage.content_retention_hours") == (
        "env LOADCOACH_STORAGE__CONTENT_RETENTION_HOURS"
    )


def test_a_stored_row_is_effective_and_reported_as_the_database(
    database: Database, settings: Settings
) -> None:
    write_runtime_settings(
        database,
        {"storage.content_retention_hours": 12},
        settings=settings,
        now=_NOW,
        principal=_ADMIN,
    )
    document = runtime_settings_document(database, settings=settings)
    definition = document["definitions"]["storage.content_retention_hours"]
    assert document["settings"]["storage.content_retention_hours"] == 12
    assert definition["source"] == "database"
    assert definition["stored"] == 12
    assert definition["configured"] == settings.storage.content_retention_hours
    assert definition["shadowed_by"] is None


def test_a_key_with_no_row_reports_configuration_and_no_stored_value(
    database: Database, settings: Settings
) -> None:
    definition = runtime_settings_document(database, settings=settings)["definitions"][
        "routing.min_confidence"
    ]
    assert definition["source"] == "configuration"
    assert definition["stored"] is None
    assert definition["shadowed_by"] is None


def test_the_environment_beats_a_stored_row_and_the_row_is_reported_as_shadowed(
    database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(env_var_for("storage.content_retention_hours"), "72")
    configured = load_settings().settings
    assert configured.storage.content_retention_hours == 72
    effective = write_runtime_settings(
        database,
        {"storage.content_retention_hours": 12},
        settings=configured,
        now=_NOW,
        principal=_ADMIN,
    )
    assert effective["storage.content_retention_hours"] == 72, (
        "configuration standards §7 precedence"
    )
    definition = runtime_settings_document(database, settings=configured)["definitions"][
        "storage.content_retention_hours"
    ]
    assert definition["stored"] == 12, "the row is kept and shown, not discarded"
    assert definition["source"] == "configuration"
    assert definition["shadowed_by"] == "env LOADCOACH_STORAGE__CONTENT_RETENTION_HOURS"


def test_the_stored_row_wins_again_once_the_environment_stops_pinning_it(
    database: Database, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(env_var_for("storage.content_retention_hours"), "72")
    write_runtime_settings(
        database,
        {"storage.content_retention_hours": 12},
        settings=settings,
        now=_NOW,
        principal=_ADMIN,
    )
    monkeypatch.delenv(env_var_for("storage.content_retention_hours"))
    effective = read_runtime_settings(database, settings=settings)
    assert effective["storage.content_retention_hours"] == 12


@pytest.mark.parametrize("flag", ["queue.paused", "queue.draining"])
def test_the_control_flags_take_the_same_path_and_have_no_configuration_layer(
    database: Database, settings: Settings, flag: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D1: one rule for every key — and for these two there is nothing to shadow them.

    ``queue.paused`` and ``queue.draining`` are not fields of :class:`~loadcoach.config.Settings`
    (their configured value is the ``False`` :func:`_configured` falls back to), so a variable
    naming one is refused by the loader before the process starts rather than quietly holding a
    console pause. The stored row is therefore always the effective value, through the same code
    path as every other key.
    """
    write_runtime_settings(database, {flag: True}, settings=settings, now=_NOW, principal=_ADMIN)
    definition = runtime_settings_document(database, settings=settings)["definitions"][flag]
    assert definition["stored"] is True
    assert definition["source"] == "database"
    assert definition["shadowed_by"] is None
    monkeypatch.setenv(env_var_for(flag), "true")
    with pytest.raises(ConfigurationError, match=flag.split(".", 1)[1]):
        load_settings()


@pytest.mark.parametrize("stored", ["nonsense", -1, 2.5, None])
def test_a_row_this_build_cannot_read_falls_back_to_configuration(
    database: Database, settings: Settings, stored: object
) -> None:
    _store(database, "routing.min_confidence", stored)
    effective = read_runtime_settings(database, settings=settings)
    assert effective["routing.min_confidence"] == settings.routing.min_confidence


def test_an_unreadable_row_is_still_reported_as_stored(
    database: Database, settings: Settings
) -> None:
    _store(database, "routing.min_confidence", "nonsense")
    definition = runtime_settings_document(database, settings=settings)["definitions"][
        "routing.min_confidence"
    ]
    assert definition["stored"] == "nonsense"


def test_no_source_module_passes_cli_overrides() -> None:
    """The environment check in ``shadowing_source`` covers the CLI layer only while there is none.

    ``load_settings`` accepts ``cli_overrides`` and nothing under ``src/`` passes it; the serving
    process receives CLI flags as environment variables (``loadcoach serve``). The day that
    changes, this test fails — instead of the precedence silently mis-ordering.
    """
    offenders = [
        str(path)
        for path in Path("src/loadcoach").rglob("*.py")
        if "cli_overrides" in path.read_text(encoding="utf-8")
        and path.name != "config.py"  # where the parameter is defined
    ]
    assert offenders == [], "shadowing_source must also consult the CLI layer"


def test_every_registry_key_has_an_environment_variable() -> None:
    for key in RUNTIME_SETTINGS:
        assert env_var_for(key).startswith("LOADCOACH_")
