# Configuration reference

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


## `[server]`

Bind address and HTTP-level limits.

| Key | Environment variable | Type | Default | Range | Runtime-changeable | Security | Example | Description |
|---|---|---|---|---|---|---|---|---|
| `server.host` | `LOADCOACH_SERVER__HOST` | `str` | `'127.0.0.1'` | — | no | **config-only:** Non-loopback exposes the service; requires allowed_hosts and a token. | `'127.0.0.1'` | Interface to bind. Loopback by default; anything else requires allowed_hosts and at least one active API token (ADR-0026). |
| `server.port` | `LOADCOACH_SERVER__PORT` | `int` | `8766` | ≥ 1, ≤ 65535 | no | **config-only:** Part of the exposure decision. | `8766` | TCP port for the web UI and the API. |
| `server.allow_lan_exposure` | `LOADCOACH_SERVER__ALLOW_LAN_EXPOSURE` | `bool` | `False` | — | no | **config-only:** Acknowledges binding every interface. | `False` | Acknowledges a deliberate bind to every interface (0.0.0.0). Without it such a bind refuses to start. |
| `server.allowed_hosts` | `LOADCOACH_SERVER__ALLOWED_HOSTS` | `tuple[str, Ellipsis]` | `()` | — | no | **config-only:** DNS-rebinding defence on a non-loopback bind (ADR-0026 §1). | `['loadcoach.local']` | Host header values accepted on a non-loopback bind, against DNS rebinding. Comma-separated in the environment. |
| `server.max_body_bytes` | `LOADCOACH_SERVER__MAX_BODY_BYTES` | `int` | `16777216` | ≥ 1024 | no | Bounds what a caller can make the server buffer. | `16777216` | The largest request body accepted, refused with 413 before buffering (Security Standards §14). Matches SetSpec's envelope limit, the largest document any endpoint parses. |
| `server.rate_limit_per_minute` | `LOADCOACH_SERVER__RATE_LIMIT_PER_MINUTE` | `int` | `600` | ≥ 0 | no | Keeps one credential from starving others. | `600` | Requests per minute one credential may make to /api/v1, sustained (spec §14). A token bucket: rate_limit_burst may arrive at once, then this rate. 0 disables. At the limit a caller gets 429 RATE_LIMITED with Retry-After, never a dropped request. |
| `server.rate_limit_burst` | `LOADCOACH_SERVER__RATE_LIMIT_BURST` | `int` | `100` | ≥ 1 | no | Keeps one credential from starving others. | `100` | How many requests one credential may make at once before the rate applies. |
| `server.failed_auth_per_minute` | `LOADCOACH_SERVER__FAILED_AUTH_PER_MINUTE` | `int` | `20` | ≥ 0 | no | Brakes credential guessing per address. | `20` | Failed authentications one address may make per minute before it is refused with 429 for the rest of the minute (ADR-0014 §6). 0 disables. |
| `server.trusted_proxies` | `LOADCOACH_SERVER__TRUSTED_PROXIES` | `tuple[str, Ellipsis]` | `()` | — | no | **config-only:** security-relevant | `['127.0.0.0/8']` | CIDR networks of reverse proxies whose X-Forwarded-For may be believed (ADR-0014 §7). When the connecting peer is inside one, the client address — what the failed-authentication brake and the unauthenticated rate bucket key on — is taken from the last untrusted hop of X-Forwarded-For; from any other peer the header is ignored entirely, because anyone can send it. Empty by default: behind a proxy with this unset, every caller shares the proxy's address and one caller's failures brake them all. Comma-separated in the environment. |

## `[storage]`

Database location, resolved through WeightsDB (ADR-0006).

