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


def test_serve_bounds_uvicorn_s_graceful_shutdown(monkeypatch: pytest.MonkeyPatch) -> None:
    """Row WPF4: ``serve`` hands uvicorn a stop bound, so an open SSE stream cannot hold it open.

    Uvicorn's default is to wait for ever, and LoadCoach's streams never end on their own; the stop
    then outlives systemd's ``TimeoutStopSec`` and the unit is ``SIGKILL``ed. ``uvicorn.run`` itself
    is never called for real here — it blocks — so this asserts what ``serve`` hands it; the stop
    that results is measured over a real socket in ``tests/e2e/test_graceful_stop.py``.
    """
    from loadcoach.cli.commands.system import SHUTDOWN_GRACE_SECONDS

    passed: dict[str, object] = {}
    monkeypatch.setattr("uvicorn.run", lambda target, **kwargs: passed.update(kwargs))
    result = runner.invoke(app, ["serve", "--host", "127.0.0.1", "--port", "18771"])
    assert result.exit_code == 0, result.output
    assert passed["timeout_graceful_shutdown"] == SHUTDOWN_GRACE_SECONDS


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


def test_config_validate_file_exits_zero_for_a_valid_candidate(tmp_path: Path) -> None:
    """ADR-0127 rule 2: `--file` runs an arbitrary candidate through the ordinary validation."""
    candidate = tmp_path / "candidate.toml"
    candidate.write_text("[server]\nport = 9001\n")
    result = runner.invoke(app, ["config", "validate", "--file", str(candidate)])
    assert result.exit_code == 0


def test_config_validate_file_names_an_unknown_key(tmp_path: Path) -> None:
    candidate = tmp_path / "candidate.toml"
    candidate.write_text('[server]\nhost_typo = "x"\n')
    result = runner.invoke(app, ["config", "validate", "--file", str(candidate)])
    assert result.exit_code == 3
    assert "server.host_typo" in result.stderr


def test_config_validate_file_refuses_an_insecure_bind(tmp_path: Path) -> None:
    candidate = tmp_path / "candidate.toml"
    candidate.write_text('[server]\nhost = "0.0.0.0"\n')
    result = runner.invoke(app, ["config", "validate", "--file", str(candidate)])
    assert result.exit_code == 3
    assert "INSECURE_BINDING" in result.stderr


