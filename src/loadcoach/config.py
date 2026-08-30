"""loadcoach.config — typed settings, source-tracked, per Configuration Standards.

Precedence, lowest to highest: built-in defaults, ``config.toml``, ``LOADCOACH_``-prefixed
environment variables, then explicit overrides (the CLI's highest layer). Overriding is per leaf
field, not per section (configuration standards §1): setting one field of ``[server]`` never
discards its siblings.

Mirrors FreeWeight's own ``config.py`` (ADR-0026 applies identically to both applications, and
LoadCoach is "the application most likely to be exposed" per spec §14): this module performs its
own merge of the file, environment and override layers rather than leaning on
``pydantic-settings``'s own source-priority machinery, because ``config show`` has to report
*which* layer produced every leaf value — a property that is easiest to get right by building the
merged dict ourselves and tracking provenance alongside it, then handing the result to pydantic once
for validation.
"""

from __future__ import annotations

import difflib
import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar, Literal

from baseaicore import ConfigurationError
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic import ValidationError as PydanticValidationError

__all__ = [
    "EXAMPLE_CONFIG_TOML",
    "ENV_PREFIX",
    "LOOPBACK_HOSTS",
    "ConfigurationError",
    "EvidenceSettings",
    "ExecutionSettings",
    "InsecureBindingError",
    "LoadedSettings",
    "LoggingSettings",
    "ProviderSettings",
    "ProvidersSettings",
    "QueueSettings",
    "ResidencySettings",
    "RoutingSettings",
    "RuntimeModelOverride",
    "RuntimeSettings",
    "ServerSettings",
    "Settings",
    "StorageSettings",
    "TelemetrySettings",
    "config_dir",
    "data_dir",
    "load_settings",
    "resolve_config_path",
    "state_dir",
]

ENV_PREFIX = "LOADCOACH_"
LOOPBACK_HOSTS: frozenset[str] = frozenset({"127.0.0.1", "localhost", "::1"})
_ALL_INTERFACES_HOST = "0.0.0.0"  # noqa: S104 — compared against, never bound to, by this module
_RESERVED_ENV_SUFFIXES = frozenset({"CONFIG", "DATA_DIR", "LOG_LEVEL"})
_DEFAULT_PORT = 8766


class InsecureBindingError(ConfigurationError):
    """A configured bind/auth combination would expose the service unsafely.

    Raised by :func:`load_settings` before anything opens a socket (configuration standards §4).
    Every rule here has a documented, deliberate acknowledgement that lifts it; none can be
    satisfied by accident.
    """

    code: ClassVar[str] = "INSECURE_BINDING"


def _split_csv(value: Any) -> Any:
    """Accept a comma-separated string for a tuple field, as environment variables must (§3)."""
    if isinstance(value, str):
        return tuple(part.strip() for part in value.split(",") if part.strip())
    return value