| Key | Environment variable | Type | Default | Range | Runtime-changeable | Security | Example | Description |
|---|---|---|---|---|---|---|---|---|
| `storage.database_url` | `LOADCOACH_STORAGE__DATABASE_URL` | `str \| None` | `None` | — | no | **config-only:** Where every job, prompt hash and token digest lives. | `'sqlite:////var/lib/loadcoach/loadcoach.sqlite3'` | SQLAlchemy URL. Unset resolves to a SQLite file under the XDG data directory; PostgreSQL is the other supported dialect (ADR-0006). |
| `storage.auto_migrate` | `LOADCOACH_STORAGE__AUTO_MIGRATE` | `bool` | `True` | — | no | — | `True` | Migrate on startup. Unset means true on SQLite and false on PostgreSQL, where a failed migration cannot be rolled back automatically (database standards §5.1). |
| `storage.backup_retention` | `LOADCOACH_STORAGE__BACKUP_RETENTION` | `int` | `5` | ≥ 0 | no | — | `5` | Automatic pre-migration backups kept before the oldest is rotated away. |
| `storage.statement_timeout_ms` | `LOADCOACH_STORAGE__STATEMENT_TIMEOUT_MS` | `int \| None` | `None` | > 0 | no | — | `30000` | PostgreSQL statement (and lock) timeout. Unset leaves the server default; SQLite uses its own busy timeout, which the engine always sets. |
| `storage.content_retention_hours` | `LOADCOACH_STORAGE__CONTENT_RETENTION_HOURS` | `int` | `24` | ≥ 0 | yes | How long finished text is kept before scrubbing. | `24` | How long a finished job keeps its prompt and response text before the retention sweep replaces them with their hashes (spec §14: content is stored as hashes by default; data model §3). 0 scrubs at the first sweep after completion. A queued job always keeps its transcript until it has run. Runtime-changeable. |
| `storage.retain_content` | `LOADCOACH_STORAGE__RETAIN_CONTENT` | `bool` | `False` | — | no | **config-only:** Keeps prompt and response text for ever (spec §14). | `False` | Keep prompt and response text for ever, disabling the retention sweep. A privacy decision, so config-only: it cannot be changed through PUT /settings. |

## `[provider]`

The default model provider LoadCoach talks to.

| Key | Environment variable | Type | Default | Range | Runtime-changeable | Security | Example | Description |
|---|---|---|---|---|---|---|---|---|
| `provider.kind` | `LOADCOACH_PROVIDER__KIND` | `str` | `'ollama'` | — | no | **config-only:** Which backend receives every prompt. | `'ollama'` | Which provider serves the models: ollama, or fake for tests. |
| `provider.base_url` | `LOADCOACH_PROVIDER__BASE_URL` | `str` | `'http://127.0.0.1:11434'` | — | no | **config-only:** Where prompts are sent. | `'http://127.0.0.1:11434'` | The provider's API endpoint. |
| `provider.timeout_seconds` | `LOADCOACH_PROVIDER__TIMEOUT_SECONDS` | `float` | `300.0` | > 0 | no | — | `300.0` | Per-call provider timeout. |

## `[providers]`

Cross-provider policy, distinct from the single default provider's own settings.

| Key | Environment variable | Type | Default | Range | Runtime-changeable | Security | Example | Description |
|---|---|---|---|---|---|---|---|---|
| `providers.allow_remote` | `LOADCOACH_PROVIDERS__ALLOW_REMOTE` | `bool` | `False` | — | no | **config-only:** Permits egress to a remote provider. | `False` | Permit a remote provider at all — an explicit, deliberate opt-in. |

## `[execution]`

``[execution]`` — concurrency, timeout and retry policy for job execution (Phase 4-5).

| Key | Environment variable | Type | Default | Range | Runtime-changeable | Security | Example | Description |
|---|---|---|---|---|---|---|---|---|
| `execution.max_concurrent_jobs` | `LOADCOACH_EXECUTION__MAX_CONCURRENT_JOBS` | `int` | `1` | ≥ 1 | no | — | `1` | Raise only on multi-GPU or CPU-only setups. |
| `execution.default_timeout_seconds` | `LOADCOACH_EXECUTION__DEFAULT_TIMEOUT_SECONDS` | `float` | `300.0` | > 0 | no | — | `300.0` | Per-job execution timeout. |
| `execution.max_attempts` | `LOADCOACH_EXECUTION__MAX_ATTEMPTS` | `int` | `3` | ≥ 1 | no | — | `3` | Attempts before a job is marked failed. |
| `execution.attempt_backoff_seconds` | `LOADCOACH_EXECUTION__ATTEMPT_BACKOFF_SECONDS` | `float` | `2.0` | ≥ 0 | no | — | `2.0` | Delay between retry attempts. |

## `[runtime]`

``[runtime]`` — the default runtime profile every execution resolves against (ADR-0023).

| Key | Environment variable | Type | Default | Range | Runtime-changeable | Security | Example | Description |
|---|---|---|---|---|---|---|---|---|
| `runtime.context_size` | `LOADCOACH_RUNTIME__CONTEXT_SIZE` | `int` | `0` | ≥ 0 | no | — | `0` | Context window to serve, in tokens. 0 leaves it to the provider; a task profile with min_context_tokens sets it explicitly where the provider reports context_configurable. |
| `runtime.kv_cache_precision` | `LOADCOACH_RUNTIME__KV_CACHE_PRECISION` | `str` | `''` | — | no | — | `''` | Empty leaves it to the provider. |
| `runtime.flash_attention` | `LOADCOACH_RUNTIME__FLASH_ATTENTION` | `bool` | `False` | — | no | — | `False` | Empty/false leaves it to the provider. |
| `runtime.keep_alive` | `LOADCOACH_RUNTIME__KEEP_ALIVE` | `str` | `'5m'` | — | no | — | `'5m'` | How long the provider holds a model resident after a call. |
| `runtime.models` | `LOADCOACH_RUNTIME__MODELS` | `dict[str, table]` | `{}` | — | no | — | `{'ollama/qwen3.5:9b-q8_0@sha256:1f3a9c4e2b70': {'context_size': 32768}}` | Per-model runtime overrides, keyed by canonical model ID. |

