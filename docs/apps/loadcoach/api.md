# LoadCoach — Public API

**Base path:** `/api/v1` · **Conventions:** [API and Contract Standards](../../standards/api-and-contract-standards.md)
This is the suite's most externally consumed API: IdeaPress and any third-party tool depend on it.
Everything here is additive within v1, and the committed OpenAPI snapshot is diff-checked in CI.

---

## 1. System

| Endpoint | Purpose |
|---|---|
| `GET /health` | Components: `database`, `provider`, `evidence`, `queue`, `gpu_telemetry` |
| `GET /version` | Application version, API versions, accepted SetSpec schema versions. **Never authenticated** — negotiation precedes credentials ([ADR-0026 §5](../../adr/0026-local-http-hardening.md)) |
| `GET /system/status` | Queue depth by state/class, oldest queued age, dispatch latency, active executions, residency, telemetry snapshot, starvation counter, circuit breakers |
| `GET /system/telemetry/stream` | SSE telemetry |

## 2. Models and task profiles

| Endpoint | Notes |
|---|---|
| `GET /models` | Registry with declared capabilities, evidence summary, reliability, residency |
| `POST /models/discover` | Re-discovery through ModelRack |
| `GET /models/{model_ref}` | Identity, descriptor, evidence per capability with source, age and `match_state`, reliability, circuit-breaker state. `model_ref` is the local ULID or an unambiguous prefix — **not** the canonical ID, which contains `/`, `:` and `@` and does not survive a path segment ([ADR-0024](../../adr/0024-canonical-id-and-model-references.md)) |
| `GET /models?canonical_id=…` | Lookup by identity; `?provider_kind=&provider_model_name=&artifact_digest=` is the exact-triple form |
| `GET /task-profiles` · `GET /task-profiles/{id}` | Definitions with version, weights, constraints, execution and validation policy |

## 3. Routing without execution

### `POST /route`

```json
{
  "task": "code.review",
  "estimated_input_tokens": 12000,
  "max_output_tokens": 2048,
  "constraints": {"requires_capabilities": ["structured_output"]},
  "overrides": null
}
```

Returns the full routing explanation ([Routing §8](routing.md)) **without** executing. Errors:
`TASK_PROFILE_NOT_FOUND`, `NO_ELIGIBLE_MODEL` (with every candidate and rejection reason).

This endpoint is the cheapest way to understand the system, and the one to reach for when a decision
looks wrong.

## 4. Synchronous generation

### `POST /generate`

```json
{
  "task": "content.article_draft",
  "system": "You are drafting one section of an article. Follow every hard requirement…",
  "prompt": "Write a 600-word section on local inference privacy.",
  "messages": null,
  "response_format": null,
  "sampling": {"temperature": 0.7, "max_output_tokens": 1200},
  "constraints": {"max_latency_seconds": 120},
  "overrides": {"model": null, "runtime_profile": null},
  "priority": {"class": "normal"},
  "idempotency_key": "01J9K…"
}
```

Exactly one of `prompt` (+ optional `system`) or `messages` is supplied; supplying both is a
`VALIDATION_ERROR`. `messages` is a list of `{"role": "system"|"user"|"assistant"|"tool",
"content": str, "tool_call_id": str|null}`.

**LoadCoach sends the caller's text to the provider unmodified.** It does not prepend a system prompt
of its own, does not substitute the task profile's wording, and does not rewrite the request. The only
prompt records it applies are the ones it originates — the structured-output corrective retry and the
circuit-breaker re-probe — and each is recorded on the attempt that used it. A caller whose own
provenance records the hash of what it sent (IdeaPress does) can therefore trust that record.

Response `200`:

```json
{
  "job_id": "01J9K…",
  "status": "completed",
  "output": {"text": "…", "structured": null, "tool_calls": []},
  "reasoning": {"available": false, "summary": null, "source": null},
  "model": {"canonical_id": "ollama/qwen3.5:9b-q8_0@sha256:1f3a9c4e2b70",
            "model_ref": "01J9K…",
            "runtime_profile_hash": "8f2c…",
            "served_context": 32768, "served_context_source": "configured",
            "target_gpu_index": 0},
  "routing": {"decision_id": "01J9K…", "rank": 1, "final_score": 0.71,
              "flags": ["low_evidence"], "explanation_url": "/api/v1/jobs/01J9K…/explanation"},
  "usage": {"input_tokens": 812, "output_tokens": 1104, "thinking_tokens": "unsupported"},
  "timing": {"total_ms": 18422, "provider_ms": 18310, "loadcoach_overhead_ms": 112,
             "ttft_ms": 640, "queue_wait_ms": 0},
  "validation": {"performed": false, "passed": null, "attempts": 1},
  "attempts": [{"attempt": 1, "model": "ollama/qwen3.5:9b-q8_0@…", "outcome": "completed"}],
  "degradations": []
}
```

Notes:
* `reasoning` is populated **only** when the provider explicitly returns reasoning content or a
  summary; otherwise `available: false`. LoadCoach never synthesizes or infers hidden chain-of-thought.