class ServerSettings(BaseModel):
    """Bind address and HTTP-level limits."""

    model_config = ConfigDict(extra="forbid")

    host: str = Field(
        default="127.0.0.1",
        description=(
            "Interface to bind. Loopback by default; anything else requires allowed_hosts and at "
            "least one active API token (ADR-0026)."
        ),
        examples=["127.0.0.1"],
    )
    port: int = Field(
        default=_DEFAULT_PORT,
        ge=1,
        le=65535,
        description="TCP port for the web UI and the API.",
        examples=[_DEFAULT_PORT],
    )
    allow_lan_exposure: bool = Field(
        default=False,
        description=(
            "Acknowledges a deliberate bind to every interface (0.0.0.0). Without it such a bind "
            "refuses to start."
        ),
        examples=[False],
    )
    allowed_hosts: tuple[str, ...] = Field(
        default=(),
        description=(
            "Host header values accepted on a non-loopback bind, against DNS rebinding. "
            "Comma-separated in the environment."
        ),
        examples=[["loadcoach.local"]],
    )

    max_body_bytes: int = Field(
        default=16 * 1024 * 1024,
        ge=1024,
        description=(
            "The largest request body accepted, refused with 413 before buffering (Security "
            "Standards §14). Matches SetSpec's envelope limit, the largest document any endpoint "
            "parses."
        ),
        examples=[16777216],
    )
    rate_limit_per_minute: int = Field(
        default=600,
        ge=0,
        description=(
            "Requests per minute one credential may make to /api/v1, sustained (spec §14). A "
            "token bucket: rate_limit_burst may arrive at once, then this rate. 0 disables. At "
            "the limit a caller gets 429 RATE_LIMITED with Retry-After, never a dropped request."
        ),
        examples=[600],
    )
    rate_limit_burst: int = Field(
        default=100,
        ge=1,
        description="How many requests one credential may make at once before the rate applies.",
        examples=[100],
    )
    failed_auth_per_minute: int = Field(
        default=20,
        ge=0,
        description=(
            "Failed authentications one address may make per minute before it is refused with "
            "429 for the rest of the minute (ADR-0014 §6). 0 disables."
        ),
        examples=[20],
    )

    _split_allowed_hosts = field_validator("allowed_hosts", mode="before")(_split_csv)


class StorageSettings(BaseModel):
    """Database location, resolved through WeightsDB (ADR-0006)."""

    model_config = ConfigDict(extra="forbid")

    database_url: str | None = Field(
        default=None,
        description=(
            "SQLAlchemy URL. Unset resolves to a SQLite file under the XDG data directory; "
            "PostgreSQL is the other supported dialect (ADR-0006)."
        ),
        examples=["sqlite:////var/lib/loadcoach/loadcoach.sqlite3"],
    )
    auto_migrate: bool = Field(
        default=True,
        description=(
            "Migrate on startup. Unset means true on SQLite and false on PostgreSQL, where a "
            "failed migration cannot be rolled back automatically (database standards §5.1)."
        ),
        examples=[True],
    )
    backup_retention: int = Field(
        default=5,
        ge=0,
        description="Automatic pre-migration backups kept before the oldest is rotated away.",
        examples=[5],
    )
    statement_timeout_ms: int | None = Field(
        default=None,
        gt=0,
        description=(
            "PostgreSQL statement (and lock) timeout. Unset leaves the server default; SQLite "
            "uses its own busy timeout, which the engine always sets."
        ),
        examples=[30000],
    )
    content_retention_hours: int = Field(
        default=24,
        ge=0,
        description=(
            "How long a finished job keeps its prompt and response text before the retention "
            "sweep replaces them with their hashes (spec §14: content is stored as hashes by "
            "default; data model §3). 0 scrubs at the first sweep after completion. A queued "
            "job always keeps its transcript until it has run. Runtime-changeable."
        ),
        examples=[24],
    )
    retain_content: bool = Field(
        default=False,
        description=(
            "Keep prompt and response text for ever, disabling the retention sweep. A privacy "
            "decision, so config-only: it cannot be changed through PUT /settings."
        ),
        examples=[False],
    )

    @model_validator(mode="after")
    def _apply_data_dir_defaults(self) -> StorageSettings:
        """Fill in the zero-configuration defaults, resolved against the XDG data directory."""
        if self.database_url is None:
            self.database_url = f"sqlite:///{data_dir()}/loadcoach.sqlite3"
        if "auto_migrate" not in self.model_fields_set:
            self.auto_migrate = self._auto_migrate_default()
        return self

    def _auto_migrate_default(self) -> bool:
        """Migrate on startup by default on SQLite, never on PostgreSQL (database standards §5)."""
        url = self.database_url or ""
        return url.startswith("sqlite")