## `[queue]`

``[queue]`` — job queue depth, leasing and ageing policy (Phase 5).

| Key | Environment variable | Type | Default | Range | Runtime-changeable | Security | Example | Description |
|---|---|---|---|---|---|---|---|---|
| `queue.max_depth` | `LOADCOACH_QUEUE__MAX_DEPTH` | `int` | `1000` | ≥ 1 | no | — | `1000` |  |
| `queue.max_active_per_source` | `LOADCOACH_QUEUE__MAX_ACTIVE_PER_SOURCE` | `int` | `200` | ≥ 0 | no | — | `200` | Active (non-terminal) jobs one source — a token, or an X-Client-Name on loopback — may hold at once; a submission past it is refused with QUEUE_FULL naming the source and the cap (spec §14). 0 disables the per-source cap. |
| `queue.lease_seconds` | `LOADCOACH_QUEUE__LEASE_SECONDS` | `int` | `60` | ≥ 1 | no | — | `60` |  |
| `queue.poll_interval_ms` | `LOADCOACH_QUEUE__POLL_INTERVAL_MS` | `int` | `250` | ≥ 1 | no | — | `250` |  |
| `queue.lease_renewal_interval_seconds` | `LOADCOACH_QUEUE__LEASE_RENEWAL_INTERVAL_SECONDS` | `int` | `20` | ≥ 1 | no | — | `20` | lease_seconds must exceed 3x this plus slack. |
| `queue.ageing_interval_seconds` | `LOADCOACH_QUEUE__AGEING_INTERVAL_SECONDS` | `int` | `30` | ≥ 1 | no | — | `30` |  |
| `queue.max_wait_seconds` | `LOADCOACH_QUEUE__MAX_WAIT_SECONDS` | `int` | `3600` | ≥ 1 | no | — | `3600` |  |
| `queue.ageing_priority_per_minute` | `LOADCOACH_QUEUE__AGEING_PRIORITY_PER_MINUTE` | `float` | `1.0` | ≥ 0 | no | — | `1.0` |  |
| `queue.overflow_allowance` | `LOADCOACH_QUEUE__OVERFLOW_ALLOWANCE` | `int` | `100` | ≥ 0 | no | — | `100` |  |
| `queue.max_affinity_streak` | `LOADCOACH_QUEUE__MAX_AFFINITY_STREAK` | `int` | `5` | ≥ 1 | no | — | `5` |  |
| `queue.idempotency_ttl_hours` | `LOADCOACH_QUEUE__IDEMPOTENCY_TTL_HOURS` | `float` | `24.0` | > 0 | no | — | `24.0` | How long a job's idempotency key stays reserved after enqueue. A key reused after this starts new work rather than replaying an old result (api.md §4, data model §2). |
| `queue.cancelling_watchdog_seconds` | `LOADCOACH_QUEUE__CANCELLING_WATCHDOG_SECONDS` | `int` | `30` | ≥ 1 | no | — | `30` | How long a job may sit in `cancelling` before the scheduler forces it to `cancelled` and records that it did (queue §8, §9). |

## `[routing]`

``[routing]`` — scoring strategy and confidence policy (Phase 3).

| Key | Environment variable | Type | Default | Range | Runtime-changeable | Security | Example | Description |
|---|---|---|---|---|---|---|---|---|
| `routing.strategy` | `LOADCOACH_ROUTING__STRATEGY` | `str` | `'weighted_evidence'` | — | no | — | `'weighted_evidence'` |  |
| `routing.min_confidence` | `LOADCOACH_ROUTING__MIN_CONFIDENCE` | `float` | `0.05` | ≥ 0, ≤ 1 | yes | — | `0.05` |  |
| `routing.prefer_resident_bonus` | `LOADCOACH_ROUTING__PREFER_RESIDENT_BONUS` | `float` | `0.05` | ≥ 0, ≤ 1 | yes | — | `0.05` |  |
| `routing.min_present_weight` | `LOADCOACH_ROUTING__MIN_PRESENT_WEIGHT` | `float` | `0.5` | ≥ 0, ≤ 1 | yes | — | `0.5` |  |
| `routing.remote_cost_factor` | `LOADCOACH_ROUTING__REMOTE_COST_FACTOR` | `float` | `0.9` | > 0, ≤ 1 | yes | — | `0.9` | The cost factor applied to a remote provider's candidates (routing §6). 1.0 is always used for local providers; anything below 1 prefers local at equal capability. |
| `routing.explanation_retention_days` | `LOADCOACH_ROUTING__EXPLANATION_RETENTION_DAYS` | `int` | `0` | ≥ 0 | no | — | `0` | 0 = forever. |

