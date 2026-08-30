"""loadcoach.services.settings — runtime-changeable settings (api.md §9, spec §12).

Configuration is loaded once at startup through the precedence chain (configuration standards
§1). A small, named set of keys may also be changed while the server runs — through
``PUT /settings``, the Settings page, or the CLI — and those live in the ``settings`` table,
where the scheduler re-reads them every second and applies them to the running process. Every
other key is config-only: security-relevant ones are refused with ``403 FORBIDDEN`` naming the
key (api.md §9), and the rest with ``VALIDATION_ERROR`` naming the key and listing what can be
changed. The set is a registry here, not a convention, so the API, the page and the CLI cannot
disagree about it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar, Final

from baseaicore import SuiteError, ValidationError
from sqlalchemy import select

from loadcoach.infrastructure.db.models import Setting
from loadcoach.infrastructure.db.repositories.settings import SettingsRepository

if TYPE_CHECKING:
    from collections.abc import Mapping
    from datetime import datetime

    from loadcoach.config import Settings
    from loadcoach.services.database import Database

__all__ = [
    "CONFIG_ONLY_SECURITY_KEYS",
    "RUNTIME_SETTINGS",
    "RuntimeSetting",
    "SettingConfigOnly",
    "read_runtime_settings",
    "runtime_settings_document",
    "write_runtime_settings",
]


class SettingConfigOnly(SuiteError):
    """A security-relevant key was sent to ``PUT /settings``; it is config-only (api.md §9)."""

    code: ClassVar[str] = "FORBIDDEN"


@dataclass(frozen=True, slots=True)
class RuntimeSetting:
    """One runtime-changeable key: where it lives in ``Settings``, its type and its bounds."""

    key: str
    kind: type[bool] | type[int] | type[float]
    description: str
    minimum: float | None = None
    maximum: float | None = None

    @property
    def section(self) -> str:
        """The ``Settings`` section the key belongs to."""
        return self.key.split(".", 1)[0]

    @property
    def field(self) -> str:
        """The field within the section."""
        return self.key.split(".", 1)[1]

    def coerce(self, value: object) -> bool | int | float:
        """Validate ``value`` for this key.

        Raises:
            ValidationError: Wrong type, or outside the bounds.
        """
        if self.kind is bool:
            if not isinstance(value, bool):
                raise ValidationError(
                    f"{self.key} must be true or false.",
                    details={"fields": [{"path": self.key, "problem": "expected a boolean"}]},
                )
            return value
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValidationError(
                f"{self.key} must be a number.",
                details={"fields": [{"path": self.key, "problem": "expected a number"}]},
            )
        number: int | float = int(value) if self.kind is int else float(value)
        if self.kind is int and float(value) != number:
            raise ValidationError(
                f"{self.key} must be a whole number.",
                details={"fields": [{"path": self.key, "problem": "expected an integer"}]},
            )
        if (self.minimum is not None and number < self.minimum) or (
            self.maximum is not None and number > self.maximum
        ):
            raise ValidationError(
                f"{self.key} must be between {self.minimum} and {self.maximum}.",
                details={
                    "fields": [
                        {
                            "path": self.key,
                            "problem": f"outside [{self.minimum}, {self.maximum}]",
                        }
                    ]
                },
            )
        return number


RUNTIME_SETTINGS: Final[dict[str, RuntimeSetting]] = {
    setting.key: setting
    for setting in (
        RuntimeSetting("queue.paused", bool, "Stop dispatch without dropping jobs."),
        RuntimeSetting("queue.draining", bool, "Finish in-flight work and claim nothing new."),
        RuntimeSetting(
            "routing.prefer_resident_bonus",
            float,
            "The residency tie-break bonus (routing §6).",
            minimum=0.0,
            maximum=1.0,
        ),
        RuntimeSetting(
            "routing.min_present_weight",
            float,
            "The measured-weight floor below which a decision is flagged low_evidence.",
            minimum=0.0,
            maximum=1.0,
        ),
        RuntimeSetting(
            "routing.min_confidence",
            float,
            "Evidence below this confidence is ignored (routing §5).",
            minimum=0.0,
            maximum=1.0,
        ),
        RuntimeSetting(
            "routing.remote_cost_factor",
            float,
            "The cost factor applied to a remote provider's candidates (routing §6).",
            minimum=0.01,
            maximum=1.0,
        ),
        RuntimeSetting(
            "storage.content_retention_hours",
            int,
            "Hours a finished job keeps its prompt and response text (spec §14).",
            minimum=0,
            maximum=24 * 365,
        ),
    )
}
"""The whole runtime-changeable set. ``queue.paused``/``queue.draining`` are the P5 control
flags under their existing keys; the rest are read by the scheduler each second."""

CONFIG_ONLY_SECURITY_KEYS: Final[frozenset[str]] = frozenset(
    {
        "server.host",
        "server.port",
        "server.allow_lan_exposure",
        "server.allowed_hosts",
        "providers.allow_remote",
        "provider.base_url",
        "provider.kind",
        "storage.database_url",
        "storage.retain_content",
        "evidence.allowed_source_hosts",
        "evidence.freeweight_url",
        "evidence.freeweight_api_key_env",
        "evidence.freeweight_api_key_file",
        "logging.include_content",
    }
)
"""Keys that decide exposure, egress, credentials or what is retained: refused with ``FORBIDDEN``
naming the key (api.md §9, spec §14)."""


def _configured(settings: Settings, setting: RuntimeSetting) -> bool | int | float:
    section = getattr(settings, setting.section)
    value: bool | int | float = getattr(section, setting.field, False)
    return value


def read_runtime_settings(database: Database, *, settings: Settings) -> dict[str, Any]:
    """Every runtime-changeable key's effective value: the table's, else the configured one."""
    with database.read() as session:
        stored = {
            str(key): value
            for key, value in session.execute(
                select(Setting.key, Setting.value_json).where(
                    Setting.key.in_(list(RUNTIME_SETTINGS))
                )
            ).all()
        }
    effective: dict[str, Any] = {}
    for key, setting in RUNTIME_SETTINGS.items():
        if key in stored:
            try:
                effective[key] = setting.coerce(stored[key])
                continue
            except ValidationError:
                pass  # a row this build cannot read falls back to configuration
        effective[key] = _configured(settings, setting)
    return effective


def write_runtime_settings(
    database: Database,
    changes: Mapping[str, Any],
    *,
    settings: Settings,
    now: datetime,
) -> dict[str, Any]:
    """Validate and store ``changes``, returning every effective value afterwards.

    Args:
        database: The application's database handle.
        changes: ``key -> value``; every key must be runtime-changeable.
        settings: The loaded configuration, for the values not overridden.
        now: The instant recorded on each row.

    Returns:
        The same document :func:`read_runtime_settings` returns.

    Raises:
        SettingConfigOnly: A security-relevant key (``403 FORBIDDEN``, naming it).
        ValidationError: An unknown key, or a value of the wrong type or outside its bounds.
    """
    for key in changes:
        if key in CONFIG_ONLY_SECURITY_KEYS:
            raise SettingConfigOnly(
                f"{key} is security-relevant and can only be set in config.toml or the "
                "environment (api.md §9).",
                details={"key": key},
            )
        if key not in RUNTIME_SETTINGS:
            raise ValidationError(
                f"{key} is not runtime-changeable.",
                details={
                    "fields": [{"path": key, "problem": "not a runtime-changeable setting"}],
                    "runtime_changeable": sorted(RUNTIME_SETTINGS),
                },
            )
    validated = {key: RUNTIME_SETTINGS[key].coerce(value) for key, value in changes.items()}
    if validated:
        repository = SettingsRepository()
        with database.write() as session:
            for key, value in validated.items():
                repository.set(session, key, value, now=now)
    return read_runtime_settings(database, settings=settings)


def runtime_settings_document(database: Database, *, settings: Settings) -> dict[str, Any]:
    """The ``GET /settings`` body: effective values, their definitions, and the config-only keys."""
    effective = read_runtime_settings(database, settings=settings)
    return {
        "settings": effective,
        "definitions": {
            key: {
                "type": setting.kind.__name__,
                "description": setting.description,
                "minimum": setting.minimum,
                "maximum": setting.maximum,
                "configured": _configured(settings, setting),
            }
            for key, setting in RUNTIME_SETTINGS.items()
        },
        "config_only": sorted(CONFIG_ONLY_SECURITY_KEYS),
    }