class ProviderSettings(BaseModel):
    """The default model provider LoadCoach talks to."""

    model_config = ConfigDict(extra="forbid")

    kind: str = Field(
        default="ollama",
        description="Which provider serves the models: ollama, or fake for tests.",
        examples=["ollama"],
    )
    base_url: str = Field(
        default="http://127.0.0.1:11434",
        description="The provider's API endpoint.",
        examples=["http://127.0.0.1:11434"],
    )
    timeout_seconds: float = Field(
        default=300.0, gt=0, description="Per-call provider timeout.", examples=[300.0]
    )


class ProvidersSettings(BaseModel):
    """Cross-provider policy, distinct from the single default provider's own settings."""

    model_config = ConfigDict(extra="forbid")

    allow_remote: bool = Field(
        default=False,
        description="Permit a remote provider at all — an explicit, deliberate opt-in.",
        examples=[False],
    )


class ExecutionSettings(BaseModel):
    """``[execution]`` — concurrency, timeout and retry policy for job execution (Phase 4-5)."""

    model_config = ConfigDict(extra="forbid")

    max_concurrent_jobs: int = Field(
        default=1,
        ge=1,
        description="Raise only on multi-GPU or CPU-only setups.",
        examples=[1],
    )
    default_timeout_seconds: float = Field(
        default=300.0, gt=0, description="Per-job execution timeout.", examples=[300.0]
    )
    max_attempts: int = Field(
        default=3, ge=1, description="Attempts before a job is marked failed.", examples=[3]
    )
    attempt_backoff_seconds: float = Field(
        default=2.0, ge=0, description="Delay between retry attempts.", examples=[2.0]
    )


class RuntimeModelOverride(BaseModel):
    """A per-model override of the default runtime profile (ADR-0023)."""

    model_config = ConfigDict(extra="forbid")

    context_size: int | None = Field(
        default=None,
        gt=0,
        description="Context window to serve for this specific model, in tokens.",
        examples=[32768],
    )


class RuntimeSettings(BaseModel):
    """``[runtime]`` — the default runtime profile every execution resolves against (ADR-0023).

    Every field defaults to a value meaning "provider decides"; ``context_size = 0`` and
    ``kv_cache_precision = ""`` are that sentinel in TOML's type system, since the profile itself
    (``baseaicore.RuntimeProfile``) uses ``None`` for the same meaning once loaded.
    """

    model_config = ConfigDict(extra="forbid")

    context_size: int = Field(
        default=0,
        ge=0,
        description=(
            "Context window to serve, in tokens. 0 leaves it to the provider; a task profile with "
            "min_context_tokens sets it explicitly where the provider reports "
            "context_configurable."
        ),
        examples=[0],
    )
    kv_cache_precision: str = Field(
        default="", description="Empty leaves it to the provider.", examples=[""]
    )
    flash_attention: bool = Field(
        default=False, description="Empty/false leaves it to the provider.", examples=[False]
    )
    keep_alive: str = Field(
        default="5m",
        description="How long the provider holds a model resident after a call.",
        examples=["5m"],
    )
    models: dict[str, RuntimeModelOverride] = Field(
        default_factory=dict,
        description="Per-model runtime overrides, keyed by canonical model ID.",
        examples=[{"ollama/qwen3.5:9b-q8_0@sha256:1f3a9c4e2b70": {"context_size": 32768}}],
    )


