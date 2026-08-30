# LoadCoach — Specification

**Type:** Application · **Import/distribution name:** `loadcoach` · **Default port:** 8766 · **Env prefix:** `LOADCOACH_`
**Status:** Specified, not implemented. Corrected 2026-08-21 by the
[final architecture audit](../../reviews/final_architecture_audit.md) (ADR-0022–0027, ADR-0029).
**Related:** [Routing](routing.md) · [Queue and Scheduling](queue-and-scheduling.md) · [API](api.md) · [Data Model](data-model.md) · [Development Plan](development-plan.md) · [Risks](risks.md)

---

## 1. Purpose

Answer: *given this task, the models installed on this machine, what the hardware is doing right now,
and whatever measured evidence exists — which model should do the work, how should it be run, and
why?* Then run it, validate the result, retry or fall back when necessary, and be able to explain
every decision afterwards.

## 2. Scope

* Task profiles: named routing intents with hard constraints and capability weights.
* Model registry: what is available, what is resident, what it costs to run.
* Evidence: import of FreeWeight capability evidence; declared capabilities as a fallback; production
  evidence from executed jobs.
* Routing: constraint filtering, scoring, candidate ranking, fallback ordering, explanation.
* Queue and scheduling: priorities, job classes, leases, concurrency, ageing, cancellation, timeouts,
  recovery.
* Execution: prompt/runtime assembly, provider call, streaming, context budgeting, result capture.
* Validation: JSON, JSON Schema, required fields, regex, length; retry and fallback policy.
* Feedback: acceptance and quality signals from calling applications, folded into reliability.
* Web UI, CLI, and a public HTTP API for other applications.

## 3. Explicit non-goals

* **No benchmark execution or scoring.** LoadCoach consumes evidence; it never measures capability.
  If a capability number is needed and none exists, it routes without it and says so.
* **No content workflows.** It executes a request; it does not decide what to write.
* No model training, fine-tuning or downloading.
* No multi-machine scheduling (single machine; a future extension).
* No general agent loop. Tool *calls* are returned to the caller; LoadCoach does not execute tools.
* No access to FreeWeight's or IdeaPress's database.
* No dependency on FreeWeight to start or to route.
* No broker, no Redis, no Celery ([ADR-0010](../../adr/0010-queue-implementation.md)).

## 4. Responsibilities

| Area | Responsibility |
|---|---|
| Task profiles | Definition, versioning, capability weights, hard constraints, defaults, validation policy |
| Registry | Model discovery via ModelRack; declared capabilities; residency; resource cost estimates |
| Evidence | Import bundles; apply confidence and freshness; combine with production evidence |
| Routing | Filter → score → rank → select → fallback chain, with a persisted explanation for every decision |
| Queue | Persistent jobs, priorities, classes, leases, ageing, cancellation, recovery |
| Admission | Refuse or defer work the machine cannot currently run, with a reason |
| Execution | Assemble, call, stream, budget context, capture results and usage |
| Validation | Structured-output and content checks per task profile |
| Resilience | Retry policy, fallback candidates, timeouts, circuit-breaking on a failing model |
| Feedback | Accept caller feedback; update reliability; detect regressions |
| Explainability | Every decision reconstructable, forever |
| Interfaces | Web UI, CLI, public API, SSE streams |

## 5. Dependencies

**Suite:** `baseaicore`, `setspec`, `modelrack`, `sweatmeter`, `weightsdb`, `mirrorwall`.
**Third party:** `fastapi`, `uvicorn[standard]`, `typer`, `pydantic`, `pydantic-settings`,
`sqlalchemy`, `alembic`, `jinja2`, `httpx` (for the optional FreeWeight evidence client).
**External services:** a model provider (Ollama by default). **Optional:** FreeWeight, for evidence.

**Required at startup:** none. LoadCoach starts and serves with no provider and no evidence,
reporting degraded health.

## 6. Consumers

* **IdeaPress** — via `POST /api/v1/generate`, `/jobs`, and the SSE streams.
* **External tools** — the same public API.
* **Users** — web UI and CLI.

## 7. Public APIs

### 7.1 HTTP (`/api/v1`, full detail in [API](api.md))

```text
GET  /health                     GET  /version                  GET  /system/status
GET  /system/telemetry/stream    GET  /models                   POST /models/discover
GET  /models/{model_ref}         GET  /task-profiles            GET  /task-profiles/{id}
POST /route                      POST /generate                 POST /generate/stream
POST /jobs                       GET  /jobs                     GET  /jobs/{id}
GET  /jobs/{id}/stream           POST /jobs/{id}/cancel         POST /jobs/{id}/feedback
GET  /jobs/{id}/explanation      GET  /queue                    GET  /evidence
POST /evidence/import            GET  /evidence/sources         GET  /reliability
GET  /settings                   PUT  /settings
```

