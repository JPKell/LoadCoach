"""Tests for loadcoach.domain.task_profile — the four rejection cases dev-plan P2 names."""

from __future__ import annotations

from pathlib import Path

import pytest

from loadcoach.domain.task_profile import TaskProfileInvalid, load_task_profiles
from loadcoach.services.task_profiles import (
    DEFAULT_SCHEMAS_DIR,
    DEFAULT_TASK_PROFILES_PATH,
    read_task_profiles_file,
)

_FILE = Path("task_profiles.toml")
_SCHEMAS_DIR = Path("schemas")


def _valid_profile(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "version": "1.0.0",
        "description": "A valid profile.",
        "weights": {"reasoning": 0.6, "instruction_following": 0.4},
        "constraints": {},
        "execution": {},
        "validation": {},
    }
    base.update(overrides)
    return base


def test_weights_not_summing_to_one_rejected() -> None:
    profiles = {"bad.profile": _valid_profile(weights={"reasoning": 0.5, "coding": 0.3})}
    with pytest.raises(TaskProfileInvalid) as excinfo:
        load_task_profiles(profiles, file=_FILE, schemas_dir=_SCHEMAS_DIR)
    assert excinfo.value.details["profile_id"] == "bad.profile"
    assert excinfo.value.details["file"] == str(_FILE)
    assert "sum" in excinfo.value.details["problem"]


def test_unknown_capability_in_weights_rejected() -> None:
    profiles = {"bad.profile": _valid_profile(weights={"telepathy": 1.0})}
    with pytest.raises(TaskProfileInvalid) as excinfo:
        load_task_profiles(profiles, file=_FILE, schemas_dir=_SCHEMAS_DIR)
    assert "telepathy" in excinfo.value.details["problem"]


def test_unknown_capability_in_requires_capabilities_rejected() -> None:
    profiles = {"bad.profile": _valid_profile(constraints={"requires_capabilities": ["telepathy"]})}
    with pytest.raises(TaskProfileInvalid) as excinfo:
        load_task_profiles(profiles, file=_FILE, schemas_dir=_SCHEMAS_DIR)
    assert "telepathy" in excinfo.value.details["problem"]


def test_unknown_capability_in_min_capability_scores_rejected() -> None:
    profiles = {
        "bad.profile": _valid_profile(constraints={"min_capability_scores": {"telepathy": 0.5}})
    }
    with pytest.raises(TaskProfileInvalid) as excinfo:
        load_task_profiles(profiles, file=_FILE, schemas_dir=_SCHEMAS_DIR)
    assert "telepathy" in excinfo.value.details["problem"]


def test_missing_schema_file_rejected(tmp_path: Path) -> None:
    profiles = {
        "bad.profile": _valid_profile(
            execution={
                "response_format": "json_schema",
                "json_schema_ref": "does_not_exist.json",
            }
        )
    }
    with pytest.raises(TaskProfileInvalid) as excinfo:
        load_task_profiles(profiles, file=_FILE, schemas_dir=tmp_path)
    assert "does_not_exist.json" in excinfo.value.details["problem"]


def test_contradictory_json_schema_format_without_ref_rejected() -> None:
    profiles = {"bad.profile": _valid_profile(execution={"response_format": "json_schema"})}
    with pytest.raises(TaskProfileInvalid) as excinfo:
        load_task_profiles(profiles, file=_FILE, schemas_dir=_SCHEMAS_DIR)
    assert "json_schema_ref" in excinfo.value.details["problem"]


def test_contradictory_require_schema_without_ref_rejected() -> None:
    profiles = {"bad.profile": _valid_profile(validation={"require_schema": True})}
    with pytest.raises(TaskProfileInvalid) as excinfo:
        load_task_profiles(profiles, file=_FILE, schemas_dir=_SCHEMAS_DIR)
    assert "json_schema_ref" in excinfo.value.details["problem"]


def test_empty_weights_rejected() -> None:
    profiles = {"bad.profile": _valid_profile(weights={})}
    with pytest.raises(TaskProfileInvalid) as excinfo:
        load_task_profiles(profiles, file=_FILE, schemas_dir=_SCHEMAS_DIR)
    assert "empty" in excinfo.value.details["problem"]


def test_a_valid_profile_loads_cleanly() -> None:
    profiles = {"general.chat": _valid_profile()}
    (loaded,) = load_task_profiles(profiles, file=_FILE, schemas_dir=_SCHEMAS_DIR)
    assert loaded.profile_id == "general.chat"
    assert loaded.weights == {"reasoning": 0.6, "instruction_following": 0.4}


def test_all_twenty_shipped_profiles_load_and_validate() -> None:
    """dev-plan P2 acceptance criterion 1."""
    profiles = read_task_profiles_file(DEFAULT_TASK_PROFILES_PATH, schemas_dir=DEFAULT_SCHEMAS_DIR)
    profile_ids = {profile.profile_id for profile in profiles}
    assert len(profiles) == 20
    assert "content.review" in profile_ids


def test_content_review_weighted_on_the_documented_capabilities() -> None:
    """routing.md §2: content.review is weighted on auditing, instruction_following, reasoning
    and structured_output — the prose-review intent IdeaPress's audit stages route to."""
    profiles = read_task_profiles_file(DEFAULT_TASK_PROFILES_PATH, schemas_dir=DEFAULT_SCHEMAS_DIR)
    content_review = next(p for p in profiles if p.profile_id == "content.review")
    assert set(content_review.weights) == {
        "auditing",
        "instruction_following",
        "reasoning",
        "structured_output",
    }
