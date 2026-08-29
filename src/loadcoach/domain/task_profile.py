"""loadcoach.domain.task_profile — the task profile shape and its validation rules.

A task profile is routing intent, not a prompt (routing §2): weights that rank candidates,
constraints that filter them, execution parameters, and a validation policy for the result.
Framework-free per `.importlinter`'s domain-purity contract — this module parses a plain dict
(already loaded from TOML by the caller) and never touches the filesystem beyond checking that a
referenced schema file exists.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar

import setspec
from baseaicore import SuiteError
from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "TaskProfile",
    "TaskProfileConstraints",
    "TaskProfileExecution",
    "TaskProfileInvalid",
    "TaskProfileValidation",
    "load_task_profiles",
]

_WEIGHT_SUM_TOLERANCE = 1e-6


class TaskProfileInvalid(SuiteError):
    """A task profile failed validation.

    ``details`` always carries ``file``, ``profile_id`` and ``problem`` — spec requires a
    malformed profile to refuse startup naming the file, the key and the problem (dev-plan P2
    acceptance criterion 3), and a caller catching this never has to parse the message to get them.
    """

    code: ClassVar[str] = "TASK_PROFILE_INVALID"


class TaskProfileConstraints(BaseModel):
    """Hard filters — a candidate that fails one is removed, not merely ranked lower."""

    model_config = ConfigDict(extra="forbid")

    min_context_tokens: int = Field(default=0, ge=0)
    requires_capabilities: tuple[str, ...] = Field(default=())
    max_latency_p95_seconds: float | None = Field(default=None, gt=0)
    min_capability_scores: dict[str, float] = Field(default_factory=dict)
    exclude_models: tuple[str, ...] = Field(default=())
    allow_remote_providers: bool = Field(default=False)


class TaskProfileExecution(BaseModel):
    """Parameters the provider call is made with.

    ``min_output_tokens`` is what makes context budgeting's reduction *permitted* rather than
    assumed (routing §9): a profile that sets it accepts a shorter answer to make a long request
    fit, down to that floor; a profile that leaves it unset is rejected with the numbers instead.
    Unset is the default, because quietly returning a truncated answer to a caller who never
    agreed to one is the failure this phase exists to prevent.
    """

    model_config = ConfigDict(extra="forbid")

    temperature: float = Field(default=0.0, ge=0.0, le=2.0)
    max_output_tokens: int = Field(default=1024, gt=0)
    min_output_tokens: int | None = Field(default=None, gt=0)
    response_format: str = Field(default="text")
    json_schema_ref: str | None = Field(default=None)
    max_attempts: int = Field(default=1, ge=1)
    fallback_depth: int = Field(default=0, ge=0)


class TaskProfileValidation(BaseModel):
    """The policy a result is checked against after execution."""

    model_config = ConfigDict(extra="forbid")

    require_valid_json: bool = Field(default=False)
    require_schema: bool = Field(default=False)
    required_fields: tuple[str, ...] = Field(default=())
    max_output_chars: int = Field(default=1_000_000, gt=0)


class TaskProfile(BaseModel):
    """One named routing intent, at one version.

    Attributes:
        profile_id: The dotted name, e.g. ``"code.review"``.
        version: Semantic version of this profile's definition.
        description: One sentence, shown in the UI and CLI.
        weights: Capability ID -> soft ranking weight. Must sum to 1.0 (validated separately, not
            by pydantic, so the error can name the exact sum found).
        constraints: Hard filters.
        execution: Provider call parameters.
        validation: Post-execution result checks.
        enabled: Whether this profile is offered for routing.
    """

    model_config = ConfigDict(extra="forbid")

    profile_id: str
    version: str
    description: str = ""
    weights: dict[str, float] = Field(default_factory=dict)
    constraints: TaskProfileConstraints = Field(default_factory=TaskProfileConstraints)
    execution: TaskProfileExecution = Field(default_factory=TaskProfileExecution)
    validation: TaskProfileValidation = Field(default_factory=TaskProfileValidation)
    enabled: bool = Field(default=True)


def _fail(file: Path, profile_id: str, problem: str) -> TaskProfileInvalid:
    return TaskProfileInvalid(
        f"Task profile {profile_id!r} in {file} is invalid: {problem}",
        details={"file": str(file), "profile_id": profile_id, "problem": problem},
    )


def _validate_one(
    file: Path, profile_id: str, raw: dict[str, Any], *, schemas_dir: Path
) -> TaskProfile:
    try:
        profile = TaskProfile.model_validate({"profile_id": profile_id, **raw})
    except Exception as exc:  # noqa: BLE001 — translated into TaskProfileInvalid below
        raise _fail(file, profile_id, str(exc)) from exc

    weight_sum = sum(profile.weights.values())
    if not profile.weights:
        raise _fail(
            file, profile_id, "weights is empty; a profile must weight at least one capability"
        )
    if abs(weight_sum - 1.0) > _WEIGHT_SUM_TOLERANCE:
        raise _fail(file, profile_id, f"weights sum to {weight_sum!r}, not 1.0")

    all_capability_names = set(profile.weights) | set(profile.constraints.requires_capabilities)
    all_capability_names |= set(profile.constraints.min_capability_scores)
    for capability_id in sorted(all_capability_names):
        if not setspec.is_known_capability(capability_id):
            raise _fail(
                file,
                profile_id,
                f"capability {capability_id!r} is not in the SetSpec vocabulary "
                f"(version {setspec.CAPABILITY_VOCABULARY_VERSION})",
            )

    if profile.execution.json_schema_ref is not None:
        schema_path = schemas_dir / profile.execution.json_schema_ref
        if not schema_path.is_file():
            raise _fail(
                file,
                profile_id,
                f"execution.json_schema_ref {profile.execution.json_schema_ref!r} does not exist "
                f"at {schema_path}",
            )
    if (
        profile.execution.response_format == "json_schema"
        and profile.execution.json_schema_ref is None
    ):
        raise _fail(
            file,
            profile_id,
            "execution.response_format is 'json_schema' but json_schema_ref is unset",
        )
    if (
        profile.execution.min_output_tokens is not None
        and profile.execution.min_output_tokens > profile.execution.max_output_tokens
    ):
        raise _fail(
            file,
            profile_id,
            f"execution.min_output_tokens ({profile.execution.min_output_tokens}) exceeds "
            f"max_output_tokens ({profile.execution.max_output_tokens})",
        )
    if profile.validation.require_schema and not profile.execution.json_schema_ref:
        raise _fail(
            file,
            profile_id,
            "validation.require_schema is set but execution.json_schema_ref is unset",
        )

    return profile


def load_task_profiles(
    profiles: dict[str, dict[str, Any]], *, file: Path, schemas_dir: Path
) -> tuple[TaskProfile, ...]:
    """Validate a set of task profiles, already parsed from TOML.

    Args:
        profiles: The ``[task_profiles."<id>"]`` table, keyed by ``profile_id``, as produced by
            ``tomllib.load`` on the shipped configuration.
        file: The source file, named in any :class:`TaskProfileInvalid` raised — this function
            never opens it itself, so the caller's own read error (if any) is reported separately.
        schemas_dir: Directory ``execution.json_schema_ref`` paths resolve against.

    Returns:
        Every profile, validated, in the order given.

    Raises:
        TaskProfileInvalid: A profile's weights do not sum to 1.0, names a capability outside the
            SetSpec vocabulary, references a schema file that does not exist, or has a
            contradictory constraint (``response_format="json_schema"`` with no
            ``json_schema_ref``, or ``require_schema`` with no ``json_schema_ref``) — named with
            the file, the profile ID and the specific problem.
    """
    return tuple(
        _validate_one(file, profile_id, raw, schemas_dir=schemas_dir)
        for profile_id, raw in profiles.items()
    )