`POST /route` performs routing **without executing** — the "explain what you would do" endpoint, and
the cheapest way for a caller to understand the system.

### 7.2 CLI

```text
loadcoach serve | health | doctor | version
loadcoach config show|validate|init|path        loadcoach db upgrade|status|backup|restore
loadcoach models list|show|refresh|residency    loadcoach tasks list|show|validate
loadcoach route explain --task … [--prompt-file …]
loadcoach generate --task … [--prompt-file …] [--stream]
loadcoach job submit|list|show|cancel|wait|feedback
loadcoach queue status|drain|pause|resume
loadcoach evidence import|show|sources|refresh
loadcoach reliability show                      loadcoach token create|list|revoke
```

## 8. Inputs

Generation requests (task, prompt or messages, optional schema, constraints, priority, overrides),
task-profile definitions, FreeWeight evidence bundles, declared model capabilities, live telemetry,
caller feedback, configuration.

## 9. Outputs

Generated text or structured output, tool-call requests, provider-exposed reasoning metadata **only
when the provider returns it explicitly**, token usage, timing (provider time and LoadCoach overhead
separately), model identity, routing metadata and explanation, validation status, job records and
events, typed errors.

**Reasoning content:** LoadCoach never asks for, synthesizes or infers hidden chain-of-thought. Where
a provider explicitly returns a reasoning summary or thinking field, it is passed through and
labelled with its origin; where it does not, the field is `unsupported`.

**Caller prompts are passed through unmodified.** A caller's `prompt`/`messages` are sent to the
provider as given. LoadCoach's own prompt records are used for exactly two things, both of which it
originates rather than relays:

* the corrective instruction appended on a structured-output retry
  ([Queue §7](queue-and-scheduling.md)), and
* the re-probe request a circuit breaker issues.

LoadCoach never prepends a system prompt of its own to a caller's request, never rewrites one, and
never substitutes a task profile's wording for the caller's. A task profile carries routing intent
and execution parameters; it does not carry a prompt ([Routing §2](routing.md)). This matters most to
IdeaPress, whose per-attempt provenance records the `prompt_id`, `version` and `sha256` of what it
sent — a record that would be a lie if LoadCoach altered the text. Every prompt record LoadCoach does
apply is recorded on the attempt that used it, so the job history shows exactly what the model saw.

**Every response names its execution subject**: the selected model, the resolved
`runtime_profile_hash`, the served context and its source, and the target GPU index
([ADR-0023](../../adr/0023-runtime-profile-resolution.md),
[ADR-0027](../../adr/0027-multi-gpu-semantics.md)).

## 10. Data ownership

Owns `loadcoach.sqlite3` exclusively: models, model_capabilities, runtime_profiles, task_profiles,
capability_evidence (imported), evidence_sources, jobs, job_attempts, job_events, routing_decisions,
routing_candidates, validations, feedback, reliability_stats, residency, api_tokens, settings.
See [Data Model](data-model.md).

LoadCoach holds no `machines` table. It knows exactly one machine — its own, whose fingerprint comes
from SweatMeter at startup — and it compares imported evidence's `machine_fingerprint` against that
single value. Evidence measured elsewhere is retained with a machine badge and is not used for
performance, memory or energy constraints
([ADR-0017](../../adr/0017-benchmark-confidence-and-freshness.md)).

## 11. Public contracts

1. **Generation contract.** `POST /generate` returns a result plus routing metadata and usage; the
   response shape is stable within `/api/v1`.
2. **Explanation contract.** Every job and every `/route` call yields a routing explanation with
   candidates, scores, rejections and fallbacks — retrievable for the lifetime of the job.
3. **Evidence contract.** `POST /evidence/import` accepts SetSpec `benchmark.evidence_bundle`;
   unsupported majors are rejected with both versions named.
3a. **Subjective-evidence contract.** A `user.*` capability — FreeWeight's user-authored goal
   evidence ([ADR-0032](../../adr/0032-judge-validity-and-user-capability-namespace.md)) — is
   **opt-in**: it is never weighted unless a task profile names it explicitly, because a capability
   that one person's taste defines must not acquire routing influence merely by existing. Its
   `judge_validity_factor` is applied as part of the confidence LoadCoach receives, never
   recomputed. Any explanation that used one names the goal, its `kappa_w` and its `n_holdout` in
   words. `goal_hash` and judge set identity are hard separations, handled like a benchmark version.