def test_config_validate_file_reports_a_missing_file_cleanly(tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist.toml"
    result = runner.invoke(app, ["config", "validate", "--file", str(missing)])
    assert result.exit_code == 3
    assert str(missing) in result.stderr


def test_config_validate_file_never_touches_the_installations_own_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_config = tmp_path / "config.toml"
    real_config.write_text("# operator's real config\n[server]\nport = 8766\n")
    monkeypatch.setenv("LOADCOACH_CONFIG", str(real_config))
    before = real_config.read_bytes()
    candidate = tmp_path / "candidate.toml"
    candidate.write_text("[server]\nport = 9999\n")
    result = runner.invoke(app, ["config", "validate", "--file", str(candidate)])
    assert result.exit_code == 0
    assert real_config.read_bytes() == before


def test_config_schema_json_flag_produces_the_document() -> None:
    import json

    result = runner.invoke(app, ["config", "schema", "--json"])
    assert result.exit_code == 0
    document = json.loads(result.stdout)
    assert document["application"] == "loadcoach"
    assert document["schema_version"] == "1.0"
    assert document["provider_form"] == "singular"
    assert {"key": "queue.paused", "kind": "bool"}.items() <= next(
        entry for entry in document["runtime_changeable"] if entry["key"] == "queue.paused"
    ).items()
    assert "server.host" in document["security_keys"]
    assert "storage.content_retention_hours" not in document["config_only"]


def test_config_schema_never_prints_a_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOADCOACH_EVIDENCE__FREEWEIGHT_API_KEY_ENV", "SOME_SECRET_VALUE_NAME")
    result = runner.invoke(app, ["config", "schema", "--json"])
    assert result.exit_code == 0
    assert "SOME_SECRET_VALUE_NAME" not in result.stdout


def test_config_schema_reports_drift_as_a_clean_error(tmp_path: Path) -> None:
    bad = tmp_path / "bad.toml"
    bad.write_text('[server]\nhost = "0.0.0.0"\n')
    result = runner.invoke(app, ["config", "schema", "--config", str(bad)])
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


def test_config_show_marks_a_database_sourced_value_and_prints_the_stored_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Configuration standards §7: a stored value is marked ``(database)``, not ``default``."""
    import json
    from datetime import UTC, datetime

    from weightsdb import MigrationRunner

    from loadcoach.config import load_settings
    from loadcoach.services.database import MIGRATIONS_LOCATION, Database
    from loadcoach.services.settings import write_runtime_settings

    database_path = tmp_path / "loadcoach.sqlite3"
    monkeypatch.setenv("LOADCOACH_STORAGE__DATABASE_URL", f"sqlite:///{database_path}")
    with Database.from_url(f"sqlite:///{database_path}") as database:
        MigrationRunner(database.engine, script_location=MIGRATIONS_LOCATION).upgrade(backup=False)
        write_runtime_settings(
            database,
            {"storage.content_retention_hours": 12},
            settings=load_settings().settings,
            now=datetime.now(UTC),
        )
    result = runner.invoke(app, ["config", "show"])
    assert result.exit_code == 0
    line = next(
        row
        for row in result.stdout.splitlines()
        if row.startswith("storage.content_retention_hours")
    )
    assert "12" in line and "(database)" in line
    payload = json.loads(runner.invoke(app, ["config", "show", "--json"]).stdout)
    assert payload["values"]["storage"]["content_retention_hours"] == 12
    assert payload["sources"]["storage.content_retention_hours"] == "database"


def test_config_show_reports_a_stored_row_the_environment_shadows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The row is named beside the variable that beats it, never printed as the value."""
    from datetime import UTC, datetime

    from weightsdb import MigrationRunner

    from loadcoach.config import load_settings
    from loadcoach.services.database import MIGRATIONS_LOCATION, Database
    from loadcoach.services.settings import write_runtime_settings

    database_path = tmp_path / "loadcoach.sqlite3"
    monkeypatch.setenv("LOADCOACH_STORAGE__DATABASE_URL", f"sqlite:///{database_path}")
    with Database.from_url(f"sqlite:///{database_path}") as database:
        MigrationRunner(database.engine, script_location=MIGRATIONS_LOCATION).upgrade(backup=False)
        write_runtime_settings(
            database,
            {"storage.content_retention_hours": 12},
            settings=load_settings().settings,
            now=datetime.now(UTC),
        )
    monkeypatch.setenv("LOADCOACH_STORAGE__CONTENT_RETENTION_HOURS", "72")
    result = runner.invoke(app, ["config", "show"])
    assert result.exit_code == 0
    line = next(
        row
        for row in result.stdout.splitlines()
        if row.startswith("storage.content_retention_hours")
    )
    assert " 72 " in line
    assert "env LOADCOACH_STORAGE__CONTENT_RETENTION_HOURS; database row 12 shadowed" in line


def test_config_show_prints_its_normal_output_when_there_is_no_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fresh install has no database yet, and inspecting configuration must not need one."""
    missing = tmp_path / "absent.sqlite3"
    monkeypatch.setenv("LOADCOACH_STORAGE__DATABASE_URL", f"sqlite:///{missing}")
    result = runner.invoke(app, ["config", "show"])
    assert result.exit_code == 0
    assert "(database)" not in result.stdout
    line = next(
        row
        for row in result.stdout.splitlines()
        if row.startswith("storage.content_retention_hours")
    )
    assert line.endswith("(default)") and " 24 " in line
    assert not missing.exists(), "an inspection command leaves no database behind"


def test_config_show_prints_its_normal_output_when_the_database_is_unmigrated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    unmigrated = tmp_path / "empty.sqlite3"
    unmigrated.touch()
    monkeypatch.setenv("LOADCOACH_STORAGE__DATABASE_URL", f"sqlite:///{unmigrated}")
    result = runner.invoke(app, ["config", "show"])
    assert result.exit_code == 0
    assert "(database)" not in result.stdout
    assert "storage.content_retention_hours" in result.stdout


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


def test_route_explain_discloses_that_breaker_state_was_unavailable() -> None:
    """F3 (M5C-3): a one-shot process has no breaker registry — the explanation says so.

    The CLI cannot see the serving process's circuit breakers, and silently passing an empty
    exclusion set would present "no breakers open" as a fact nobody checked. The flag is the
    disclosure, in the JSON payload and on the human flags line alike.
    """
    import json

    _prepared_database()
    result = runner.invoke(app, ["route", "explain", "--task", "general.chat", "--json"])
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert "breaker_state_unavailable" in payload["flags"]
    human = runner.invoke(app, ["route", "explain", "--task", "general.chat"])
    assert human.exit_code == 0, human.stdout
    assert "breaker_state_unavailable" in human.stdout


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
    assert len(profiles) == 21
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
    assert len(second) == 21


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


def test_job_submit_show_list_cancel_and_queue_controls_without_a_server() -> None:
    """The job and queue commands work against the database alone (mode: local)."""
    import json

    assert runner.invoke(app, ["db", "upgrade"]).exit_code == 0
    submitted = runner.invoke(
        app, ["job", "submit", "--task", "general.chat", "--prompt", "hello", "--json"]
    )
    assert submitted.exit_code == 0, submitted.output
    document = json.loads(submitted.stdout)
    job_id = document["job_id"]
    assert document["state"] == "queued" and document["source"] == "cli"

    shown = runner.invoke(app, ["job", "show", job_id])
    assert shown.exit_code == 0 and job_id in shown.stdout and "queued" in shown.stdout

    listed = runner.invoke(app, ["job", "list", "--json"])
    assert listed.exit_code == 0
    assert [item["job_id"] for item in json.loads(listed.stdout)] == [job_id]

    status = runner.invoke(app, ["queue", "status", "--json"])
    assert status.exit_code == 0
    report = json.loads(status.stdout)
    assert report["depth_by_state"] == {"queued": 1}
    assert report["flags"] == {"paused": False, "draining": False}
    assert report["executions"] is None  # only the serving process knows

    assert runner.invoke(app, ["queue", "pause"]).exit_code == 0
    assert json.loads(runner.invoke(app, ["queue", "status", "--json"]).stdout)["flags"]["paused"]
    assert runner.invoke(app, ["queue", "resume"]).exit_code == 0

    cancelled = runner.invoke(app, ["job", "cancel", job_id, "--json"])
    assert cancelled.exit_code == 0
    assert json.loads(cancelled.stdout)["state"] == "cancelled"
    assert runner.invoke(app, ["job", "cancel", job_id]).exit_code == 1
    assert runner.invoke(app, ["job", "show", "01NOPE0000000000000000000"]).exit_code == 5
    waited = runner.invoke(app, ["job", "wait", job_id, "--timeout", "1"])
    assert waited.exit_code == 1  # terminal, but cancelled rather than completed


def test_job_submit_refuses_a_priority_outside_the_band_and_an_unknown_task() -> None:
    runner.invoke(app, ["db", "upgrade"])
    bad = runner.invoke(
        app, ["job", "submit", "--task", "general.chat", "--prompt", "x", "--priority", "950"]
    )
    assert bad.exit_code == 2
    unknown = runner.invoke(app, ["job", "submit", "--task", "no.such", "--prompt", "x"])
    assert unknown.exit_code == 5
    neither = runner.invoke(app, ["job", "submit", "--task", "general.chat"])
    assert neither.exit_code == 2


def test_models_residency_is_empty_on_a_fresh_install() -> None:
    import json

    runner.invoke(app, ["db", "upgrade"])
    result = runner.invoke(app, ["models", "residency", "--json"])
    assert result.exit_code == 0
    assert json.loads(result.stdout) == []


def test_generate_routes_executes_and_prints_the_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """spec §7.2's ``loadcoach generate``, through the same executor as POST /generate."""
    import json

    from tests.integration.test_generate import NOW

    from loadcoach.config import ProviderSettings
    from loadcoach.infrastructure.providers.factory import build_provider
    from loadcoach.services.database import Database, ensure_ready
    from loadcoach.services.models import discover_models

    url = f"sqlite:///{tmp_path / 'gen.sqlite3'}"
    monkeypatch.setenv("LOADCOACH_STORAGE__DATABASE_URL", url)
    database = Database.from_url(url)
    ensure_ready(database, auto_migrate=True)
    # Discover with the same fake the command itself builds, so the two agree on the model.
    discover_models(database, build_provider(ProviderSettings(kind="fake")), now=NOW)
    database.close()
    result = runner.invoke(app, ["generate", "--task", "general.chat", "--prompt", "2+2?"])
    assert result.exit_code == 0, result.output
    text = result.stdout.strip()
    assert text  # the fake provider's default generation
    as_json = runner.invoke(app, ["generate", "--task", "general.chat", "--prompt", "x", "--json"])
    assert as_json.exit_code == 0, as_json.output
    document = json.loads(as_json.stdout)
    assert document["status"] == "completed" and document["routing"]["decision_id"]
    assert document["output"]["text"]  # the fake's default text differs per call
    # F3 (M5C-3): the CLI's one-shot process has no breaker registry, and the decision says so.
    assert "breaker_state_unavailable" in document["routing"]["flags"]
    prompt_file = tmp_path / "p.txt"
    prompt_file.write_text("from a file")
    streamed = runner.invoke(
        app, ["generate", "--task", "general.chat", "--prompt-file", str(prompt_file), "--stream"]
    )
    assert streamed.exit_code == 0, streamed.output
    assert streamed.stdout.strip()  # the deltas, printed as they arrived
    assert runner.invoke(app, ["generate", "--task", "general.chat"]).exit_code == 2
    assert runner.invoke(app, ["generate", "--task", "no.such", "--prompt", "x"]).exit_code == 5
