"""``docs/configuration.md`` is generated from the settings model and cannot drift (config §8)."""

from __future__ import annotations

import re
from pathlib import Path

from typer.testing import CliRunner

from loadcoach.cli.main import app
from loadcoach.config import Settings
from loadcoach.services.config_reference import render_configuration_reference
from loadcoach.services.settings import CONFIG_ONLY_SECURITY_KEYS, RUNTIME_SETTINGS

REFERENCE = Path(__file__).resolve().parents[2] / "docs" / "configuration.md"


def test_the_committed_reference_matches_the_model() -> None:
    assert REFERENCE.is_file(), "docs/configuration.md is missing; run `loadcoach config reference`"
    assert REFERENCE.read_text(encoding="utf-8") == render_configuration_reference(), (
        "docs/configuration.md drifted from the settings model: "
        "run `loadcoach config reference --output docs/configuration.md`"
    )


def test_every_field_appears_with_its_columns() -> None:
    rendered = render_configuration_reference()
    for section_name, section_field in Settings.model_fields.items():
        model = section_field.annotation
        assert f"## `[{section_name}]`" in rendered
        for field_name in model.model_fields:  # type: ignore[union-attr]  # every section is a model
            key = f"{section_name}.{field_name}"
            line = next(line for line in rendered.splitlines() if line.startswith(f"| `{key}` |"))
            assert f"`LOADCOACH_{section_name.upper()}__{field_name.upper()}`" in line
            cells = [cell.strip() for cell in re.split(r"(?<!\\)\|", line)[1:-1]]
            assert len(cells) == 9, line
            assert cells[5] == ("yes" if key in RUNTIME_SETTINGS else "no"), key
            if key in CONFIG_ONLY_SECURITY_KEYS:
                assert cells[6].startswith("**config-only:**"), key


def test_the_check_command_reports_drift(tmp_path: Path) -> None:
    runner = CliRunner()
    target = tmp_path / "configuration.md"
    written = runner.invoke(app, ["config", "reference", "--output", str(target)])
    assert written.exit_code == 0 and target.read_text() == render_configuration_reference()
    assert (
        runner.invoke(app, ["config", "reference", "--check", "--output", str(target)]).exit_code
        == 0
    )
    target.write_text("stale")
    drifted = runner.invoke(app, ["config", "reference", "--check", "--output", str(target)])
    assert drifted.exit_code == 1 and "differs" in drifted.output