class QueueSettings(BaseModel):
    """``[queue]`` — job queue depth, leasing and ageing policy (Phase 5)."""

    model_config = ConfigDict(extra="forbid")

    max_depth: int = Field(default=1000, ge=1, examples=[1000])
    max_active_per_source: int = Field(
        default=200,
        ge=0,
        description=(
            "Active (non-terminal) jobs one source — a token, or an X-Client-Name on loopback — "
            "may hold at once; a submission past it is refused with QUEUE_FULL naming the "
            "source and the cap (spec §14). 0 disables the per-source cap."
        ),
        examples=[200],
    )
    lease_seconds: int = Field(default=60, ge=1, examples=[60])
    poll_interval_ms: int = Field(default=250, ge=1, examples=[250])
    lease_renewal_interval_seconds: int = Field(
        default=20,
        ge=1,
        description="lease_seconds must exceed 3x this plus slack.",
        examples=[20],
    )
    ageing_interval_seconds: int = Field(default=30, ge=1, examples=[30])
    max_wait_seconds: int = Field(default=3600, ge=1, examples=[3600])
    ageing_priority_per_minute: float = Field(default=1.0, ge=0, examples=[1.0])
    overflow_allowance: int = Field(default=100, ge=0, examples=[100])
    max_affinity_streak: int = Field(default=5, ge=1, examples=[5])
    idempotency_ttl_hours: float = Field(
        default=24.0,
        gt=0,
        description=(
            "How long a job's idempotency key stays reserved after enqueue. A key reused after "
            "this starts new work rather than replaying an old result (api.md §4, data model §2)."
        ),
        examples=[24.0],
    )
    cancelling_watchdog_seconds: int = Field(
        default=30,
        ge=1,
        description=(
            "How long a job may sit in `cancelling` before the scheduler forces it to `cancelled` "
            "and records that it did (queue §8, §9)."
        ),
        examples=[30],
    )

    @model_validator(mode="after")
    def _check_lease_renewal_margin(self) -> QueueSettings:
        """Refuse a lease shorter than three renewal intervals.

        Exactly 3x — the shipped ``60 / 20`` — is accepted, and is safe given how the keeper is
        scheduled. The lease keeper runs on the scheduler thread, which is woken every
        ``poll_interval_ms`` (250 ms) and renews every in-flight lease to ``now + lease_seconds``
        as soon as ``lease_renewal_interval_seconds`` have elapsed since the last renewal — so a
        renewal is late by at most one scheduler tick, never by a whole interval. At 3x, a lease
        renewed at ``t`` still has ``lease_seconds - interval`` (40 s) left when the next renewal
        is due, and survives **two** consecutive missed renewals; it is lost only when the
        scheduler thread has stalled for more than ``2 x interval`` (40 s), which is precisely the
        condition a lease exists to detect (ADR-0029 §4). The "+ slack" the spec comment asks for
        is therefore already inside the 3x: the slack is one full interval, not a fraction of one.

        Raises:
            ValueError: ``lease_seconds`` is below 3x ``lease_renewal_interval_seconds`` — a lease
                that could expire after a single missed renewal would let a slow-but-alive
                worker's job be reclaimed out from under it, which is the double-execution defect
                the atomic claim exists to prevent.
        """
        if self.lease_seconds < 3 * self.lease_renewal_interval_seconds:
            raise ValueError(
                f"queue.lease_seconds ({self.lease_seconds}) must be at least 3x "
                f"queue.lease_renewal_interval_seconds ({self.lease_renewal_interval_seconds})."
            )
        return self


class RoutingSettings(BaseModel):
    """``[routing]`` — scoring strategy and confidence policy (Phase 3)."""

    model_config = ConfigDict(extra="forbid")

    strategy: str = Field(default="weighted_evidence", examples=["weighted_evidence"])
    min_confidence: float = Field(default=0.05, ge=0, le=1, examples=[0.05])
    prefer_resident_bonus: float = Field(default=0.05, ge=0, le=1, examples=[0.05])
    min_present_weight: float = Field(default=0.5, ge=0, le=1, examples=[0.5])
    remote_cost_factor: float = Field(
        default=0.9,
        gt=0,
        le=1,
        description=(
            "The cost factor applied to a remote provider's candidates (routing §6). 1.0 is "
            "always used for local providers; anything below 1 prefers local at equal capability."
        ),
        examples=[0.9],
    )
    explanation_retention_days: int = Field(
        default=0, ge=0, description="0 = forever.", examples=[0]
    )


