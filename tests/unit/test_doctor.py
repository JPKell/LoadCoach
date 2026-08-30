"""``loadcoach doctor`` diagnoses every documented failure mode by name (dev-plan P9)."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

from loadcoach.cli.main import app
from loadcoach.services.doctor import DOCUMENTED_FAILURE_MODES, diagnose

runner = CliRunner()
SPEC = Path(__file__).resolve().parents[2] / "docs" / "apps" / "loadcoach" / "spec.md"


def _codes(findings: list[dict[str, object]]) -> dict[str, str]:
    return {str(f["code"]): str(f["verdict"]) for f in findings}


def test_every_documented_code_the_doctor_claims_is_in_spec_13_or_the_degradation_contract() -> (
    None
):
    section = SPEC.read_text().split("## 13. Error behaviour")[1].split("## 14.")[0]
    documented = set(re.findall(r"\b[A-Z][A-Z_]{4,}\b", section))
    components = set(
        re.findall(r"`([a-z_]+)`", SPEC.read_text().split("## 17.")[1].split("## 18.")[0])
    )
    for code in DOCUMENTED_FAILURE_MODES:
        if code.startswith("degraded:"):
            assert code.removeprefix("degraded:") in components, code
        elif code in (
            "CONFIGURATION_ERROR",
            "INSECURE_BINDING",
            "DATABASE_UNAVAILABLE",
            "MIGRATION_REQUIRED",
            "SCHEMA_AHEAD",
            "STORAGE_FULL",
            "STORAGE_BUSY",
            "RATE_LIMITED",
        ):
            pass  # startup and storage codes documented in configuration/database standards
        else:
            assert code in documented, code


def test_a_healthy_zero_configuration_install_diagnoses_without_a_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datetime import UTC, datetime

    from loadcoach.services.database import Database, ensure_ready
    from loadcoach.services.task_profiles import import_task_profiles, read_task_profiles_file

    url = f"sqlite:///{tmp_path / 'ok.sqlite3'}"
    monkeypatch.setenv("LOADCOACH_PROVIDER__KIND", "fake")
    monkeypatch.setenv("LOADCOACH_STORAGE__DATABASE_URL", url)
    handle = Database.from_url(url)
    ensure_ready(handle, auto_migrate=True)
    # Migrated but never served: the one failure is the missing task profiles, named.
    bare = diagnose()
    assert bare.status == "fail"
    assert _codes(bare.as_json()["findings"])["TASK_PROFILE_NOT_FOUND"] == "fail"
    import_task_profiles(handle, read_task_profiles_file(), now=datetime.now(UTC))
    handle.close()
    diagnosis = diagnose()
    codes = _codes(diagnosis.as_json()["findings"])
    assert diagnosis.status != "fail", codes
    assert codes["CONFIGURATION_ERROR"] == "ok" and codes["MIGRATION_REQUIRED"] == "ok"
    assert codes["INSECURE_BINDING"] == "ok" and codes["TASK_PROFILE_NOT_FOUND"] == "ok"
    assert codes["MODEL_NOT_FOUND"] == "warn"  # the fake provider has not been discovered yet
    assert set(codes) <= set(DOCUMENTED_FAILURE_MODES)
    missing = set(DOCUMENTED_FAILURE_MODES) - set(codes)
    assert missing <= {"SCHEMA_AHEAD", "DATABASE_UNAVAILABLE"}, missing  # only one of each pair


def test_an_unreachable_database_names_database_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LOADCOACH_PROVIDER__KIND", "fake")
    monkeypatch.setenv("LOADCOACH_STORAGE__DATABASE_URL", "sqlite:////proc/nowhere/x.sqlite3")
    diagnosis = diagnose()
    codes = _codes(diagnosis.as_json()["findings"])
    assert diagnosis.status == "fail" and codes["DATABASE_UNAVAILABLE"] == "fail"
    finding = next(f for f in diagnosis.findings if f.code == "DATABASE_UNAVAILABLE")
    assert finding.remedy and "storage.database_url" in finding.remedy


def test_an_unmigrated_database_and_a_paused_queue_are_named(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    url = f"sqlite:///{tmp_path / 'empty.sqlite3'}"
    monkeypatch.setenv("LOADCOACH_PROVIDER__KIND", "fake")
    monkeypatch.setenv("LOADCOACH_STORAGE__DATABASE_URL", url)
    codes = _codes(diagnose().as_json()["findings"])
    assert codes["MIGRATION_REQUIRED"] == "warn"  # auto_migrate is on for SQLite
    monkeypatch.setenv("LOADCOACH_STORAGE__AUTO_MIGRATE", "false")
    strict = diagnose()
    assert strict.status == "fail"
    assert next(f for f in strict.findings if f.code == "MIGRATION_REQUIRED").remedy
    monkeypatch.delenv("LOADCOACH_STORAGE__AUTO_MIGRATE")

    from datetime import UTC, datetime

    from loadcoach.services.database import Database, ensure_ready
    from loadcoach.services.queue import set_queue_flag

    handle = Database.from_url(url)
    ensure_ready(handle, auto_migrate=True)
    set_queue_flag(handle, "queue.paused", True, now=datetime.now(UTC))
    handle.close()
    paused = diagnose()
    queue = next(f for f in paused.findings if f.code == "degraded:queue")
    assert queue.verdict == "warn" and "paused" in queue.detail
    assert queue.remedy == "`loadcoach queue resume`"


def test_a_non_loopback_bind_without_tokens_is_insecure_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LOADCOACH_PROVIDER__KIND", "fake")
    monkeypatch.setenv("LOADCOACH_STORAGE__DATABASE_URL", f"sqlite:///{tmp_path / 'lan.sqlite3'}")
    monkeypatch.setenv("LOADCOACH_SERVER__HOST", "192.0.2.10")
    monkeypatch.setenv("LOADCOACH_SERVER__ALLOWED_HOSTS", "coach.test")
    from loadcoach.services.database import Database, ensure_ready

    handle = Database.from_url(f"sqlite:///{tmp_path / 'lan.sqlite3'}")
    ensure_ready(handle, auto_migrate=True)
    handle.close()
    diagnosis = diagnose()
    binding = next(f for f in diagnosis.findings if f.code == "INSECURE_BINDING")
    assert (
        binding.verdict == "fail" and binding.remedy and "loadcoach token create" in binding.remedy
    )
    assert diagnosis.status == "fail"
    monkeypatch.delenv("LOADCOACH_SERVER__ALLOWED_HOSTS")
    without_hosts = diagnose()
    assert without_hosts.findings[0].code == "INSECURE_BINDING"
    assert (
        without_hosts.findings[0].verdict == "fail"
        and "allowed_hosts" in without_hosts.findings[0].detail
    )


def test_the_cli_prints_every_finding_and_exits_four_on_a_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from loadcoach.services.database import Database, ensure_ready

    url = f"sqlite:///{tmp_path / 'cli.sqlite3'}"
    monkeypatch.setenv("LOADCOACH_PROVIDER__KIND", "fake")
    monkeypatch.setenv("LOADCOACH_STORAGE__DATABASE_URL", url)
    unmigrated = runner.invoke(app, ["doctor"])
    assert unmigrated.exit_code == 0 and "! MIGRATION_REQUIRED" in unmigrated.stdout
    handle = Database.from_url(url)
    ensure_ready(handle, auto_migrate=True)
    handle.close()
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 4, result.output  # no task profile imported yet
    assert "✗ TASK_PROFILE_NOT_FOUND" in result.stdout and "→" in result.stdout
    as_json = runner.invoke(app, ["doctor", "--json"])
    document = json.loads(as_json.stdout)
    assert document["status"] == "fail"
    assert {f["code"] for f in document["findings"]} >= {
        "CONFIGURATION_ERROR",
        "MIGRATION_REQUIRED",
    }
