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