4. **Feedback contract.** `POST /jobs/{id}/feedback` accepts an acceptance signal and optional
   quality/validation detail; it is idempotent per `(job_id, source)`.
5. **Streaming contract.** SSE per [API Standards §8](../../standards/api-and-contract-standards.md),
   with `token` deltas and a terminal `result` event.
6. **Degradation contract.** No evidence, no FreeWeight, or no GPU each produce a documented degraded
   behaviour, never a failure to serve.

## 12. Configuration

`~/.config/loadcoach/config.toml`, `LOADCOACH_*` environment variables, CLI flags, per
[Configuration Standards](../../standards/configuration-standards.md) — defaults, then file, then
environment, then CLI, field by field. Principal sections:

```toml
[server]      host = "127.0.0.1"  port = 8766  allow_lan_exposure = false
[storage]     database_url = "sqlite:///<data>/loadcoach.sqlite3"  auto_migrate = true
              content_retention_hours = 24   # finished jobs keep text this long; runtime-changeable
              retain_content = false         # keep text for ever; config-only (§14)
[provider]    kind = "ollama"  base_url = "http://127.0.0.1:11434"  timeout_seconds = 300
[providers]   allow_remote = false
[server]      allowed_hosts = []           # required when host is not loopback (ADR-0026)
              rate_limit_per_minute = 600  rate_limit_burst = 100  failed_auth_per_minute = 20
              max_body_bytes = 16777216    # 413 before buffering (Security Standards §14)
[execution]   max_concurrent_jobs = 1      # raise only on multi-GPU or CPU-only setups
              default_timeout_seconds = 300  max_attempts = 3  attempt_backoff_seconds = 2
[runtime]     # the default runtime profile every execution resolves against (ADR-0023)
              context_size = 0             # 0 = leave to the provider; any task profile with
                                           # min_context_tokens sets it explicitly where the
                                           # provider reports context_configurable
              kv_cache_precision = ""  flash_attention = false  keep_alive = "5m"
[runtime.models."ollama/qwen3.5:9b-q8_0@sha256:1f3a9c4e2b70"]   # optional per-model override
              context_size = 32768
[queue]       max_depth = 1000  max_active_per_source = 200  lease_seconds = 60  poll_interval_ms = 250
              lease_renewal_interval_seconds = 20   # lease_seconds must exceed 3x this + slack
              ageing_interval_seconds = 30
              max_wait_seconds = 3600  ageing_priority_per_minute = 1
              overflow_allowance = 100  max_affinity_streak = 5
[routing]     strategy = "weighted_evidence"  min_confidence = 0.05
              prefer_resident_bonus = 0.05  min_present_weight = 0.5
              explanation_retention_days = 0    # 0 = forever
[evidence]    freeweight_url = ""          # empty = not configured, not "unavailable"
              freeweight_api_key_env = ""  # or freeweight_api_key_file (ADR-0026)
              allowed_source_hosts = ["127.0.0.1", "localhost", "::1"]
              import_interval_hours = 24  accept_schema_majors = [1]
[residency]   unload_idle_seconds = 900  max_resident_models = 1   # per GPU
[telemetry]   interval_ms = 1000  vram_headroom_bytes = 536870912  # per GPU
[logging]     level = "INFO"  include_content = false
```

## 13. Error behaviour

```text
NO_ELIGIBLE_MODEL         VALIDATION_FAILED          QUEUE_FULL
TASK_PROFILE_NOT_FOUND    STRUCTURED_OUTPUT_INVALID  JOB_NOT_FOUND
MODEL_NOT_FOUND           INSUFFICIENT_RESOURCES     JOB_NOT_CANCELLABLE
PROVIDER_UNAVAILABLE      CONTEXT_LIMIT_EXCEEDED     MAX_WAIT_EXCEEDED
PROVIDER_TIMEOUT          CAPABILITY_UNSUPPORTED     EVIDENCE_IMPORT_FAILED
PROVIDER_PROTOCOL_ERROR   ALL_CANDIDATES_FAILED      SCHEMA_VERSION_UNSUPPORTED
PROVIDER_REJECTED         GENERATION_CANCELLED       EVIDENCE_SOURCE_REFUSED
```

Every error ModelRack can raise has exactly one mapping here, so no provider failure reaches a caller
as `INTERNAL_ERROR`:

| ModelRack error | LoadCoach code | HTTP |
|---|---|---|
| `ProviderUnavailable` | `PROVIDER_UNAVAILABLE` | 503 |
| `ProviderTimeout` | `PROVIDER_TIMEOUT` | 504 |
| `ProviderProtocolError` | `PROVIDER_PROTOCOL_ERROR` | 502 |
| `ProviderRejected` | `PROVIDER_REJECTED` (provider message preserved in `details`) | 502 |
| `ModelNotFound` | `MODEL_NOT_FOUND` | 404 |
| `ContextLimitExceeded` | `CONTEXT_LIMIT_EXCEEDED` | 422 |
| `CapabilityUnsupported` | `CAPABILITY_UNSUPPORTED` | 422 |
| `GenerationCancelled` | `GENERATION_CANCELLED` — terminal, never retried | 200 with a cancelled job |

`EVIDENCE_SOURCE_REFUSED` is returned when an import URL fails the fetch allowlist
([ADR-0026 §3](../../adr/0026-local-http-hardening.md)); it is distinct from
`EVIDENCE_IMPORT_FAILED`, which means the bundle itself was unusable.

Behavioural rules:
* `NO_ELIGIBLE_MODEL` always lists every candidate and the constraint that rejected it.
* A job whose resources are temporarily unavailable **waits** (with a reason) until `max_wait_seconds`,
  then fails explicitly.
* Retries and fallbacks are recorded per attempt; a fallback is never silent.
* A model failing repeatedly is temporarily deprioritized (circuit-breaking) with the reason visible.
* Cancellation is honoured within one stream chunk.

## 14. Security considerations

* Loopback default; non-loopback requires tokens plus the exposure acknowledgement — this is the
  application most likely to be exposed on a LAN, so its startup refusal matters most.
* Scopes: `read` (status, models, explanations), `write` (submit, cancel, feedback), `admin`
  (settings, evidence import, queue control).
* Prompt and response text is retained for `[storage] content_retention_hours` (24 by default)
  after a job finishes, then removed by a sweep — the hashes, token counts, timings, model,
  decision and events stay, so the job remains explicable (the M5-14 reconciliation: a queued job
  needs its transcript to run, and a caller polling a finished job expects its output for a
  while). A scrubbed job says "content removed by retention" wherever the text would have shown.
  `retain_content = true` keeps text for ever and is config-only. Note that a
  `loadcoach db backup` taken before the sweep is a byte copy: it keeps the text for as long as
  the backup exists, outside the sweep's reach.
* Imported evidence is untrusted input: schema-validated, size-limited, provenance-checked; an import
  can never execute anything or alter task profiles.
* An import that names a **URL** is a fetch LoadCoach performs on a caller's behalf, so it obeys the
  scheme, host-allowlist (loopback only by default), literal-IP, redirect and size rules in
  [ADR-0026 §3](../../adr/0026-local-http-hardening.md). A credential configured for one evidence
  source is never sent to any other host.
* `Host` header allowlist on every request, before routing and before authentication; non-loopback
  binding additionally requires `server.allowed_hosts`. This is the application most likely to be
  exposed, so it is also the one most exposed to DNS rebinding when it is not.
* Tool definitions supplied by callers are passed to the provider and returned; **LoadCoach never
  executes a tool call**.
* Per-token rate limits and queue depth caps prevent a single caller from starving others.
* Remote providers require explicit opt-in and are marked as egress in the UI and in every routing
  explanation that selects one.

## 15. Performance considerations

| Measure | Target | Ceiling |
|---|---|---|
| Enqueue (accepted → committed) | ≤ 15 ms | 50 ms |
| Dispatch latency (eligible → executing), idle worker | ≤ 100 ms | 500 ms |
| Routing decision, 20 candidates, warm evidence cache | ≤ 20 ms | 100 ms |
| Routing decision, cold cache | ≤ 150 ms | 500 ms |
| Execution overhead excluding provider time | ≤ 15 ms | 50 ms |
| Added latency per streamed chunk | ≤ 5 ms | 20 ms |
| Cancellation acknowledged (queued / executing) | ≤ 50 ms / ≤ 1 s | 200 ms / 5 s |
| Queue poll CPU at idle | ≤ 0.5 % of a core | 2 % |
| Recovery of 1 000 in-flight jobs at startup | ≤ 2 s | 10 s |

Provider time and LoadCoach overhead are always reported separately, in the response and in the job
record.

## 16. Cross-platform considerations

Linux tier 1. On other platforms the queue, routing, execution and API work; VRAM-based admission
control degrades to RAM-only or to unconstrained with a recorded reason, and residency management is
disabled where the provider cannot report it.

## 17. Observability