class EvidenceSettings(BaseModel):
    """``[evidence]`` — the optional FreeWeight evidence source (Phase 6, ADR-0026)."""

    model_config = ConfigDict(extra="forbid")

    freeweight_url: str = Field(
        default="",
        description="Empty means not configured, not unavailable.",
        examples=[""],
    )
    freeweight_api_key_env: str = Field(
        default="",
        description="Environment variable naming a bearer token, or empty (ADR-0026).",
        examples=[""],
    )
    freeweight_api_key_file: str = Field(
        default="",
        description="Path to a file containing a bearer token, or empty (ADR-0026).",
        examples=[""],
    )
    allowed_source_hosts: tuple[str, ...] = Field(
        default=("127.0.0.1", "localhost", "::1"),
        description="Fetch allowlist for evidence import URLs (ADR-0026 §3).",
        examples=[["127.0.0.1", "localhost", "::1"]],
    )
    import_interval_hours: float = Field(default=24.0, gt=0, examples=[24.0])
    accept_schema_majors: tuple[int, ...] = Field(
        default=(1,),
        description=(
            "Which `benchmark.evidence_bundle` schema majors this installation reads. May only "
            "narrow what this build carries payload models for, never widen it."
        ),
        examples=[[1]],
    )

    _split_allowed_source_hosts = field_validator("allowed_source_hosts", mode="before")(_split_csv)

    @field_validator("accept_schema_majors")
    @classmethod
    def _check_majors_are_readable(cls, value: tuple[int, ...]) -> tuple[int, ...]:
        """Refuse a schema major this build has no payload models for.

        Accepting a major SetSpec does not ship models for would let a bundle past version
        negotiation and into a v1 reader, which is the "partially parse a newer major" failure
        the import contract exists to prevent. The setting may therefore *narrow* what this build
        can read — an installation that wants to refuse a major it could read is entitled to —
        and never widen it.

        Args:
            value: The configured majors.

        Returns:
            The same tuple.

        Raises:
            ValueError: A configured major is not one this build carries models for; the message
                names both what was asked for and what is available.
        """
        import setspec

        available = sorted(setspec.SUPPORTED_SCHEMAS["benchmark.evidence_bundle"])
        unknown = sorted(set(value) - set(available))
        if unknown:
            message = (
                f"evidence.accept_schema_majors names major version(s) {unknown}, which this "
                f"build has no benchmark.evidence_bundle payload models for; it carries "
                f"{available}. A consumer cannot read a shape it does not have a model for, and "
                "accepting one anyway would let a newer major be parsed by an older reader."
            )
            raise ValueError(message)
        return value


class ResidencySettings(BaseModel):
    """``[residency]`` — model unload policy, per device (Phase 5, queue §6)."""

    model_config = ConfigDict(extra="forbid")

    unload_idle_seconds: int = Field(default=900, ge=0, examples=[900])
    max_resident_models: int = Field(default=1, ge=1, description="Per GPU.", examples=[1])


class TelemetrySettings(BaseModel):
    """``[telemetry]`` — GPU sampling behaviour."""

    model_config = ConfigDict(extra="forbid")

    interval_ms: int = Field(default=1000, gt=0, examples=[1000])
    vram_headroom_bytes: int = Field(
        default=536_870_912, ge=0, description="Per GPU.", examples=[536870912]
    )


class LoggingSettings(BaseModel):
    """Structured-logging behaviour."""

    model_config = ConfigDict(extra="forbid")

    level: str = Field(default="INFO", description="Log verbosity.", examples=["INFO"])
    format: Literal["text", "json", "auto"] = Field(
        default="auto",
        description="text, json, or auto (text on a TTY, json otherwise).",
        examples=["auto"],
    )
    include_content: bool = Field(
        default=False,
        description=(
            "Log full prompts and responses. Off by default: only hashes and lengths are logged."
        ),
        examples=[False],
    )


