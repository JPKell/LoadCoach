"""Tests for the CLI skeleton: exit codes across system, config and db commands."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from loadcoach.cli.main import app

runner = CliRunner()


@pytest.fixture(autouse=True)
def _fake_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOADCOACH_PROVIDER__KIND", "fake")


def test_health_exits_zero_when_ok_or_degraded() -> None:
    result = runner.invoke(app, ["health"])
    assert result.exit_code == 0
    assert "status:" in result.stdout


def test_health_json_flag_produces_valid_json() -> None:
    import json

    result = runner.invoke(app, ["health", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert "status" in payload
    assert "components" in payload


def test_version_prints_application_name() -> None:
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert "loadcoach" in result.stdout


def test_version_flag_on_root_command() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert "loadcoach" in result.stdout


def test_doctor_runs_and_exits_zero() -> None:
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0


def test_config_validate_exits_zero_for_default_config() -> None:
    result = runner.invoke(app, ["config", "validate"])
    assert result.exit_code == 0


def test_config_validate_exits_three_for_invalid_config(tmp_path: Path) -> None:
    config_file = tmp_path / "bad.toml"
    config_file.write_text('[server]\nhost = "0.0.0.0"\n')  # unacknowledged LAN exposure
    result = runner.invoke(app, ["config", "validate", "--config", str(config_file)])
    assert result.exit_code == 3
    assert "INSECURE_BINDING" in result.stderr


def test_config_show_lists_effective_values() -> None:
    result = runner.invoke(app, ["config", "show"])
    assert result.exit_code == 0
    assert "server.host" in result.stdout


def test_config_show_redacts_secret_looking_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOADCOACH_EVIDENCE__FREEWEIGHT_API_KEY_ENV", "SOME_SECRET_VALUE_NAME")
    result = runner.invoke(app, ["config", "show"])
    assert result.exit_code == 0
    assert "SOME_SECRET_VALUE_NAME" not in result.stdout
    assert "********" in result.stdout


def test_config_path_prints_a_path() -> None:
    result = runner.invoke(app, ["config", "path"])
    assert result.exit_code == 0
    assert result.stdout.strip().endswith("config.toml")


def test_config_init_writes_a_file(tmp_path: Path) -> None:
    target = tmp_path / "new-config.toml"
    result = runner.invoke(app, ["config", "init", "--config", str(target)])
    assert result.exit_code == 0
    assert target.is_file()
    assert "[server]" in target.read_text()


def test_config_init_refuses_to_overwrite_without_force(tmp_path: Path) -> None:
    target = tmp_path / "existing.toml"
    target.write_text("# already here\n")
    result = runner.invoke(app, ["config", "init", "--config", str(target)])
    assert result.exit_code == 3
    assert target.read_text() == "# already here\n"


def test_db_upgrade_then_status_then_backup() -> None:
    upgrade_result = runner.invoke(app, ["db", "upgrade"])
    assert upgrade_result.exit_code == 0

    status_result = runner.invoke(app, ["db", "status", "--json"])
    assert status_result.exit_code == 0
    import json

    payload = json.loads(status_result.stdout)
    assert payload["is_at_head"] is True

    backup_result = runner.invoke(app, ["db", "backup"])
    assert backup_result.exit_code == 0
    assert Path(backup_result.stdout.strip()).is_file()


def test_db_restore_requires_yes_flag(tmp_path: Path) -> None:
    fake_backup = tmp_path / "backup.sqlite3"
    fake_backup.write_bytes(b"not a real backup")
    result = runner.invoke(app, ["db", "restore", str(fake_backup)])
    assert result.exit_code == 2
    assert "--yes" in result.stderr


def test_db_upgrade_is_idempotent_no_op_on_second_call() -> None:
    runner.invoke(app, ["db", "upgrade"])
    second = runner.invoke(app, ["db", "upgrade"])
    assert second.exit_code == 0
    assert "(empty)" not in second.stdout


def _prepared_database() -> None:
    """Migrate and discover, so ``route explain`` has a registry to route over."""
    from datetime import UTC, datetime

    from modelrack.testing import FakeProvider

    from loadcoach.config import load_settings
    from loadcoach.services.database import Database, ensure_ready
    from loadcoach.services.models import discover_models

    url = load_settings().settings.storage.database_url
    assert url is not None
    with Database.from_url(url) as database:
        ensure_ready(database, auto_migrate=True)
        discover_models(database, FakeProvider(), now=datetime.now(UTC))


def test_route_explain_prints_the_resolved_subject() -> None:
    """dev-plan P3 Work item: `loadcoach route explain`, and acceptance criterion 1a."""
    _prepared_database()
    result = runner.invoke(app, ["route", "explain", "--task", "general.chat"])
    assert result.exit_code == 0, result.stdout
    assert "runtime_profile_hash" in result.stdout
    assert "served_context" in result.stdout
    assert "evidence  none" in result.stdout


def test_route_explain_json_flag_produces_the_whole_explanation() -> None:
    import json

    _prepared_database()
    result = runner.invoke(app, ["route", "explain", "--task", "general.chat", "--json"])
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["selected"]["runtime_profile_hash"]
    assert payload["selected"]["served_context_source"] in {"configured", "reported", "assumed"}


def test_route_explain_exits_five_for_an_unknown_task() -> None:
    _prepared_database()
    result = runner.invoke(app, ["route", "explain", "--task", "no.such.task"])
    assert result.exit_code == 5


def test_route_explain_exits_four_and_lists_every_rejection() -> None:
    """`NO_ELIGIBLE_MODEL` is useless without the candidates and their reasons."""
    _prepared_database()
    result = runner.invoke(
        app, ["route", "explain", "--task", "general.chat", "--input-tokens", "10000000"]
    )
    assert result.exit_code == 4
    assert "NO_ELIGIBLE_MODEL" in result.output
    assert "context_limit_exceeded" in result.output


def test_tasks_list_shows_shipped_profiles_on_a_fresh_install_without_serve() -> None:
    """Regression for the LC14 gap: ``tasks list``/``tasks show`` must not depend on ``serve``.

    Task profile import lives in ``bootstrap()``, which only ``loadcoach serve`` calls. The
    standalone ``tasks`` commands open a database handle directly, so before this was fixed a
    fresh install that ran ``db upgrade`` and then ``tasks list`` saw an empty list — for data
    that ships in the repository. Nothing in this test starts the server.
    """
    import json

    assert runner.invoke(app, ["db", "upgrade"]).exit_code == 0

    listed = runner.invoke(app, ["tasks", "list", "--json"])
    assert listed.exit_code == 0
    profiles = json.loads(listed.stdout)
    assert len(profiles) == 15
    assert "general.chat" in {profile["profile_id"] for profile in profiles}

    shown = runner.invoke(app, ["tasks", "show", "general.chat"])
    assert shown.exit_code == 0
    assert json.loads(shown.stdout)["profile_id"] == "general.chat"


def test_tasks_list_import_is_idempotent_across_repeated_invocations() -> None:
    """The import ``tasks list`` performs is an upsert, so reading twice must not duplicate."""
    import json

    runner.invoke(app, ["db", "upgrade"])
    first = json.loads(runner.invoke(app, ["tasks", "list", "--json"]).stdout)
    second = json.loads(runner.invoke(app, ["tasks", "list", "--json"]).stdout)
    assert first == second
    assert len(second) == 15


def test_models_list_is_empty_on_a_fresh_install_and_that_is_honest() -> None:
    """The deliberate asymmetry with ``tasks list``: an empty registry is a true statement.

    No provider has been asked yet, so there is nothing to report — unlike the shipped task
    profiles, which exist in the repository whether or not anything has run.
    """
    import json

    assert runner.invoke(app, ["db", "upgrade"]).exit_code == 0
    listed = runner.invoke(app, ["models", "list", "--json"])
    assert listed.exit_code == 0
    assert json.loads(listed.stdout) == []