* Structured logs with `request_id`, `job_id`, `attempt`, `task_profile_id`, `model_canonical_id`.
* Persisted job events with SSE replay.
* Health components: `database`, `provider`, `evidence`, `queue`, `reliability` (degraded when a
  model's recent validated-success rate has regressed against its own baseline, naming the pair
  and the numbers — [Routing §11](routing.md)). GPU telemetry is deliberately not a health
  component: a machine without a GPU is not unhealthy (ADR-0016 — absence is `UNSUPPORTED`,
  never a failure), and the readings live on `GET /system/status` and the System page.
* `GET /api/v1/system/status`: queue depth by state and priority, oldest queued age, active
  executions, residency, telemetry snapshot, dispatch latency, starvation counter.
* Every routing decision persisted in full — 100 % of decisions, not a sample.

## 18. Test strategy

| Layer | Coverage |
|---|---|
| Unit | Scoring maths; constraint filtering; ageing; lease expiry; context budgeting; validation; retry/fallback policy; circuit-breaker state |
| Contract | Evidence import against SetSpec goldens (including an unsupported major); API against the OpenAPI snapshot; error codes; streaming shape |
| Integration | Queue with a real database (both dialects); recovery after simulated crash; concurrency limits; execution against `FakeProvider` |
| E2E | Submit → route → execute → validate → feedback, over HTTP and CLI; streaming with disconnect and replay |
| Failure-path | No evidence; stale evidence; FreeWeight unreachable; provider down mid-job; insufficient VRAM; all candidates fail; queue full; max wait exceeded |
| Scheduling simulation | Deterministic simulator over a fake clock: starvation bounds, priority ordering, fairness, ageing, throughput under mixed classes |
| Performance | Every budget in §15 |
| Security | Auth required and scoped; oversize import; rate limits; no tool execution; no secret in logs |
| Live (marked) | Real Ollama: route, execute, stream, cancel, residency |

## 19. Compatibility and versioning

* Application semver; API `v1`; SetSpec versions independent.
* Task profile definitions are versioned; a job records the profile version it used, so an
  explanation remains truthful after the profile changes.
* Every job records its resolved `runtime_profile_hash`, served context and served-context source, so
  an explanation remains truthful after the runtime configuration changes.
* Routing strategy and confidence policy versions are recorded on every decision.
* Evidence import accepts a configured set of schema majors and rejects others explicitly.

## 20. Acceptance criteria

1. `pip install loadcoach && loadcoach serve` works with only Ollama running; no configuration and
   **no FreeWeight**.
2. `POST /api/v1/generate {"task": "content.article_draft", "prompt": "…"}` selects a model, executes
   it, and returns the result with routing metadata, usage and timings.
3. With no benchmark evidence at all, routing still works from declared capabilities and says
   `evidence: none` in the explanation.
4. Importing a FreeWeight evidence bundle changes routing in a way the explanation makes visible.
5. Every job has a complete, retrievable routing explanation: candidates, scores, rejections,
   fallbacks, confidence and evidence age.
6. The queue survives a kill -9 with no lost, duplicated or stuck job.
7. A low-priority job's wait is bounded by the ageing policy — proven by the scheduling simulation.
8. Cancellation stops an executing job within one chunk boundary and leaves no resident model orphaned.
9. Structured output is validated; a schema failure retries or falls back per policy and is recorded.
10. Insufficient VRAM defers the job with a reason rather than failing or thrashing, and the reason
    names the device it evaluated.
10a. Evidence measured under a different runtime profile than the one LoadCoach resolves is **not**
    used, is named in the explanation with both hashes, and the explanation suggests the FreeWeight
    invocation that would produce matching evidence.
10b. A task profile requiring 16 384 context tokens does not select a model the provider will serve
    at 4 096, and where the served context can only be assumed the decision says so.
11. Full test suite passes with no GPU, no Ollama, no FreeWeight and no network.
12. All LoadCoach gold standards in [Gold Standards §2](../../standards/gold-standards.md) are met.

## 21. Future extensions

* Multi-machine execution (workers on other hosts) — the `JobQueue` port is the seam.
* Exploration routing (deliberate occasional non-greedy choices to gather production evidence).
* Learned routing fitted on production outcomes, behind the existing `RoutingStrategy` port.
* Batch/affinity scheduling: grouping jobs by model to amortize load time.
* Cost-aware routing for remote providers.
* Speculative execution of a cheap model with escalation on validation failure.
* A `LoadCoachClient` package, once a second external consumer exists
  ([ADR-0011](../../adr/0011-shared-package-boundaries.md)).