class Settings(BaseModel):
    """The complete, validated LoadCoach configuration.

    Constructed only by :func:`load_settings`, which resolves the precedence chain first — never
    call ``Settings(**raw_dict)`` directly on unmerged input, or the file/env/CLI layering in
    configuration standards §1 is bypassed.
    """

    model_config = ConfigDict(extra="forbid")

    server: ServerSettings = Field(default_factory=ServerSettings)
    storage: StorageSettings = Field(default_factory=StorageSettings)
    provider: ProviderSettings = Field(default_factory=ProviderSettings)
    providers: ProvidersSettings = Field(default_factory=ProvidersSettings)
    execution: ExecutionSettings = Field(default_factory=ExecutionSettings)
    runtime: RuntimeSettings = Field(default_factory=RuntimeSettings)
    queue: QueueSettings = Field(default_factory=QueueSettings)
    routing: RoutingSettings = Field(default_factory=RoutingSettings)
    evidence: EvidenceSettings = Field(default_factory=EvidenceSettings)
    residency: ResidencySettings = Field(default_factory=ResidencySettings)
    telemetry: TelemetrySettings = Field(default_factory=TelemetrySettings)
    logging: LoggingSettings = Field(default_factory=LoggingSettings)


@dataclass(frozen=True, slots=True)
class LoadedSettings:
    """The result of resolving configuration: the settings, and where every value came from."""

    settings: Settings
    config_path: Path
    config_file_used: bool
    sources: dict[str, str]


def config_dir() -> Path:
    """Return ``$XDG_CONFIG_HOME/loadcoach``, falling back to ``~/.config/loadcoach``."""
    base = os.environ.get("XDG_CONFIG_HOME")
    root = Path(base).expanduser() if base else Path.home() / ".config"
    return root / "loadcoach"


def data_dir() -> Path:
    """Return ``$LOADCOACH_DATA_DIR``, else ``$XDG_DATA_HOME/loadcoach``, else the XDG default."""
    override = os.environ.get(f"{ENV_PREFIX}DATA_DIR")
    if override:
        return Path(override).expanduser()
    base = os.environ.get("XDG_DATA_HOME")
    root = Path(base).expanduser() if base else Path.home() / ".local" / "share"
    return root / "loadcoach"


def state_dir() -> Path:
    """Return ``$XDG_STATE_HOME/loadcoach``, falling back to ``~/.local/state/loadcoach``."""
    base = os.environ.get("XDG_STATE_HOME")
    root = Path(base).expanduser() if base else Path.home() / ".local" / "state"
    return root / "loadcoach"


def resolve_config_path(explicit: str | Path | None = None) -> Path:
    """Resolve the configuration file location per Configuration Standards §2.

    Order: an explicit path (``--config``), then ``LOADCOACH_CONFIG``, then a project-local
    ``./loadcoach.toml`` if one exists in the current directory, then the XDG default. A missing
    file at the resolved path is not an error — :func:`load_settings` falls back to defaults.
    """
    if explicit is not None:
        return Path(explicit).expanduser()
    env_path = os.environ.get(f"{ENV_PREFIX}CONFIG")
    if env_path:
        return Path(env_path).expanduser()
    local = Path.cwd() / "loadcoach.toml"
    if local.is_file():
        return local
    return config_dir() / "config.toml"


