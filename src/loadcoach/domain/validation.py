"""loadcoach.domain.validation — checking a model's output against a task profile's policy.

Five kinds, in the order they can possibly succeed: `json` (it parses), `json_schema` (it fits the
shape), `required_fields` (the keys a caller depends on are there), `regex` and `length`. Each is
independent and each records its own pass or failure, so a job history says *which* check failed
rather than that validation failed.

**The schema validator is deliberately small and deliberately strict about its own limits.** It
implements the JSON Schema 2020-12 keywords the suite's shipped schemas use, and it **refuses** a
schema that uses anything else rather than ignoring it. An ignored constraint is worse than an
unsupported one: it produces a validation that passed for a reason nobody intended, which is the
same class of defect as a fabricated measurement.

Framework-free per `.importlinter`'s domain-purity contract: plain data in, plain data out.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

__all__ = [
    "SUPPORTED_SCHEMA_KEYWORDS",
    "SchemaUnsupported",
    "ValidationCheck",
    "ValidationOutcome",
    "validate_json",
    "validate_length",
    "validate_output",
    "validate_regex",
    "validate_required_fields",
    "validate_schema",
]

SUPPORTED_SCHEMA_KEYWORDS: Final[frozenset[str]] = frozenset(
    {
        "$schema",
        "$comment",
        "title",
        "description",
        "type",
        "properties",
        "required",
        "additionalProperties",
        "items",
        "enum",
        "const",
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "minLength",
        "maxLength",
        "minItems",
        "maxItems",
        "pattern",
    }
)
"""Every keyword this validator understands. A schema using anything else is refused."""

_TYPE_CHECKS: Final[dict[str, Any]] = {
    "object": dict,
    "array": list,
    "string": str,
    "boolean": bool,
    "null": type(None),
}


class SchemaUnsupported(Exception):
    """A task profile's JSON Schema uses a keyword this validator does not implement.

    Raised at load time, not at validation time: a schema whose constraints cannot all be checked
    must fail the profile that references it, not silently under-validate every job that uses it.
    """


@dataclass(frozen=True, slots=True)
class ValidationCheck:
    """One check's result, as stored in the ``validations`` table.

    Attributes:
        kind: ``json``, ``json_schema``, ``required_fields``, ``regex`` or ``length``.
        passed: Whether it passed.
        detail: What failed, with the field paths a caller can act on. Empty when it passed.
    """

    kind: str
    passed: bool
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ValidationOutcome:
    """Every check performed on one attempt's output.

    Attributes:
        performed: Whether the profile asked for any validation at all. ``False`` is distinct
            from ``passed=True``: a profile that validates nothing has not verified anything.
        passed: Whether every performed check passed. ``None`` when none was performed.
        checks: Each check, in the order it ran.
        parsed: The parsed JSON, when a JSON check ran and succeeded.
    """

    performed: bool
    passed: bool | None
    checks: tuple[ValidationCheck, ...]
    parsed: Any = None

    @property
    def failures(self) -> tuple[ValidationCheck, ...]:
        """Every check that failed."""
        return tuple(check for check in self.checks if not check.passed)

    def as_json(self) -> dict[str, Any]:
        """Return the mapping the API response and the stored rows carry."""
        return {
            "performed": self.performed,
            "passed": self.passed,
            "checks": [
                {"kind": check.kind, "passed": check.passed, "detail": check.detail}
                for check in self.checks
            ],
        }


def validate_json(text: str) -> tuple[ValidationCheck, Any]:
    """Check that ``text`` is JSON, and return what it parsed to.

    Args:
        text: The model's output, exactly as returned.

    Returns:
        ``(check, parsed)``. ``parsed`` is ``None`` when the check failed.
    """
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError) as exc:
        return (
            ValidationCheck(
                kind="json",
                passed=False,
                detail={"problem": str(exc), "output_prefix": text[:200]},
            ),
            None,
        )
    return ValidationCheck(kind="json", passed=True), parsed


def _reject_unsupported(schema: Any, path: str = "$") -> None:
    if isinstance(schema, dict):
        unsupported = sorted(set(schema) - SUPPORTED_SCHEMA_KEYWORDS)
        if unsupported:
            message = (
                f"JSON Schema keyword(s) {unsupported} at {path} are not implemented by "
                f"loadcoach.domain.validation; a constraint that cannot be checked must not be "
                f"silently ignored"
            )
            raise SchemaUnsupported(message)
        for key, value in schema.items():
            if key in {"properties"} and isinstance(value, dict):
                for name, sub in value.items():
                    _reject_unsupported(sub, f"{path}.{name}")
            elif key in {"items", "additionalProperties"} and isinstance(value, dict):
                _reject_unsupported(value, f"{path}[]")


def _type_matches(value: Any, expected: str) -> bool:
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    checker = _TYPE_CHECKS.get(expected)
    if checker is None:
        return False
    if checker is str:
        return isinstance(value, str)
    return isinstance(value, checker) and not isinstance(value, bool)


def _check_node(
    value: Any, schema: Mapping[str, Any], path: str, problems: list[dict[str, str]]
) -> None:
    expected = schema.get("type")
    if expected is not None:
        options = [expected] if isinstance(expected, str) else list(expected)
        if not any(_type_matches(value, option) for option in options):
            problems.append(
                {
                    "path": path,
                    "problem": f"expected type {'|'.join(options)}, got {type(value).__name__}",
                }
            )
            return

    if "enum" in schema and value not in schema["enum"]:
        problems.append({"path": path, "problem": f"must be one of {schema['enum']!r}"})
    if "const" in schema and value != schema["const"]:
        problems.append({"path": path, "problem": f"must equal {schema['const']!r}"})

    if isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            problems.append({"path": path, "problem": f"shorter than {schema['minLength']}"})
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            problems.append({"path": path, "problem": f"longer than {schema['maxLength']}"})
        if "pattern" in schema and re.search(schema["pattern"], value) is None:
            problems.append({"path": path, "problem": f"does not match {schema['pattern']!r}"})

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        for keyword, ok, wording in (
            ("minimum", value >= schema.get("minimum", value), "below the minimum"),
            ("maximum", value <= schema.get("maximum", value), "above the maximum"),
            (
                "exclusiveMinimum",
                value > schema.get("exclusiveMinimum", value - 1),
                "not above the exclusive minimum",
            ),
            (
                "exclusiveMaximum",
                value < schema.get("exclusiveMaximum", value + 1),
                "not below the exclusive maximum",
            ),
        ):
            if keyword in schema and not ok:
                problems.append({"path": path, "problem": f"{wording} {schema[keyword]!r}"})

    if isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            problems.append({"path": path, "problem": f"fewer than {schema['minItems']} items"})
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            problems.append({"path": path, "problem": f"more than {schema['maxItems']} items"})
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for index, item in enumerate(value):
                _check_node(item, item_schema, f"{path}[{index}]", problems)

    if isinstance(value, dict):
        properties = schema.get("properties", {})
        for name in schema.get("required", []):
            if name not in value:
                problems.append({"path": f"{path}.{name}", "problem": "required but missing"})
        if schema.get("additionalProperties") is False:
            for name in value:
                if name not in properties:
                    problems.append(
                        {"path": f"{path}.{name}", "problem": "not allowed by the schema"}
                    )
        for name, sub_schema in properties.items():
            if name in value and isinstance(sub_schema, dict):
                _check_node(value[name], sub_schema, f"{path}.{name}", problems)


def validate_schema(parsed: Any, schema: Mapping[str, Any]) -> ValidationCheck:
    """Check parsed JSON against a task profile's schema.

    Args:
        parsed: The already-parsed output.
        schema: The schema, as loaded from the profile's ``json_schema_ref``.

    Returns:
        The check, with every failing field path in ``detail["fields"]`` — all of them, not the
        first, because a corrective retry that fixes one problem at a time takes one round trip
        per problem.

    Raises:
        SchemaUnsupported: The schema uses a keyword this validator does not implement.
    """
    _reject_unsupported(schema)
    problems: list[dict[str, str]] = []
    _check_node(parsed, schema, "$", problems)
    if problems:
        return ValidationCheck(kind="json_schema", passed=False, detail={"fields": problems})
    return ValidationCheck(kind="json_schema", passed=True)


def validate_required_fields(parsed: Any, fields: Sequence[str]) -> ValidationCheck:
    """Check that every named top-level field is present.

    Args:
        parsed: The already-parsed output.
        fields: The field names the profile requires.

    Returns:
        The check, naming every missing field.
    """
    if not isinstance(parsed, dict):
        return ValidationCheck(
            kind="required_fields",
            passed=False,
            detail={"problem": f"output is {type(parsed).__name__}, not an object"},
        )
    missing = [name for name in fields if name not in parsed]
    if missing:
        return ValidationCheck(kind="required_fields", passed=False, detail={"missing": missing})
    return ValidationCheck(kind="required_fields", passed=True)


def validate_regex(text: str, pattern: str) -> ValidationCheck:
    """Check that the output matches a pattern.

    Args:
        text: The model's output.
        pattern: The pattern the profile requires.

    Returns:
        The check. A pattern that will not compile fails the check with the compilation error
        rather than raising: a malformed policy is a policy failure, not a crash on every job.
    """
    try:
        matched = re.search(pattern, text) is not None
    except re.error as exc:
        return ValidationCheck(
            kind="regex", passed=False, detail={"problem": f"invalid pattern: {exc}"}
        )
    if not matched:
        return ValidationCheck(kind="regex", passed=False, detail={"pattern": pattern})
    return ValidationCheck(kind="regex", passed=True)


def validate_length(text: str, *, maximum_chars: int) -> ValidationCheck:
    """Check that the output is not longer than the profile permits.

    Args:
        text: The model's output.
        maximum_chars: The ceiling.

    Returns:
        The check, carrying both numbers when it fails.
    """
    if len(text) > maximum_chars:
        return ValidationCheck(
            kind="length",
            passed=False,
            detail={"chars": len(text), "max_output_chars": maximum_chars},
        )
    return ValidationCheck(kind="length", passed=True)


def validate_output(
    text: str,
    *,
    require_valid_json: bool = False,
    schema: Mapping[str, Any] | None = None,
    required_fields: Sequence[str] = (),
    pattern: str | None = None,
    max_output_chars: int | None = None,
) -> ValidationOutcome:
    """Run a task profile's whole validation policy over one attempt's output.

    Checks that depend on parsed JSON are skipped when the JSON check itself failed — reporting
    "missing required field" about text that is not JSON at all names the wrong problem.

    Args:
        text: The model's output, exactly as returned.
        require_valid_json: Whether the output must parse as JSON.
        schema: The profile's JSON Schema, when it requires one.
        required_fields: Top-level fields the profile requires.
        pattern: A regular expression the output must match.
        max_output_chars: The output ceiling.

    Returns:
        The :class:`ValidationOutcome`. ``performed=False`` when the policy asked for nothing.

    Raises:
        SchemaUnsupported: The schema uses a keyword this validator does not implement.
    """
    checks: list[ValidationCheck] = []
    parsed: Any = None
    needs_json = require_valid_json or schema is not None or bool(required_fields)

    if needs_json:
        json_check, parsed = validate_json(text)
        checks.append(json_check)
        if json_check.passed:
            if schema is not None:
                checks.append(validate_schema(parsed, schema))
            if required_fields:
                checks.append(validate_required_fields(parsed, required_fields))

    if pattern is not None:
        checks.append(validate_regex(text, pattern))
    if max_output_chars is not None:
        checks.append(validate_length(text, maximum_chars=max_output_chars))

    if not checks:
        return ValidationOutcome(performed=False, passed=None, checks=(), parsed=None)
    return ValidationOutcome(
        performed=True,
        passed=all(check.passed for check in checks),
        checks=tuple(checks),
        parsed=parsed,
    )
