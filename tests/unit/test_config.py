"""Tests for loadcoach.config: precedence chain and the unsafe-binding refusal set."""

from __future__ import annotations

from pathlib import Path

import pytest

from loadcoach.config import InsecureBindingError, load_settings


def test_zero_configuration_defaults_validate_cleanly(tmp_path: Path) -> None:
    """A fresh install with no config file at all is fully functional (spec §20 AC1)."""
    loaded = load_settings(config_path=tmp_path / "does-not-exist.toml")
    assert loaded.config_file_used is False
    assert loaded.settings.server.host == "127.0.0.1"
    assert loaded.settings.server.port == 8766
    assert loaded.settings.storage.database_url is not None
    assert loaded.settings.storage.database_url.startswith("sqlite:///")


def test_precedence_file_then_env_then_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text('[server]\nport = 9001\n[logging]\nlevel = "DEBUG"\n')

    # File alone.
    loaded = load_settings(config_path=config_file)
    assert loaded.settings.server.port == 9001
    assert loaded.settings.logging.level == "DEBUG"
    assert loaded.sources["server.port"] == "file"

    # Environment overrides the file for the field it sets, leaving siblings alone.
    monkeypatch.setenv("LOADCOACH_SERVER__PORT", "9002")
    loaded = load_settings(config_path=config_file)
    assert loaded.settings.server.port == 9002
    assert loaded.settings.logging.level == "DEBUG"  # untouched by the env override
    assert loaded.sources["server.port"].startswith("env")

    # CLI overrides both file and environment.
    loaded = load_settings(config_path=config_file, cli_overrides={"server": {"port": 9003}})
    assert loaded.settings.server.port == 9003
    assert loaded.sources["server.port"] == "cli"


def test_per_leaf_override_leaves_siblings_alone(tmp_path: Path) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text('[server]\nhost = "127.0.0.1"\nport = 9100\n')
    loaded = load_settings(
        config_path=config_file, cli_overrides={"server": {"allow_lan_exposure": True}}
    )
    assert loaded.settings.server.port == 9100  # from file, not clobbered
    assert loaded.settings.server.allow_lan_exposure is True  # from cli


def test_unknown_key_rejected_with_suggestion(tmp_path: Path) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text('[server]\nhosts = "127.0.0.1"\n')  # typo: hosts, not host
    from baseaicore import ConfigurationError

    with pytest.raises(ConfigurationError, match="hosts"):
        load_settings(config_path=config_file)


def test_invalid_toml_rejected(tmp_path: Path) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text("this is not [ valid toml")
    from baseaicore import ConfigurationError

    with pytest.raises(ConfigurationError, match="not valid TOML"):
        load_settings(config_path=config_file)


def test_lan_exposure_without_acknowledgement_refused(tmp_path: Path) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text('[server]\nhost = "0.0.0.0"\n')
    with pytest.raises(InsecureBindingError, match="allow_lan_exposure"):
        load_settings(config_path=config_file)


def test_lan_exposure_with_acknowledgement_alone_still_refused_without_allowed_hosts(
    tmp_path: Path,
) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text('[server]\nhost = "0.0.0.0"\nallow_lan_exposure = true\n')
    with pytest.raises(InsecureBindingError, match="allowed_hosts"):
        load_settings(config_path=config_file)


def test_non_loopback_named_host_requires_allowed_hosts(tmp_path: Path) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text('[server]\nhost = "loadcoach.example.com"\n')
    with pytest.raises(InsecureBindingError, match="allowed_hosts"):
        load_settings(config_path=config_file)


def test_full_unsafe_combination_acknowledged_passes_config_level_checks(tmp_path: Path) -> None:
    """Config-level checks alone are not the full refusal set — see bootstrap() for the token."""
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        '[server]\nhost = "0.0.0.0"\nallow_lan_exposure = true\n'
        'allowed_hosts = ["loadcoach.example.com"]\n'
    )
    loaded = load_settings(config_path=config_file)
    assert loaded.settings.server.allow_lan_exposure is True


def test_secret_looking_fields_are_not_special_cased_by_load_settings(tmp_path: Path) -> None:
    """Redaction is a CLI/display concern (config show); load_settings returns real values."""
    config_file = tmp_path / "config.toml"
    config_file.write_text('[evidence]\nfreeweight_api_key_env = "MY_TOKEN_VAR"\n')
    loaded = load_settings(config_path=config_file)
    assert loaded.settings.evidence.freeweight_api_key_env == "MY_TOKEN_VAR"


def test_queue_lease_margin_validated(tmp_path: Path) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text("[queue]\nlease_seconds = 10\nlease_renewal_interval_seconds = 20\n")
    from baseaicore import ConfigurationError

    with pytest.raises(ConfigurationError, match="lease_seconds"):
        load_settings(config_path=config_file)


def test_env_csv_split_for_tuple_fields(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("LOADCOACH_SERVER__ALLOWED_HOSTS", "a.example.com, b.example.com")
    monkeypatch.setenv("LOADCOACH_SERVER__HOST", "a.example.com")
    loaded = load_settings(config_path=tmp_path / "missing.toml")
    assert loaded.settings.server.allowed_hosts == ("a.example.com", "b.example.com")