* `provider_ms` and `loadcoach_overhead_ms` are always reported separately.
* `idempotency_key` makes a retried POST safe: the same key returns the original job rather than
  creating a second one. Keys are scoped **per caller**, not globally, so two clients cannot collide;
  the caller is the authenticated token's name, or `X-Client-Name` on an unauthenticated loopback
  bind. A key is reserved for `queue.idempotency_ttl_hours` (default 24) and then released, so a key
  reused months later starts new work rather than replaying an old result. On
  `POST /generate/stream`, a repeated key replays the completed job's `result` event rather than
  re-executing.
* A synchronous request that cannot start within its `max_latency_seconds` returns 503
  `INSUFFICIENT_RESOURCES` or 429 `QUEUE_FULL` rather than blocking indefinitely.

### `POST /generate/stream`

Same body; `text/event-stream` response:

```text
event: routing      data: {"schema":"event.envelope","schema_version":"1.0",…,"payload":{…}}
event: token        data: {"delta": "Local ", "index": 0}
event: tool_call    data: {"schema":"event.envelope",…,"payload":{…}}
event: result       data: {"schema":"event.envelope",…,"payload":{…the full response object…}}
```

Every frame carries the SetSpec event envelope **except** `token`, which is bare — the one documented
exception, taken because a five-field envelope per token is roughly a hundred bytes of overhead on the
hottest path in the suite ([ADR-0025 §3](../../adr/0025-envelope-boundaries.md)).

Terminal event is always `result` or `error`. Reconnection with `Last-Event-ID` replays from the
persisted job events.

## 5. Jobs

| Endpoint | Notes |
|---|---|
| `POST /jobs` | Asynchronous submission; same body as `/generate` plus `class`, `priority`, `max_wait_seconds`, `idempotent`. Returns `202` with the job |
| `GET /jobs` | Filter by state, class, task, model, date; cursor pagination |
| `GET /jobs/{id}` | Full job: state, attempts, routing summary, usage, timings, validation, degradations |
| `GET /jobs/{id}/stream` | SSE: state changes, tokens (when streaming was requested), terminal result |
| `POST /jobs/{id}/cancel` | 202, or 409 `JOB_NOT_CANCELLABLE` |
| `GET /jobs/{id}/explanation` | The complete routing explanation |
| `POST /jobs/{id}/feedback` | Caller feedback (see §6) |

Job events: `job.queued`, `job.leased`, `job.admitted`, `job.waiting_resources`, `job.executing`,
`job.token`, `job.validating`, `job.retrying`, `job.fallback`, `job.completed`, `job.failed`,
`job.cancelled`, `job.degraded`.

**Cancellation of a non-streaming execution.** LoadCoach always calls the provider through
`Provider.stream()` and assembles the response itself, even when the caller asked for
`POST /generate` rather than `/generate/stream`. A non-streaming provider call offers no boundary at
which a cancellation token can take effect, so "cancelled within one chunk" would be unachievable for
exactly the requests most likely to be long. Assembling internally costs nothing — the transport is
the same NDJSON either way — and it makes cancellation, the stream-idle timeout and partial-response
preservation uniform across both endpoints. Where a provider cannot stream
(`ProviderCapabilities.streaming` false), the job records the degradation
`cancellation_deferred_to_completion` so the limit is visible rather than assumed away.

## 6. Feedback

### `POST /jobs/{id}/feedback`

```json
{
  "source": "ideapress",
  "accepted": true,
  "quality_score": 0.8,
  "validation": {"passed": true, "detail": null},
  "edited": false,
  "notes": "used verbatim"
}
```

`source` is set by LoadCoach from the authenticated token's name (or `X-Client-Name` on an
unauthenticated loopback bind) and the body's value is ignored when a token is present, so one caller
cannot overwrite another's feedback; with neither a token nor the header, the body's `source` is used,
and `anonymous` failing that. Idempotent per `(job_id, source)`; a second call from the same
source updates the existing record. Returns `201` with the stored record on a source's first feedback
for a job and `200` on an update; every source's record is also listed under `feedback` in
`GET /jobs/{id}`. Requires `write`. Accepted for any existing job — feedback on a job that has not
run yet is kept and attributed once it has. Feeds the
`reliability_factor` and regression detection ([Routing §11](routing.md)). Never mutates benchmark
evidence — production and benchmark evidence remain separate sources.

## 7. Evidence

| Endpoint | Notes |
|---|---|
| `POST /evidence/import` | Body: a SetSpec `benchmark.evidence_bundle`, or `{"url": "http://127.0.0.1:8765"}` to pull from FreeWeight. Returns counts imported / updated / **unmatched** / rejected with reasons. The URL form obeys the fetch allowlist in [ADR-0026 §3](../../adr/0026-local-http-hardening.md) — scheme, `evidence.allowed_source_hosts` (loopback only by default), literal-IP, redirect and size checks — and returns `EVIDENCE_SOURCE_REFUSED` when a URL fails them |
| `GET /evidence` | Imported evidence, filterable by capability, model, `match_state`, minimum confidence, staleness. A **collection** envelope (`items`/`page`) whose items are `capability.evidence` SetSpec envelopes ([ADR-0025 §2](../../adr/0025-envelope-boundaries.md)), plus a `summary` object — the same store overview `GET /evidence/sources`, the Benchmarks page, `/health`'s `evidence` component and every routing explanation carry, so the four cannot disagree |
| `GET /evidence/sources` | Configured and observed sources with last import time, schema version and status |
| `GET /reliability` | Production evidence per (model, task profile): the `7d`, `30d` and `all` window statistics, each value with the sample count behind it and a reason when absent; the `reliability_factor` routing applies with its inputs; the regression verdict against the model's own baseline; and the circuit breaker's persisted state. Filter by `task` and `model` |