def _read_env(prefix: str) -> dict[str, Any]:
    """Parse ``<prefix>SECTION__FIELD`` environment variables into a nested dict."""
    nested: dict[str, Any] = {}
    for key, value in os.environ.items():
        if not key.startswith(prefix):
            continue
        suffix = key[len(prefix) :]
        if suffix in _RESERVED_ENV_SUFFIXES:
            continue
        path = suffix.lower().split("__")
        node = nested
        for part in path[:-1]:
            node = node.setdefault(part, {})
        node[path[-1]] = value

    log_level = os.environ.get(f"{prefix}LOG_LEVEL")
    if log_level and "level" not in nested.get("logging", {}):
        nested.setdefault("logging", {})["level"] = log_level
    return nested


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Merge ``override`` onto ``base``, recursively, per leaf field rather than per section."""
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _known_dotted_keys() -> list[str]:
    """Every ``section`` and ``section.field`` name Settings recognizes, for typo suggestions."""
    known: list[str] = []
    for section_name, section_field in Settings.model_fields.items():
        known.append(section_name)
        section_model = section_field.annotation
        if isinstance(section_model, type) and issubclass(section_model, BaseModel):
            known.extend(
                f"{section_name}.{field_name}" for field_name in section_model.model_fields
            )
    return known


def _translate_validation_error(
    exc: PydanticValidationError, config_path: Path
) -> ConfigurationError:
    """Turn a pydantic ``ValidationError`` into a :class:`ConfigurationError` naming the field."""
    known_keys = _known_dotted_keys()
    problems: list[str] = []
    for error in exc.errors():
        loc = ".".join(str(part) for part in error["loc"])
        if error["type"] == "extra_forbidden":
            suggestion = difflib.get_close_matches(loc, known_keys, n=1)
            hint = f" (did you mean '{suggestion[0]}'?)" if suggestion else ""
            problems.append(f"unknown configuration key '{loc}'{hint}")
        else:
            problems.append(f"{loc}: {error['msg']} (got {error.get('input')!r})")
    message = f"Configuration invalid ({config_path}): " + "; ".join(problems)
    return ConfigurationError(message, details={"file": str(config_path), "problems": problems})


def _validate_security(settings: Settings) -> None:
    """Refuse the unsafe bind combinations checkable without a database (configuration standards).

    The third member of the refusal set — at least one active API token when non-loopback — needs
    a database read and is checked separately, in ``bootstrap()``, once the database is ready.
    """
    server = settings.server
    if server.host == _ALL_INTERFACES_HOST and not server.allow_lan_exposure:
        raise InsecureBindingError(
            "server.host is '0.0.0.0' (all interfaces) but server.allow_lan_exposure is false. "
            "Exposing the service beyond this machine must be a deliberate act: set "
            "server.allow_lan_exposure = true if that is intended.",
            details={"field": "server.allow_lan_exposure", "host": server.host},
        )
    if server.host not in LOOPBACK_HOSTS and not server.allowed_hosts:
        raise InsecureBindingError(
            "server.host is not loopback but server.allowed_hosts is empty. A non-loopback bind "
            "must name every hostname it will accept, or DNS rebinding can reach it.",
            details={"field": "server.allowed_hosts", "host": server.host},
        )


def _track_sources(
    file_data: dict[str, Any], env_data: dict[str, Any], cli_data: dict[str, Any]
) -> dict[str, str]:
    """Report, for every leaf field, which layer produced its effective value."""
    sources: dict[str, str] = {}
    for section_name, section_field in Settings.model_fields.items():
        section_model = section_field.annotation
        if not (isinstance(section_model, type) and issubclass(section_model, BaseModel)):
            continue
        for field_name in section_model.model_fields:
            path = f"{section_name}.{field_name}"
            if section_name in cli_data and field_name in cli_data[section_name]:
                sources[path] = "cli"
            elif section_name in env_data and field_name in env_data[section_name]:
                env_key = f"{ENV_PREFIX}{section_name.upper()}__{field_name.upper()}"
                sources[path] = f"env {env_key}"
            elif section_name in file_data and field_name in file_data[section_name]:
                sources[path] = "file"
            else:
                sources[path] = "default"
    return sources


def load_settings(
    *,
    config_path: str | Path | None = None,
    cli_overrides: dict[str, Any] | None = None,
) -> LoadedSettings:
    """Resolve configuration through the full precedence chain and validate it.

    Args:
        config_path: An explicit ``--config`` path. See :func:`resolve_config_path` for the
            fallback order when this is ``None``.
        cli_overrides: Explicit values from CLI flags, nested the same way as the TOML file
            (``{"server": {"port": 9000}}``). This is the highest-precedence layer.

    Returns:
        The validated :class:`LoadedSettings`.

    Raises:
        ConfigurationError: The file is not valid TOML, a key is unrecognized, a value fails a
            field's type or range, or an unsafe bind combination is configured
            (:class:`InsecureBindingError`, a subclass). Does **not** check for an active API
            token — see :mod:`loadcoach.bootstrap`.
    """
    resolved_path = resolve_config_path(config_path)
    file_data: dict[str, Any] = {}
    file_used = False
    if resolved_path.is_file():
        try:
            with resolved_path.open("rb") as handle:
                file_data = tomllib.load(handle)
        except tomllib.TOMLDecodeError as exc:
            raise ConfigurationError(
                f"Configuration file {resolved_path} is not valid TOML: {exc}",
                details={"file": str(resolved_path)},
            ) from exc
        file_used = True

    env_data = _read_env(ENV_PREFIX)
    cli_data = cli_overrides or {}
    merged = _deep_merge(_deep_merge(file_data, env_data), cli_data)

    try:
        settings = Settings.model_validate(merged)
    except PydanticValidationError as exc:
        raise _translate_validation_error(exc, resolved_path) from exc

    _validate_security(settings)

    sources = _track_sources(file_data, env_data, cli_data)
    return LoadedSettings(
        settings=settings, config_path=resolved_path, config_file_used=file_used, sources=sources
    )


EXAMPLE_CONFIG_TOML = """\
# LoadCoach configuration.
# Every key below is optional; a fresh install with no file at all is fully functional.
# Precedence: defaults -> this file -> LOADCOACH_* environment variables -> CLI flags.

