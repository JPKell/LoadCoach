"""loadcoach.services.settings — runtime-changeable settings (api.md §9, spec §12).

Configuration is loaded once at startup through the precedence chain (configuration standards
§1). A small, named set of keys may also be changed while the server runs — through
``PUT /settings``, the Settings page, or the CLI — and those live in the ``settings`` table,
where the scheduler re-reads them every second and applies them to the running process. Every
other key is config-only: security-relevant ones are refused with ``403 FORBIDDEN`` naming the
key (api.md §9), and the rest with ``VALIDATION_ERROR`` naming the key and listing what can be
changed. The set is a registry here, not a convention, so the API, the page and the CLI cannot
disagree about it.

**Precedence follows the standard.** Configuration standards §7 puts a database-backed setting
*between* file and environment — ``defaults → file → database → env → CLI`` — so an operator who
pinned a value in the environment keeps it, and a stored row that cannot take effect is reported
as shadowed rather than silently applied or silently dropped. Every key follows the one rule,
``queue.paused`` and ``queue.draining`` included: an exception per key would be a second rule
nobody would remember, and a queue that unpauses itself on restart because the environment said
so is a queue whose state nobody can explain. The mitigation is visibility — the shadowed row is
kept, reported, and takes effect again the moment the variable is unset. Until 1.1.2 this module
took a stored row whenever one existed, which is the divergence
[ADR-0100](../../docs/adr/0100-promptcadences-runtime-changeable-set.md) recorded against this
application; this is the fix.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Final

from baseaicore import SuiteError, ValidationError
from sqlalchemy import select

from loadcoach.config import (
    DERIVED_CONFIG_KEYS,
    ENV_PREFIX,
    Settings,
    env_var_for,
    leaf_keys,
    load_settings_tolerant,
)
from loadcoach.domain.authorization import Principal, authorize
from loadcoach.infrastructure.db.models import Setting
from loadcoach.infrastructure.db.repositories.settings import SettingsRepository

if TYPE_CHECKING:
    from collections.abc import Mapping
    from datetime import datetime

    from loadcoach.services.database import Database

__all__ = [
    "CONFIG_ONLY_SECURITY_KEYS",
    "RUNTIME_SETTINGS",
    "SCHEMA_VERSION",
    "RuntimeSetting",
    "SettingConfigOnly",
    "config_schema_document",
    "database_overlay",
    "read_runtime_settings",
    "runtime_settings_document",
    "shadowing_source",
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
        "server.trusted_proxies",
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


def shadowing_source(key: str) -> str | None:
    """The environment variable pinning ``key``, or ``None`` when nothing shadows a stored row.

    Configuration standards §7 puts the database *between* file and environment, so a key set in
    the environment beats a stored row. CLI overrides need no separate check: this application has
    no CLI configuration layer in the serving process — ``loadcoach serve`` applies its flags as
    environment variables before :func:`loadcoach.bootstrap.bootstrap` calls the loader, and
    ``tests/unit/test_config.py`` holds ``load_settings(cli_overrides=…)`` to the tests that
    exercise the loader. The day a CLI layer reaches ``src/``,
    ``test_no_source_module_passes_cli_overrides`` fails rather than this check quietly
    mis-ordering.

    Args:
        key: A dotted ``section.field`` path from :data:`RUNTIME_SETTINGS`.

    Returns:
        ``"env LOADCOACH_…"``, naming the variable, or ``None``.
    """
    name = env_var_for(key)
    return f"env {name}" if name in os.environ else None


def _configured(settings: Settings, setting: RuntimeSetting) -> bool | int | float:
    section = getattr(settings, setting.section)
    value: bool | int | float = getattr(section, setting.field, False)
    return value


def _stored(database: Database) -> dict[str, Any]:
    """Every stored row belonging to the registry, keyed by dotted path."""
    with database.read() as session:
        return {
            str(key): value
            for key, value in session.execute(
                select(Setting.key, Setting.value_json).where(
                    Setting.key.in_(list(RUNTIME_SETTINGS))
                )
            ).all()
        }


def read_runtime_settings(database: Database, *, settings: Settings) -> dict[str, Any]:
    """Every runtime-changeable key's effective value.

    The stored row wins unless the environment pins the key (:func:`shadowing_source`) or the row
    is one this build cannot read — a value whose type or bounds the registry now refuses falls
    back to configuration rather than raising, because a row written by another version must not
    stop this one from serving.

    Args:
        database: The application's database handle.
        settings: The **configured** settings — the file/environment/CLI layers as loaded.

    Returns:
        ``key -> value`` for every key in :data:`RUNTIME_SETTINGS`.
    """
    stored = _stored(database)
    effective: dict[str, Any] = {}
    for key, setting in RUNTIME_SETTINGS.items():
        if key in stored and shadowing_source(key) is None:
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
    principal: Principal | None = None,
) -> dict[str, Any]:
    """Validate and store ``changes``, returning every effective value afterwards.

    Args:
        database: The application's database handle.
        changes: ``key -> value``; every key must be runtime-changeable.
        settings: The loaded configuration, for the values not overridden.
        now: The instant recorded on each row.

    Returns:
        The same mapping :func:`read_runtime_settings` returns — which may differ from what was
        written, when the environment shadows a key the caller stored. The row is kept either
        way: unsetting the variable makes it effective.

    Raises:
        SettingConfigOnly: A security-relevant key (``403 FORBIDDEN``, naming it).
        ValidationError: An unknown key, or a value of the wrong type or outside its bounds.
    """
    authorize(principal, "admin")
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
    """The ``GET /settings`` body: what is effective, why, and what is refused here.

    Every key carries its stored row *and* whether that row is what the process is running on: a
    row shadowed by the environment does nothing, and a document that showed it as the value
    would be the lie configuration standards §7's precedence exists to prevent.

    Args:
        database: The application's database handle.
        settings: The configured settings.

    Returns:
        ``settings`` (effective values), ``definitions`` (per key: type, description, bounds, the
        configured value, the stored value or ``None``, ``source`` — ``"database"`` or
        ``"configuration"`` — and ``shadowed_by``), and ``config_only`` (the keys refused by name).
    """
    effective = read_runtime_settings(database, settings=settings)
    stored = _stored(database)
    definitions: dict[str, Any] = {}
    for key, setting in RUNTIME_SETTINGS.items():
        shadowed_by = shadowing_source(key) if key in stored else None
        definitions[key] = {
            "type": setting.kind.__name__,
            "description": setting.description,
            "minimum": setting.minimum,
            "maximum": setting.maximum,
            "configured": _configured(settings, setting),
            "stored": stored.get(key),
            "source": "database" if key in stored and shadowed_by is None else "configuration",
            "shadowed_by": shadowed_by,
        }
    return {
        "settings": effective,
        "definitions": definitions,
        "config_only": sorted(CONFIG_ONLY_SECURITY_KEYS),
    }


def database_overlay(settings: Settings) -> dict[str, tuple[Any, str]]:
    """The runtime-changeable values the ``settings`` table decides, and how to label them.

    Configuration standards §7 asks ``config show`` to mark database-sourced values
    ``(database)``. This opens the configured database read-only to find them, and **never
    raises**: an absent, unmigrated or unreadable database is not a failure of the caller —
    printing the configured values is exactly the right answer when there is no database to
    consult, and a command or document that needed one would be unusable on a fresh install.
    Shared by ``loadcoach config show`` and :func:`config_schema_document`, so the two cannot
    disagree about which layer produced a value.

    Args:
        settings: The loaded :class:`Settings` — the file/environment layers as resolved.

    Returns:
        ``path -> (value, source)`` for the keys the database decides, plus the keys whose stored
        row is beaten by an environment variable — those keep their configured value and say that
        a row exists and does nothing. Empty when no database can be read. ``queue.paused`` and
        ``queue.draining`` are absent: they are not fields of ``Settings``, so there is no row to
        mark for them here.
    """
    from sqlalchemy.engine import make_url
    from sqlalchemy.exc import SQLAlchemyError

    from loadcoach.services.database import Database

    database_url = settings.storage.database_url
    if database_url is None:  # pragma: no cover — StorageSettings always fills this in
        return {}
    url = make_url(database_url)
    # Connecting would create the file. A read-only inspection must not leave a database behind
    # that `db status` would then report as unmigrated.
    if (
        url.drivername.startswith("sqlite")
        and url.database not in (None, ":memory:")
        and not Path(str(url.database)).is_file()
    ):
        return {}
    try:
        with Database.from_url(database_url) as database:
            document = runtime_settings_document(database, settings=settings)
    except (SQLAlchemyError, SuiteError, OSError):
        return {}
    overlay: dict[str, tuple[Any, str]] = {}
    for key, definition in document["definitions"].items():
        if definition["source"] == "database":
            overlay[key] = (document["settings"][key], "database")
        elif definition["shadowed_by"] is not None:
            overlay[key] = (
                document["settings"][key],
                f"{definition['shadowed_by']}; database row {definition['stored']} shadowed",
            )
    return overlay


SCHEMA_VERSION: Final = "1.0"
"""The version of the settings-schema document :func:`config_schema_document` emits (ADR-0127)."""


def config_schema_document(config_path: str | Path | None = None) -> dict[str, Any]:
    """Build the ADR-0127 rule 1 settings-schema document.

    Everything comes from objects that already exist and are already tested: pydantic's own
    ``Settings.model_json_schema()``, :data:`RUNTIME_SETTINGS`, :data:`CONFIG_ONLY_SECURITY_KEYS`
    and the per-leaf sources :func:`~loadcoach.config.load_settings` and :func:`database_overlay`
    already compute for ``config show``. Nothing here is a second copy of a key list.

    Args:
        config_path: As :func:`~loadcoach.config.load_settings`; the file WeightRoomGym (or an
            operator) wants described. Defaults to the resolved installation config.

    Returns:
        ``schema_version``, ``application``, ``version``, ``env_prefix``, ``config_path``,
        ``json_schema``, ``runtime_changeable`` (one entry per :data:`RUNTIME_SETTINGS` key, as
        ``key``/``kind``/``minimum``/``maximum``/``description``), ``security_keys`` (sorted
        :data:`CONFIG_ONLY_SECURITY_KEYS`), ``config_only`` (every other leaf an operator writes —
        :data:`~loadcoach.config.DERIVED_CONFIG_KEYS` excluded), ``provider_form`` (``"singular"``
        or ``"plural"``, ADR-0077 — which form is effective, so a form generator does not render
        two editors for one registration), ``sources`` (the same per-leaf layer ``config show``
        prints, database overlay included) and ``problems`` (an unknown key in the file, never
        dropped — see :func:`~loadcoach.config.load_settings_tolerant`).

    Raises:
        ConfigurationError: A *known* key in the file fails validation, both provider forms are
            configured, or an unsafe bind combination is configured — the same refusals
            ``config show`` and ``config validate`` give.
    """
    from loadcoach import __about__

    loaded, problems = load_settings_tolerant(config_path)

    sources = dict(loaded.sources)
    for path, (_value, source) in database_overlay(loaded.settings).items():
        sources[path] = source

    runtime_keys = set(RUNTIME_SETTINGS)
    security_keys = set(CONFIG_ONLY_SECURITY_KEYS)
    config_only = sorted(set(leaf_keys()) - runtime_keys - security_keys - DERIVED_CONFIG_KEYS)

    return {
        "schema_version": SCHEMA_VERSION,
        "application": "loadcoach",
        "version": __about__.__version__,
        "env_prefix": ENV_PREFIX,
        "config_path": str(loaded.config_path),
        "json_schema": Settings.model_json_schema(),
        "runtime_changeable": [
            {
                "key": setting.key,
                "kind": setting.kind.__name__,
                "minimum": setting.minimum,
                "maximum": setting.maximum,
                "description": setting.description,
            }
            for setting in RUNTIME_SETTINGS.values()
        ],
        "security_keys": sorted(security_keys),
        "config_only": config_only,
        "provider_form": "plural" if loaded.settings.providers.registrations else "singular",
        "sources": sources,
        "problems": list(problems),
    }
