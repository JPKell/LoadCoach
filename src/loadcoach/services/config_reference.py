"""loadcoach.services.config_reference — ``docs/configuration.md``, generated from the model.

Configuration standards §8: the reference lists, per field, the key path, the environment
variable, the type, the default, the valid range, whether it is runtime-changeable, its security
implications, and an example — and a test fails when the generated document differs from the
committed one. Generated from :class:`~loadcoach.config.Settings`'s own field metadata, so it
cannot drift from what the application reads; the runtime-changeable and security columns come
from :mod:`loadcoach.services.settings`'s registry for the same reason.
"""

from __future__ import annotations

import types
import typing
from typing import Any, get_args, get_origin

from pydantic import BaseModel
from pydantic.fields import FieldInfo
from pydantic_core import PydanticUndefined

from loadcoach.config import Settings
from loadcoach.services.settings import CONFIG_ONLY_SECURITY_KEYS, RUNTIME_SETTINGS

__all__ = ["render_configuration_reference"]

_HEADER = """# Configuration reference

**Generated** from `loadcoach.config.Settings` by `loadcoach config reference`; do not edit by
hand — `tests/unit/test_config_reference.py` fails when this file differs from the model.

Precedence, field by field (configuration standards §1): built-in defaults, then
`config.toml` (`loadcoach config path` prints where), then `LOADCOACH_*` environment variables,
then CLI flags. Sections and fields are joined with a double underscore in the environment:
`[server] port` is `LOADCOACH_SERVER__PORT`. Lists are comma-separated in the environment.

**Runtime-changeable** keys may also be set while the server runs, through `PUT /api/v1/settings`
or the Settings page; the scheduler applies them within a second (api.md §9). **Security-relevant**
keys decide exposure, egress, credentials or retention; they are refused there by name and can only
be set in the file or the environment (spec §14).
"""

_SECURITY_NOTES: dict[str, str] = {
    "server.host": "Non-loopback exposes the service; requires allowed_hosts and a token.",
    "server.port": "Part of the exposure decision.",
    "server.allow_lan_exposure": "Acknowledges binding every interface.",
    "server.allowed_hosts": "DNS-rebinding defence on a non-loopback bind (ADR-0026 §1).",
    "server.max_body_bytes": "Bounds what a caller can make the server buffer.",
    "server.rate_limit_per_minute": "Keeps one credential from starving others.",
    "server.rate_limit_burst": "Keeps one credential from starving others.",
    "server.failed_auth_per_minute": "Brakes credential guessing per address.",
    "storage.database_url": "Where every job, prompt hash and token digest lives.",
    "storage.retain_content": "Keeps prompt and response text for ever (spec §14).",
    "storage.content_retention_hours": "How long finished text is kept before scrubbing.",
    "provider.kind": "Which backend receives every prompt.",
    "provider.base_url": "Where prompts are sent.",
    "providers.allow_remote": "Permits egress to a remote provider.",
    "evidence.freeweight_url": "An outbound fetch target (ADR-0026 §3).",
    "evidence.freeweight_api_key_env": "A credential; resolved through the secret chain.",
    "evidence.freeweight_api_key_file": "A credential; resolved through the secret chain.",
    "evidence.allowed_source_hosts": "The outbound fetch allowlist (ADR-0026 §3).",
    "logging.include_content": "Logs full prompts and responses when true.",
}


def _type_name(annotation: Any) -> str:
    origin = get_origin(annotation)
    if origin is types.UnionType or origin is typing.Union:
        return " | ".join(_type_name(arg) for arg in get_args(annotation))
    if origin is typing.Literal:
        return " | ".join(repr(arg) for arg in get_args(annotation))
    if origin in (list, tuple, dict, set, frozenset):
        inner = ", ".join(_type_name(arg) for arg in get_args(annotation)) or "…"
        return f"{origin.__name__}[{inner}]"
    if annotation is type(None):
        return "None"
    if isinstance(annotation, type):
        if issubclass(annotation, BaseModel):
            return "table"
        return annotation.__name__
    return str(annotation).replace("typing.", "")


def _default(info: FieldInfo) -> str:
    if info.default_factory is not None:
        try:
            produced = info.default_factory()  # type: ignore[call-arg]  # no-arg factories only
        except TypeError:
            return "—"
        if isinstance(produced, BaseModel):
            return "—"
        return f"`{produced!r}`"
    if info.default is PydanticUndefined:
        return "required"
    if isinstance(info.default, BaseModel):
        return "—"
    return f"`{info.default!r}`"


def _range(info: FieldInfo) -> str:
    parts = []
    for meta in info.metadata:
        for name, symbol in (("ge", "≥"), ("gt", ">"), ("le", "≤"), ("lt", "<")):
            value = getattr(meta, name, None)
            if value is not None:
                parts.append(f"{symbol} {value}")
        pattern = getattr(meta, "pattern", None)
        if pattern:
            parts.append(f"matches `{pattern}`")
        for name in ("min_length", "max_length"):
            value = getattr(meta, name, None)
            if value is not None:
                parts.append(f"{name.replace('_', ' ')} {value}")
    return ", ".join(parts) if parts else "—"


def _example(info: FieldInfo) -> str:
    examples = info.examples or []
    return f"`{examples[0]!r}`" if examples else "—"


def _escape(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def render_configuration_reference() -> str:
    """Render the reference as Markdown, section by section, field by field."""
    lines = [_HEADER]
    for section_name, section_field in Settings.model_fields.items():
        model = section_field.annotation
        assert isinstance(model, type) and issubclass(model, BaseModel), section_name  # noqa: S101 — a settings section is always a model
        doc = (model.__doc__ or "").strip().split("\n")[0]
        lines.append(f"\n## `[{section_name}]`\n\n{_escape(doc)}\n")
        lines.append(
            "| Key | Environment variable | Type | Default | Range | Runtime-changeable | "
            "Security | Example | Description |"
        )
        lines.append("|---|---|---|---|---|---|---|---|---|")
        for field_name, info in model.model_fields.items():
            key = f"{section_name}.{field_name}"
            env = f"`LOADCOACH_{section_name.upper()}__{field_name.upper()}`"
            runtime = "yes" if key in RUNTIME_SETTINGS else "no"
            security = (
                "**config-only:** " + _SECURITY_NOTES.get(key, "security-relevant")
                if key in CONFIG_ONLY_SECURITY_KEYS
                else _SECURITY_NOTES.get(key, "—")
            )
            description = _escape(info.description or "")
            lines.append(
                f"| `{key}` | {env} | `{_escape(_type_name(info.annotation))}` | "
                f"{_escape(_default(info))} | {_escape(_range(info))} | {runtime} | "
                f"{_escape(security)} | {_escape(_example(info))} | {description} |"
            )
    lines.append("")
    return "\n".join(lines)