## `[evidence]`

``[evidence]`` — the optional FreeWeight evidence source (Phase 6, ADR-0026).

| Key | Environment variable | Type | Default | Range | Runtime-changeable | Security | Example | Description |
|---|---|---|---|---|---|---|---|---|
| `evidence.freeweight_url` | `LOADCOACH_EVIDENCE__FREEWEIGHT_URL` | `str` | `''` | — | no | **config-only:** An outbound fetch target (ADR-0026 §3). | `''` | Empty means not configured, not unavailable. |
| `evidence.freeweight_api_key_env` | `LOADCOACH_EVIDENCE__FREEWEIGHT_API_KEY_ENV` | `str` | `''` | — | no | **config-only:** A credential; resolved through the secret chain. | `''` | Environment variable naming a bearer token, or empty (ADR-0026). |
| `evidence.freeweight_api_key_file` | `LOADCOACH_EVIDENCE__FREEWEIGHT_API_KEY_FILE` | `str` | `''` | — | no | **config-only:** A credential; resolved through the secret chain. | `''` | Path to a file containing a bearer token, or empty (ADR-0026). |
| `evidence.allowed_source_hosts` | `LOADCOACH_EVIDENCE__ALLOWED_SOURCE_HOSTS` | `tuple[str, Ellipsis]` | `('127.0.0.1', 'localhost', '::1')` | — | no | **config-only:** The outbound fetch allowlist (ADR-0026 §3). | `['127.0.0.1', 'localhost', '::1']` | Fetch allowlist for evidence import URLs (ADR-0026 §3). |
| `evidence.import_interval_hours` | `LOADCOACH_EVIDENCE__IMPORT_INTERVAL_HOURS` | `float` | `24.0` | > 0 | no | — | `24.0` |  |
| `evidence.accept_schema_majors` | `LOADCOACH_EVIDENCE__ACCEPT_SCHEMA_MAJORS` | `tuple[int, Ellipsis]` | `(1,)` | — | no | — | `[1]` | Which `benchmark.evidence_bundle` schema majors this installation reads. May only narrow what this build carries payload models for, never widen it. |

## `[residency]`

``[residency]`` — model unload policy, per device (Phase 5, queue §6).

| Key | Environment variable | Type | Default | Range | Runtime-changeable | Security | Example | Description |
|---|---|---|---|---|---|---|---|---|
| `residency.unload_idle_seconds` | `LOADCOACH_RESIDENCY__UNLOAD_IDLE_SECONDS` | `int` | `900` | ≥ 0 | no | — | `900` |  |
| `residency.max_resident_models` | `LOADCOACH_RESIDENCY__MAX_RESIDENT_MODELS` | `int` | `1` | ≥ 1 | no | — | `1` | Per GPU. |

## `[telemetry]`

``[telemetry]`` — GPU sampling behaviour.

| Key | Environment variable | Type | Default | Range | Runtime-changeable | Security | Example | Description |
|---|---|---|---|---|---|---|---|---|
| `telemetry.interval_ms` | `LOADCOACH_TELEMETRY__INTERVAL_MS` | `int` | `1000` | > 0 | no | — | `1000` |  |
| `telemetry.vram_headroom_bytes` | `LOADCOACH_TELEMETRY__VRAM_HEADROOM_BYTES` | `int` | `536870912` | ≥ 0 | no | — | `536870912` | Per GPU. |

## `[logging]`

Structured-logging behaviour.

| Key | Environment variable | Type | Default | Range | Runtime-changeable | Security | Example | Description |
|---|---|---|---|---|---|---|---|---|
| `logging.level` | `LOADCOACH_LOGGING__LEVEL` | `str` | `'INFO'` | — | no | — | `'INFO'` | Log verbosity. |
| `logging.format` | `LOADCOACH_LOGGING__FORMAT` | `'text' \| 'json' \| 'auto'` | `'auto'` | — | no | — | `'auto'` | text, json, or auto (text on a TTY, json otherwise). |
| `logging.include_content` | `LOADCOACH_LOGGING__INCLUDE_CONTENT` | `bool` | `False` | — | no | **config-only:** Logs full prompts and responses when true. | `False` | Log full prompts and responses. Off by default: only hashes and lengths are logged. |
