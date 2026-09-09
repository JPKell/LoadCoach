"""Editing ``[providers.<name>]`` in place (ADR-0117).

The property that matters is not "the value round-trips" — it is that everything the operator
wrote and this edit did not touch comes back byte for byte.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from baseaicore import SuiteError, ValidationError

from loadcoach.config import load_settings
from loadcoach.services.providers import (
    ProviderConfigChanged,
    config_digest,
    delete_registration,
    describe_registrations,
    save_registration,
)

_FILE = """\
# the deployment note nobody wants to lose
[server]
port = 8770  # deliberately not the default

# the box under the desk
[provider]
kind = "ollama"
base_url = "http://127.0.0.1:11434"
"""


@pytest.fixture
def config_path(tmp_path: Path) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(_FILE, encoding="utf-8")
    return path


def test_a_write_keeps_every_comment_and_every_untouched_key(config_path: Path) -> None:
    save_registration(
        config_path, "workstation", {"kind": "ollama", "base_url": "http://10.0.0.5:11434"}
    )
    text = config_path.read_text(encoding="utf-8")
    assert "# the deployment note nobody wants to lose" in text
    assert "port = 8770  # deliberately not the default" in text
    assert "# the box under the desk" in text
    settings = load_settings(config_path=config_path).settings
    assert settings.server.port == 8770


def test_the_singular_block_becomes_the_registration_it_already_was(config_path: Path) -> None:
    """ADR-0077 rule 3 refuses both forms at once, so the singular one is moved, not duplicated."""
    save_registration(
        config_path, "workstation", {"kind": "ollama", "base_url": "http://10.0.0.5:11434"}
    )
    settings = load_settings(config_path=config_path).settings
    assert sorted(settings.providers.registrations) == ["ollama", "workstation"]
    assert "[provider]\n" not in config_path.read_text(encoding="utf-8")


def test_the_previous_file_is_kept_beside_the_new_one(config_path: Path) -> None:
    save_registration(config_path, "workstation", {"kind": "ollama"})
    assert config_path.with_name("config.toml.bak").read_text(encoding="utf-8") == _FILE


def test_a_document_the_application_would_refuse_never_lands(config_path: Path) -> None:
    with pytest.raises(SuiteError) as caught:
        save_registration(config_path, "broken", {"kind": "ollama", "timeout_seconds": -1.0})
    assert "timeout_seconds" in str(caught.value)
    assert config_path.read_text(encoding="utf-8") == _FILE


def test_a_key_outside_the_registration_is_refused_by_name(config_path: Path) -> None:
    with pytest.raises(ValidationError) as caught:
        save_registration(config_path, "workstation", {"allow_remote": True})
    assert "allow_remote" in str(caught.value)
    assert config_path.read_text(encoding="utf-8") == _FILE


def test_a_write_against_a_stale_digest_is_refused(config_path: Path) -> None:
    stale = config_digest(config_path)
    config_path.write_text(_FILE + "\n# someone else was editing\n", encoding="utf-8")
    with pytest.raises(ProviderConfigChanged):
        save_registration(config_path, "workstation", {"kind": "ollama"}, base_digest=stale)
    assert "someone else was editing" in config_path.read_text(encoding="utf-8")


def test_the_last_registration_cannot_be_removed(config_path: Path) -> None:
    with pytest.raises(ValidationError):
        delete_registration(config_path, "ollama")
    settings = load_settings(config_path=config_path).settings
    assert describe_registrations(settings)[0].name == "ollama"


def test_removing_one_of_two_leaves_the_other(config_path: Path) -> None:
    save_registration(config_path, "workstation", {"kind": "ollama", "remote": True})
    delete_registration(config_path, "workstation")
    settings = load_settings(config_path=config_path).settings
    assert sorted(settings.providers.registrations) == ["ollama"]


def test_an_environment_variable_is_reported_as_shadowing(
    config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Configuration standards §7: the environment beats the file, so the page says so."""
    save_registration(config_path, "workstation", {"kind": "ollama"})
    monkeypatch.setenv("LOADCOACH_PROVIDERS__OLLAMA__BASE_URL", "http://elsewhere:11434")
    settings = load_settings(config_path=config_path).settings
    views = {view.name: view for view in describe_registrations(settings)}
    assert views["ollama"].shadowed_by == "LOADCOACH_PROVIDERS__OLLAMA__BASE_URL"