[server]
host = "127.0.0.1"
port = 8766
allow_lan_exposure = false
allowed_hosts = []          # required when host is not loopback (ADR-0026)

[storage]
# database_url defaults to a location under the XDG data directory.
# auto_migrate defaults to true on SQLite and false on PostgreSQL, where a failed migration
# cannot be rolled back automatically (database standards §5.1). Set it explicitly to override.
backup_retention = 5        # automatic pre-migration backups to keep

[provider]
kind = "ollama"
base_url = "http://127.0.0.1:11434"
timeout_seconds = 300.0

[providers]
allow_remote = false

[execution]
max_concurrent_jobs = 1         # raise only on multi-GPU or CPU-only setups
default_timeout_seconds = 300.0
max_attempts = 3
attempt_backoff_seconds = 2.0

[runtime]
# The default runtime profile every execution resolves against (ADR-0023).
context_size = 0            # 0 = leave to the provider
kv_cache_precision = ""
flash_attention = false
keep_alive = "5m"
# [runtime.models."ollama/qwen3.5:9b-q8_0@sha256:1f3a9c4e2b70"]
# context_size = 32768

[queue]
max_depth = 1000
lease_seconds = 60
poll_interval_ms = 250
lease_renewal_interval_seconds = 20   # lease_seconds must exceed 3x this
ageing_interval_seconds = 30
max_wait_seconds = 3600
ageing_priority_per_minute = 1.0
overflow_allowance = 100
max_affinity_streak = 5
idempotency_ttl_hours = 24.0          # a key is reserved this long, then released
cancelling_watchdog_seconds = 30      # a job never stays in `cancelling` longer than this

[routing]
strategy = "weighted_evidence"
min_confidence = 0.05
prefer_resident_bonus = 0.05
min_present_weight = 0.5
explanation_retention_days = 0    # 0 = forever

[evidence]
freeweight_url = ""          # empty = not configured, not "unavailable"
freeweight_api_key_env = ""  # or freeweight_api_key_file (ADR-0026)
allowed_source_hosts = ["127.0.0.1", "localhost", "::1"]
import_interval_hours = 24.0
accept_schema_majors = [1]

[residency]
unload_idle_seconds = 900
max_resident_models = 1     # per GPU

[telemetry]
interval_ms = 1000
vram_headroom_bytes = 536870912   # per GPU

[logging]
level = "INFO"
format = "auto"              # text | json | auto (text on a TTY, json otherwise)
include_content = false
"""
