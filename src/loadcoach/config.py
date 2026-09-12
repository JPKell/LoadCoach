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
    "DERIVED_CONFIG_KEYS",
    "ENV_PREFIX",
    "LOOPBACK_HOSTS",
    "AdaptersSettings",
    "ConfigurationError",
    "EvidenceSettings",
    "ExecutionSettings",
    "FakeProviderSettings",
    "InsecureBindingError",
    "LoadedSettings",
    "LoggingSettings",
    "ProviderRegistrationSettings",
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
    "env_var_for",
    "leaf_keys",
    "load_settings",
    "load_settings_tolerant",
    "resolve_config_path",
    "state_dir",
]

ENV_PREFIX = "LOADCOACH_"
LOOPBACK_HOSTS: frozenset[str] = frozenset({"127.0.0.1", "localhost", "::1"})
DERIVED_CONFIG_KEYS: frozenset[str] = frozenset({"providers.registrations"})
"""Leaf keys that are *collected*, not written by an operator (see :class:`ProvidersSettings`).

Shared by the configuration reference (``services/config_reference.py``) and the settings-schema
document (``services/settings.py``, ADR-0127) so a key nobody types is never reported as an
ordinary key in either place.
"""
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
    trusted_proxies: tuple[str, ...] = Field(
        default=(),
        description=(
            "CIDR networks of reverse proxies whose X-Forwarded-For may be believed "
            "(ADR-0014 §7). When the connecting peer is inside one, the client address — what "
            "the failed-authentication brake and the unauthenticated rate bucket key on — is "
            "taken from the last untrusted hop of X-Forwarded-For; from any other peer the "
            "header is ignored entirely, because anyone can send it. Empty by default: behind "
            "a proxy with this unset, every caller shares the proxy's address and one caller's "
            "failures brake them all. Comma-separated in the environment."
        ),
        examples=[["127.0.0.0/8"]],
    )

    _split_allowed_hosts = field_validator("allowed_hosts", mode="before")(_split_csv)
    _split_trusted_proxies = field_validator("trusted_proxies", mode="before")(_split_csv)

    @field_validator("trusted_proxies")
    @classmethod
    def _trusted_proxies_are_networks(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        """Refuse a value that is not a parseable network, at startup rather than per request."""
        import ipaddress

        for entry in value:
            try:
                ipaddress.ip_network(entry, strict=False)
            except ValueError as exc:
                raise ValueError(
                    f"trusted_proxies entry {entry!r} is not an IP network (e.g. 127.0.0.0/8)"
                ) from exc
        return value


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


class FakeProviderSettings(BaseModel):
    """``[provider.fake]`` — override the declared model ``kind = "fake"`` serves (E6).

    Every field defaults to ``None``, in which case :func:`loadcoach.infrastructure.providers.
    factory.build_provider` uses its own small built-in fake model — one whose VRAM estimate never
    trips the ``insufficient_vram`` hard constraint (routing.md §4), so a fake-provider journey is
    reproducible on any machine rather than gated on a free GPU.

    Setting these lets an operator deliberately reproduce ``insufficient_vram`` end to end, with
    the whole ``estimate`` block populated, on a machine with plenty of free VRAM. All four fields
    must be set together, or none: the estimate's KV term (``2 × layers × kv_heads × head_dim × 2
    bytes`` for the assumed f16 precision, multiplied by the served context) dominates
    ``size_bytes`` at any interesting context length, so ``size_bytes`` alone cannot reliably
    provoke the rejection this block exists to reach.
    """

    model_config = ConfigDict(extra="forbid")

    size_bytes: int | None = Field(
        default=None,
        gt=0,
        description="On-disk weight size to declare, overriding the small built-in default.",
        examples=[8_540_000_000],
    )
    layers: int | None = Field(
        default=None,
        gt=0,
        description="Transformer block count to declare, overriding the small built-in default.",
        examples=[32],
    )
    kv_heads: int | None = Field(
        default=None,
        gt=0,
        description="Key/value head count to declare, overriding the small built-in default.",
        examples=[8],
    )
    head_dim: int | None = Field(
        default=None,
        gt=0,
        description="Per-head dimension to declare, overriding the small built-in default.",
        examples=[128],
    )


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
    fake: FakeProviderSettings = Field(
        default_factory=FakeProviderSettings,
        description=(
            "Override the model kind='fake' declares, to provoke insufficient_vram on purpose "
            "(E6); absent by default, and absent entirely on a normal install. See spec.md §12."
        ),
        examples=[
            {"size_bytes": 8_540_000_000, "layers": 32, "kv_heads": 8, "head_dim": 128},
        ],
    )

    def as_registration(self) -> ProviderRegistrationSettings:
        """Return this singular block as the one registration it is (ADR-0077 rule 1).

        ``remote`` is ``False`` and carries no knob here: the shipped default is a loopback
        provider, every deployment that has this block today is local, and an operator with a
        remote endpoint moves to a named block to say so — which is the same act as opting in
        (ADR-0077 rule 2).
        """
        return ProviderRegistrationSettings(
            kind=self.kind,
            base_url=self.base_url,
            timeout_seconds=self.timeout_seconds,
            remote=False,
            fake=self.fake,
        )

    @property
    def name(self) -> str:
        """The singular block's registration name: its kind (ADR-0077 rule 1)."""
        return self.kind


class ProviderRegistrationSettings(BaseModel):
    """One ``[providers.<name>]`` block: a provider LoadCoach registers, by name and kind.

    LC-E1, generalized (ADR-0055). The name is the operator's, and it is what explanations, the
    models UI and ``doctor`` refer to; the kind decides which adapter is constructed.
    """

    model_config = ConfigDict(extra="forbid")

    kind: str = Field(
        description="Which provider adapter serves this registration.",
        examples=["ollama", "llamacpp", "fake"],
    )
    enabled: bool = Field(
        default=True,
        description=(
            "Whether this registration is registered at all. A disabled registration keeps its "
            "block in the file — nothing about it is lost or has to be retyped — and is skipped "
            "by the registry: no handle is built for it, discovery never lists it, routing "
            "cannot reach it, and the models it last served answer "
            "`unavailable_reason = 'provider_disabled'`. Disabling every registration is "
            "refused: an application with no provider registers nothing and serves nothing."
        ),
        examples=[True],
    )
    base_url: str = Field(
        default="",
        description="The provider's API endpoint, where its kind takes one.",
        examples=["http://127.0.0.1:11434"],
    )
    timeout_seconds: float = Field(
        default=300.0, gt=0, description="Per-call provider timeout.", examples=[300.0]
    )
    remote: bool = Field(
        default=False,
        description=(
            "Whether this registration is somewhere other than this machine. Declared, never "
            "inferred from the kind or the URL: an OpenAI-compatible endpoint on loopback is "
            "local, and the same kind pointed at a hosted API is remote (ADR-0055 rule 4)."
        ),
        examples=[False],
    )
    model_directory: str = Field(
        default="",
        description=(
            "For kind='llamacpp': the directory of GGUF weights this server serves. Required "
            "for that kind, meaningless for every other — a llama.cpp registration launches and "
            "supervises its own server rather than talking to one that is already running."
        ),
        examples=["~/models/llm"],
    )
    state_dir: str = Field(
        default="",
        description=(
            "For kind='llamacpp': where the supervisor keeps its per-server state and the "
            "artifact-digest file (ADR-0071). Empty uses `<data_dir>/llamacpp/<name>`."
        ),
        examples=[""],
    )
    server_path: str = Field(
        default="llama-server",
        description="For kind='llamacpp': the server binary, found on PATH by default.",
        examples=["llama-server"],
    )
    memory_max_bytes: int | None = Field(
        default=None,
        ge=1,
        description=(
            "For kind='llamacpp': a host-memory cap on every server this registration launches "
            "(ADR-0119) — a systemd-run user scope with MemoryMax at this value and swap denied, "
            "so a server that does not fit is killed rather than swapping the host. Unset "
            "launches uncapped."
        ),
        examples=[25769803776],
    )
    memory_high_bytes: int | None = Field(
        default=None,
        ge=1,
        description=(
            "For kind='llamacpp': the throttle point below memory_max_bytes (MemoryHigh). "
            "Requires memory_max_bytes and must be below it."
        ),
        examples=[23622320128],
    )
    fake: FakeProviderSettings = Field(
        default_factory=FakeProviderSettings,
        description="For kind='fake' only; see ProviderSettings.fake.",
    )

    @model_validator(mode="after")
    def _check_memory_cap(self) -> ProviderRegistrationSettings:
        """Refuse a throttle point without a cap, or one not below it (ADR-0119).

        Raises:
            ValueError: ``memory_high_bytes`` is set without ``memory_max_bytes``, or is not
                below it. ModelRack refuses the same shape at construction; catching it here names
                the configuration key rather than a constructor argument.
        """
        if self.memory_high_bytes is not None and (
            self.memory_max_bytes is None or self.memory_high_bytes >= self.memory_max_bytes
        ):
            raise ValueError(
                f"memory_high_bytes ({self.memory_high_bytes}) requires memory_max_bytes and "
                f"must be below it (got {self.memory_max_bytes})."
            )
        return self


def _registration_valued(schema: dict[str, Any]) -> None:
    """Say in the JSON schema that an extra key under ``[providers]`` is a registration table.

    ``extra="allow"`` alone emits ``additionalProperties: true``, which is less than the truth:
    :meth:`ProvidersSettings._collect_registrations` refuses an extra that is not a registration
    table, so every extra key **is** a :class:`ProviderRegistrationSettings`. A generated settings
    form reading ``config schema --json`` (ADR-0127) needs that: without it, an operator's
    ``[providers.local]`` keys are types the document cannot resolve and the form has to show them
    raw. The reference is taken from the ``registrations`` field's own schema rather than written
    out, so it cannot name a definition this document does not carry.

    Args:
        schema: The generated schema of this model; mutated in place, as pydantic's
            ``json_schema_extra`` callable contract expects.
    """
    schema["additionalProperties"] = schema["properties"]["registrations"]["additionalProperties"]


class ProvidersSettings(BaseModel):
    """Cross-provider policy, plus the ``[providers.<name>]`` registrations themselves.

    ``allow_remote`` is policy — may *any* remote provider be routed to at all — and it is
    evaluated above a registration's own ``remote`` flag: a remote registration in a deployment
    that disallows remote is configured, visible and never routed to (ADR-0077 rule 4).

    Every other key under ``[providers]`` is a registration, so this model allows extras and
    validates them itself rather than forbidding them: ``[providers.local]`` and
    ``[providers] allow_remote`` share one TOML table, and pydantic sees both in one mapping.
    """

    model_config = ConfigDict(extra="allow", json_schema_extra=_registration_valued)

    allow_remote: bool = Field(
        default=False,
        description="Permit a remote provider at all — an explicit, deliberate opt-in.",
        examples=[False],
    )
    registrations: dict[str, ProviderRegistrationSettings] = Field(
        default_factory=dict,
        description="The named registrations, keyed by their operator-chosen names.",
    )

    @model_validator(mode="after")
    def _collect_registrations(self) -> ProvidersSettings:
        """Lift every extra key under ``[providers]`` into :attr:`registrations`.

        Returns:
            This model, with the extras consumed.

        Raises:
            ValueError: An extra key whose value is not a table — a typo like
                ``[providers] allow_remot = true`` reaches here as a scalar, and a scalar is not a
                provider. Refusing it keeps ``extra="allow"`` from turning every misspelling into
                a silently ignored key.
        """
        extras = self.__pydantic_extra__ or {}
        if not extras:
            return self
        collected = dict(self.registrations)
        for name, value in extras.items():
            if not isinstance(value, dict):
                raise ValueError(
                    f"[providers] has an unknown key {name!r}. A key under [providers] is either "
                    "`allow_remote` or a provider registration table `[providers.<name>]`; "
                    f"{name!r} is neither."
                )
            collected[name] = ProviderRegistrationSettings.model_validate(value)
        object.__setattr__(self, "registrations", collected)
        if self.__pydantic_extra__ is not None:
            self.__pydantic_extra__.clear()
        return self


class AdaptersSettings(BaseModel):
    """``[adapters]`` — the operator's directory of adapter artifacts and their manifests.

    Opt-in, per application (ADR-0061 rule 2): **empty means the whole feature is off**, and a
    deployment that has never heard of adapters is unaffected by every part of it.
    """

    model_config = ConfigDict(extra="forbid")

    directory: str = Field(
        default="",
        description=(
            "Directory holding adapter artifacts and their reviewed manifests. Empty — the "
            "default — turns adapters off entirely; nothing is scanned, registered or routed."
        ),
        examples=["~/models/adapters"],
    )

    @property
    def path(self) -> Path | None:
        """The configured directory as a path, or ``None`` when the feature is off."""
        if not self.directory.strip():
            return None
        return Path(self.directory).expanduser()


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
    """A per-model override of the default runtime profile (ADR-0023).

    Every field left ``None`` says nothing and lets the ``[runtime]`` default through.
    ``kv_cache_precision`` and ``flash_attention`` are llama.cpp launch settings (ADR-0120): a
    resolved profile that sets either for an Ollama registration is rejected by name at routing
    time, and a quantized cache without flash attention likewise — both are evaluated on the
    *resolved* profile, because the default level may supply the other half.
    """

    model_config = ConfigDict(extra="forbid")

    context_size: int | None = Field(
        default=None,
        gt=0,
        description="Context window to serve for this specific model, in tokens.",
        examples=[32768],
    )
    kv_cache_precision: Literal["f16", "q8_0", "q4_0"] | None = Field(
        default=None,
        description=(
            "KV-cache precision for this model: f16, q8_0 or q4_0 (llamacpp only, ADR-0120). "
            "q8_0 and q4_0 require flash attention on the resolved profile."
        ),
        examples=["q8_0"],
    )
    flash_attention: bool | None = Field(
        default=None,
        description="Flash attention for this model (llamacpp only, ADR-0120).",
        examples=[True],
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
    kv_cache_precision: Literal["", "f16", "q8_0", "q4_0"] = Field(
        default="",
        description=(
            "Empty leaves it to the provider. f16, q8_0 or q4_0 is sent to llama.cpp launches; "
            "q8_0 and q4_0 require flash_attention = true (ADR-0120)."
        ),
        examples=[""],
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

    @model_validator(mode="after")
    def _check_quantized_cache_has_flash_attention(self) -> RuntimeSettings:
        """Refuse a quantized default KV cache without flash attention (ADR-0120 rule 3).

        Raises:
            ValueError: ``kv_cache_precision`` is ``q8_0`` or ``q4_0`` and ``flash_attention``
                is false. llama.cpp would keep the V cache at f16 without saying so, and a stored
                profile naming a precision that was not served is a fabricated subject. A
                per-model override is checked on the resolved profile at routing time instead.
        """
        if self.kv_cache_precision in {"q8_0", "q4_0"} and not self.flash_attention:
            raise ValueError(
                f"runtime.kv_cache_precision = {self.kv_cache_precision!r} requires "
                "runtime.flash_attention = true: llama.cpp cannot quantize the V cache without "
                "flash attention and would silently serve f16."
            )
        return self


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

    task_profiles_path: str = Field(
        default="",
        description=(
            "A task_profiles.toml to import at startup instead of the shipped one. Empty means "
            "the shipped file. The import is an upsert per (profile_id, version), so a file "
            "naming a shipped profile replaces it and one naming a new id adds it."
        ),
        examples=["/etc/loadcoach/task_profiles.toml"],
    )
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
    base_switch_penalty: float = Field(
        default=0.10,
        ge=0,
        le=1,
        description=(
            "Two-level residency's penalty for a candidate that would need its own base loaded "
            "while another base is resident (routing §6.1, ADR-0066). Chosen, not measured: "
            "twice prefer_resident_bonus, so a base switch is never decided by the tie-break the "
            "bonus exists to be. A deployment that has measured its own load times should set it "
            "from them."
        ),
        examples=[0.10],
    )
    require_adapter_evidence: bool = Field(
        default=True,
        description=(
            "Refuse to *route* to an adapter subject with no measured evidence for the profile's "
            "top-weighted capability (ADR-0064 rule 3). On by default: until FreeWeight measures "
            "adapters, every adapter subject is unmeasured, so adapters are invisible to routed "
            "selection while pins keep working. That is 'no benchmark, no use' behaving correctly."
        ),
        examples=[True],
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


class ConsoleSettings(BaseModel):
    """Where WeightRoomGym is, when one fronts this application (row WM2).

    Set, the top bar gains the suite's tab strip: WeightRoomGym itself and the peer applications
    through it (``<url>/apps/<name>``). Unset — the default — nothing is rendered: an
    application knows only its own port, and a strip of loopback links another machine cannot
    reach would be a strip of dead links.
    """

    model_config = ConfigDict(extra="forbid")

    url: str = Field(
        default="",
        description=(
            "WeightRoomGym's base URL (https://<host>:8769); empty renders no application tab "
            "strip."
        ),
        examples=["https://jordan-main.local:8769"],
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
    adapters: AdaptersSettings = Field(default_factory=AdaptersSettings)
    execution: ExecutionSettings = Field(default_factory=ExecutionSettings)
    runtime: RuntimeSettings = Field(default_factory=RuntimeSettings)
    queue: QueueSettings = Field(default_factory=QueueSettings)
    routing: RoutingSettings = Field(default_factory=RoutingSettings)
    evidence: EvidenceSettings = Field(default_factory=EvidenceSettings)
    residency: ResidencySettings = Field(default_factory=ResidencySettings)
    telemetry: TelemetrySettings = Field(default_factory=TelemetrySettings)
    logging: LoggingSettings = Field(default_factory=LoggingSettings)
    console: ConsoleSettings = Field(default_factory=ConsoleSettings)


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


def leaf_keys() -> tuple[str, ...]:
    """Every ``section.field`` dotted path :class:`Settings` recognizes.

    Built from the same walk as :func:`_known_dotted_keys`, filtered to leaves — the schema
    document (ADR-0127) and the configuration reference both need this list, and neither keeps a
    second copy of it.
    """
    return tuple(key for key in _known_dotted_keys() if "." in key)


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


def env_var_for(path: str) -> str:
    """The environment variable that sets the leaf at dotted ``path``.

    One spelling, shared by the loader, the configuration reference and
    :func:`loadcoach.services.settings.shadowing_source`, so no two of them can disagree about
    which variable pins a key.

    Args:
        path: A dotted ``section.field`` path, as :attr:`LoadedSettings.sources` keys them.

    Returns:
        The full variable name, prefix included — ``storage.content_retention_hours`` is
        ``LOADCOACH_STORAGE__CONTENT_RETENTION_HOURS``.

    Raises:
        ValueError: ``path`` names no field (no ``.`` in it).
    """
    section, _, field_name = path.partition(".")
    if not field_name:
        message = f"{path!r} is not a section.field path"
        raise ValueError(message)
    return f"{ENV_PREFIX}{section.upper()}__{field_name.upper().replace('.', '__')}"


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
                sources[path] = f"env {env_var_for(path)}"
            elif section_name in file_data and field_name in file_data[section_name]:
                sources[path] = "file"
            else:
                sources[path] = "default"
    return sources


def _refuse_both_provider_forms(merged: dict[str, Any], config_path: Path) -> None:
    """Refuse a configuration that writes both the singular and a named provider block.

    ADR-0077 rule 3. There is no precedence rule, because a precedence rule is a silent answer to
    a question the operator did not know they had asked: the half-migrated file — a
    ``[providers.<name>]`` added and ``[provider]`` not deleted — would otherwise run a registry
    that is not the one its operator is reading.

    It runs on the merged raw data rather than on :class:`Settings`, because after validation the
    singular block is always present with its defaults and "written by the operator" is no longer
    a question the model can answer.

    Args:
        merged: File, environment and CLI layers, merged, before defaults.
        config_path: The file the message names.

    Raises:
        ConfigurationError: Both forms are present, naming the singular block, every named block
            and the one-line fix.
    """
    if "provider" not in merged:
        return
    providers = merged.get("providers")
    if not isinstance(providers, dict):
        return
    named = sorted(key for key, value in providers.items() if isinstance(value, dict))
    if not named:
        return
    listed = ", ".join(f"[providers.{name}]" for name in named)
    raise ConfigurationError(
        f"{config_path} configures providers twice: the singular [provider] block and "
        f"{listed}. The singular block is exactly one registration named after its kind "
        "(ADR-0077), so keeping both would run a registry that is not the one written here. "
        "Delete [provider] and keep the named blocks, or delete the named blocks and keep "
        "[provider].",
        details={"field": "provider", "named_providers": named, "file": str(config_path)},
    )


def _read_file(resolved_path: Path) -> tuple[dict[str, Any], bool]:
    """Parse ``resolved_path`` as TOML, or return an empty mapping when it does not exist."""
    if not resolved_path.is_file():
        return {}, False
    try:
        with resolved_path.open("rb") as handle:
            return tomllib.load(handle), True
    except tomllib.TOMLDecodeError as exc:
        raise ConfigurationError(
            f"Configuration file {resolved_path} is not valid TOML: {exc}",
            details={"file": str(resolved_path)},
        ) from exc


def _validate(merged: dict[str, Any], resolved_path: Path) -> Settings:
    """Validate a merged, layered configuration dict into a :class:`Settings`."""
    _refuse_both_provider_forms(merged, resolved_path)
    try:
        settings = Settings.model_validate(merged)
    except PydanticValidationError as exc:
        raise _translate_validation_error(exc, resolved_path) from exc
    _validate_security(settings)
    return settings


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
            field's type or range, both the singular ``[provider]`` block and a
            ``[providers.<name>]`` block are configured (ADR-0077 rule 3), or an unsafe bind
            combination is configured (:class:`InsecureBindingError`, a subclass). Does **not**
            check for an active API token — see :mod:`loadcoach.bootstrap`.
    """
    resolved_path = resolve_config_path(config_path)
    file_data, file_used = _read_file(resolved_path)
    env_data = _read_env(ENV_PREFIX)
    cli_data = cli_overrides or {}
    merged = _deep_merge(_deep_merge(file_data, env_data), cli_data)
    settings = _validate(merged, resolved_path)
    sources = _track_sources(file_data, env_data, cli_data)
    return LoadedSettings(
        settings=settings, config_path=resolved_path, config_file_used=file_used, sources=sources
    )


def load_settings_tolerant(
    config_path: str | Path | None = None,
) -> tuple[LoadedSettings, tuple[str, ...]]:
    """Like :func:`load_settings`, but an unknown key in the file is reported, never fatal.

    Built for ``config schema`` (ADR-0127 rule 1): the tool that describes why a configuration
    file doesn't load cannot itself refuse to load it. Every other kind of problem — a bad type,
    an out-of-range value, both provider forms, an unsafe bind — still raises exactly as
    :func:`load_settings` does; only an unrecognized key path is stripped and reported back rather
    than failing the whole document. A section that validates its own extra keys — ``[providers]``
    and its ``[providers.<name>]`` registrations (ADR-0077) — is passed through untouched, since
    there is nothing here for it to strip: the model's own ``extra="allow"`` already accepts them.

    Args:
        config_path: As :func:`load_settings`.

    Returns:
        The validated :class:`LoadedSettings` (built with unknown keys removed) and a tuple of
        ``"unknown configuration key '…'"`` messages, empty when the file had none.

    Raises:
        ConfigurationError: The file is not valid TOML, a *known* key fails validation, both
            provider forms are configured, or an unsafe bind combination is configured.
    """
    resolved_path = resolve_config_path(config_path)
    file_data, file_used = _read_file(resolved_path)
    known_sections = set(Settings.model_fields)
    known_leaves = set(leaf_keys())
    problems: list[str] = []
    clean_file: dict[str, Any] = {}
    for section, fields in file_data.items():
        if section not in known_sections:
            problems.append(f"unknown configuration key '{section}'")
            continue
        section_model = Settings.model_fields[section].annotation
        allows_extra = (
            isinstance(section_model, type)
            and issubclass(section_model, BaseModel)
            and section_model.model_config.get("extra") == "allow"
        )
        if not isinstance(fields, dict) or allows_extra:
            clean_file[section] = fields  # not a table, or validates its own extras
            continue
        clean_fields: dict[str, Any] = {}
        for field_name, value in fields.items():
            path = f"{section}.{field_name}"
            if path not in known_leaves:
                problems.append(f"unknown configuration key '{path}'")
                continue
            clean_fields[field_name] = value
        clean_file[section] = clean_fields

    env_data = _read_env(ENV_PREFIX)
    merged = _deep_merge(_deep_merge(clean_file, env_data), {})
    settings = _validate(merged, resolved_path)
    sources = _track_sources(clean_file, env_data, {})
    loaded = LoadedSettings(
        settings=settings, config_path=resolved_path, config_file_used=file_used, sources=sources
    )
    return loaded, tuple(problems)


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

# One provider, the common case. This block is fully supported and is not deprecated: it is
# exactly one registration named after its kind, declaring remote = false (ADR-0077).
[provider]
kind = "ollama"
base_url = "http://127.0.0.1:11434"
timeout_seconds = 300.0

# More than one provider: name each of them. `remote` is declared, never inferred from the kind
# or the URL (ADR-0055). Writing a [providers.<name>] block *and* the [provider] block above is
# refused at startup, naming both -- delete one.
# [providers.local]
# kind = "ollama"
# base_url = "http://127.0.0.1:11434"
# remote = false
# [providers.hosted]
# kind = "ollama"
# base_url = "https://example.invalid"
# remote = true

[providers]
allow_remote = false        # policy: may *any* remote registration be routed to at all

[adapters]
directory = ""              # empty = adapters are off entirely (ADR-0061)

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
# A task_profiles.toml of your own, imported at startup instead of the shipped one. Empty means
# the shipped file. The import is an upsert per (profile_id, version): a file naming a shipped
# profile replaces it, one naming a new id adds it. A path that is not a file is refused at
# startup rather than falling back, because routing under profiles you did not write is not
# something you could tell from the outside.
task_profiles_path = ""
strategy = "weighted_evidence"
min_confidence = 0.05
prefer_resident_bonus = 0.05
base_switch_penalty = 0.10        # two-level residency (routing §6.1); chosen, not measured
require_adapter_evidence = true   # "no benchmark, no use" for adapter subjects (ADR-0064 rule 3)
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

[console]
url = ""                     # WeightRoomGym's URL (https://<host>:8769); set, the top bar links
                             # to it and to the peer applications through it (row WM2)
"""
