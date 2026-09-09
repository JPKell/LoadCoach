"""The ADR-0127 settings-schema document (`config schema`) is stable and complete."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, cast

import pytest

from loadcoach.config import Settings
from loadcoach.services.settings import CONFIG_ONLY_SECURITY_KEYS, RUNTIME_SETTINGS, SCHEMA_VERSION

GOLDEN = Path(__file__).resolve().parents[1] / "fixtures" / "config" / "config_schema.json"


_NOT_A_SETTINGS_FIELD = frozenset({"queue.paused", "queue.draining"})
"""Runtime-changeable keys with no ``Settings`` field: the P5 control flags live only in the
``settings`` table (services/settings.py's ``RUNTIME_SETTINGS`` docstring; config_reference.py's
generated header carries the same exception for `docs/configuration.md`)."""


def _resolve_field_schema(json_schema: dict[str, Any], path: str) -> dict[str, Any]:
    """Look up ``path``'s field schema inside ``Settings.model_json_schema()``'s ``$defs``."""
    section, _, field_name = path.partition(".")
    def_name = Settings.model_fields[section].annotation.__name__  # type: ignore[union-attr]
    return cast("dict[str, Any]", json_schema["$defs"][def_name]["properties"][field_name])


def test_every_runtime_changeable_and_security_key_exists_in_json_schema() -> None:
    json_schema = Settings.model_json_schema()
    for key in RUNTIME_SETTINGS:
        if key in _NOT_A_SETTINGS_FIELD:
            continue
        assert _resolve_field_schema(json_schema, key), key
    for key in CONFIG_ONLY_SECURITY_KEYS:
        assert _resolve_field_schema(json_schema, key), key


@pytest.fixture
def _isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A clean environment and an absent config/database, so the document is deterministic."""
    for name in list(os.environ):
        if name.startswith("LOADCOACH_"):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(
        "LOADCOACH_STORAGE__DATABASE_URL", f"sqlite:///{tmp_path / 'absent.sqlite3'}"
    )
    return tmp_path / "absent-config.toml"


def test_the_committed_schema_matches_the_model(_isolated: Path) -> None:
    from loadcoach import __about__
    from loadcoach.services.settings import config_schema_document

    document = config_schema_document(_isolated)
    assert document["version"] == __about__.__version__
    document["config_path"] = "<config_path>"  # machine-specific; asserted separately below
    document["version"] = "<version>"  # changes on every release; asserted separately above
    assert GOLDEN.is_file(), "tests/fixtures/config/config_schema.json is missing"
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    assert document == golden, (
        "the settings-schema document drifted from the settings model; regenerate "
        "tests/fixtures/config/config_schema.json from loadcoach.services.settings."
        "config_schema_document()"
    )
    assert document["schema_version"] == SCHEMA_VERSION


def test_config_path_reflects_the_resolved_file(_isolated: Path) -> None:
    from loadcoach.services.settings import config_schema_document

    document = config_schema_document(_isolated)
    assert document["config_path"] == str(_isolated)


def test_config_only_excludes_runtime_security_and_derived_keys(_isolated: Path) -> None:
    from loadcoach.services.settings import config_schema_document

    document = config_schema_document(_isolated)
    config_only = set(document["config_only"])
    runtime_keys = {entry["key"] for entry in document["runtime_changeable"]}
    assert not config_only & runtime_keys
    assert not config_only & set(document["security_keys"])
    assert "providers.registrations" not in config_only
    assert "providers.registrations" not in runtime_keys
    assert "providers.registrations" not in document["security_keys"]


def test_provider_form_reports_plural_when_a_named_registration_is_configured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in list(os.environ):
        if name.startswith("LOADCOACH_"):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("LOADCOACH_STORAGE__DATABASE_URL", f"sqlite:///{tmp_path / 'a.sqlite3'}")
    config_file = tmp_path / "config.toml"
    config_file.write_text('[providers.local]\nkind = "ollama"\n')

    from loadcoach.services.settings import config_schema_document

    assert config_schema_document(config_file)["provider_form"] == "plural"


def test_an_unknown_key_is_reported_as_a_problem_not_a_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in list(os.environ):
        if name.startswith("LOADCOACH_"):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("LOADCOACH_STORAGE__DATABASE_URL", f"sqlite:///{tmp_path / 'a.sqlite3'}")
    config_file = tmp_path / "config.toml"
    config_file.write_text('[server]\nhost_typo = "x"\n')

    from loadcoach.services.settings import config_schema_document

    document = config_schema_document(config_file)
    assert any("server.host_typo" in problem for problem in document["problems"])