An unsupported schema major is rejected with `SCHEMA_VERSION_UNSUPPORTED` naming both versions;
existing evidence is untouched — the version is decided *before* the transaction opens, so a
rejected bundle cannot have written a source row, let alone a record. Import is `admin`-scoped.

`summary.status` is one of `not_configured`, `none`, `ok`, `unreachable`, `refused` or `failed`,
and `summary.note` says the same thing in a sentence. `not_configured` (`[evidence]
freeweight_url` is empty) is a **healthy** state and reads differently from `unreachable`: the
first means nobody asked for evidence, the second means the last import is retained and marked
stale while routing continues on it.

Import never fails because a model has not been discovered. Evidence for an unknown model, or
`name_only` evidence against a locally-digested model, is **retained** with a `match_state` and
counted separately in the response; it is bound automatically when discovery next produces a match,
and it never contributes to a routing score until it is
([ADR-0022 §4](../../adr/0022-capability-evidence-record-contract.md)).

## 8. Queue

| Endpoint | Notes |
|---|---|
| `GET /queue` | Depth by state and class, oldest queued age, dispatch latency, starvation counter |
| `POST /queue/pause` · `POST /queue/resume` | Admin: stop or resume dispatch without dropping jobs |
| `POST /queue/drain` | Admin: finish in-flight work and stop claiming new jobs (for a clean shutdown) |

## 9. Settings

`GET /settings`, `PUT /settings` — runtime-changeable settings only. Security-relevant keys are
config-only and return 403 `FORBIDDEN` naming the key.

The runtime-changeable set is a registry (`loadcoach.services.settings.RUNTIME_SETTINGS`), shared by
the API, the Settings page and the scheduler that applies a change within a second: `queue.paused`,
`queue.draining`, `routing.prefer_resident_bonus`, `routing.min_present_weight`,
`routing.min_confidence`, `routing.remote_cost_factor`, `storage.content_retention_hours`. `GET`
returns every key's effective value, its definition and bounds, the configured value it overrides,
and the list of config-only keys. A key that is neither runtime-changeable nor security-relevant is
`400 VALIDATION_ERROR` naming it and listing the set. `PUT` is `admin`-scoped.

## 10. Errors

Standard envelope. Codes as listed in the [spec §13](spec.md), with these presentation rules:

* `NO_ELIGIBLE_MODEL` always includes every candidate and its rejection reason in `details`.
* `INSUFFICIENT_RESOURCES` includes the estimate and what was free.
* `VALIDATION_FAILED` includes the failing field paths and the attempt count.
* `ALL_CANDIDATES_FAILED` includes each attempt with its model and error.

## 11. Authentication

Loopback with no tokens: open. Otherwise `Authorization: Bearer <token>` with scopes:

Scopes are **cumulative**: `admin` ⊃ `write` ⊃ `read`. A token carries one scope, and holding it
grants every scope beneath it — so a `write` token needs no separate `read` grant to poll the job it
just submitted.

| Scope | Grants |
|---|---|
| `read` | health, status, models, task profiles, jobs (read), explanations, evidence (read), queue (read) |
| `write` | everything `read` grants, plus `/route`, `/generate`, `/jobs`, cancel, feedback |
| `admin` | everything `write` grants, plus settings, evidence import, queue control, token management |

`GET /version` requires no scope at all.

Per-token rate limits and queue-depth caps prevent one caller from starving others.

## 12. Client guidance (IdeaPress and others)

1. `GET /api/v1/version` on first contact; verify the API major; cache with a TTL.
2. Prefer `/generate` for interactive work and `/jobs` for background work.
3. Always send an `idempotency_key` for non-idempotent submissions.
4. Propagate `X-Request-ID` so a trace spans both applications, and set `X-Client-Name` so jobs and
   feedback are attributed to you on an unauthenticated loopback deployment.
5. Treat LoadCoach being unreachable as **degraded**: fall back to a direct provider, or fail the
   stage with a clear message if the user pinned LoadCoach.
6. Send feedback — it is what makes routing improve.
7. Read `routing.flags`: `low_evidence` means the decision was made with little measured evidence, and
   is worth surfacing to a user who is wondering why a model was chosen. `assumed_context` means the
   served context could not be established and was taken from the model's advertised maximum.
8. Send `system` and `prompt` (or `messages`) as the text you want the model to see. LoadCoach does
   not modify it, so your own prompt-version provenance stays true.
