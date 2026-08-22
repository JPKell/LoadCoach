# LoadCoach — Development Plan

**Sequence position:** second application. Begins after **FreeWeight P11 (M3, 1.0-rc)**, so that real
evidence bundles exist to build against.
**Milestones:** M4 (beta) at P6 · **M5 (LoadCoach 1.0) at P9**.
**Also produces:** WeightsDB (extracted at P1), MirrorWall and `setspec.prompts` (extracted at P4).
**Corrected 2026-08-21** by the [final architecture audit](../../reviews/final_architecture_audit.md):
P3's resource constraints, P4's runtime-profile resolution, P5's queue mechanics.

---

## Phase 1 — Skeleton, storage and the WeightsDB extraction

**Goal:** LoadCoach serves, has a migrated database of its own, and does it on the newly extracted
WeightsDB — proving the package works for a second schema.

**Prerequisites:** FreeWeight P11; `baseaicore`, `setspec`, `modelrack`, `sweatmeter`.

**Work**
* Repository skeleton; settings with precedence and unsafe-combination refusals; logging;
  request IDs; health and version endpoints; CLI skeleton.
* **Extract WeightsDB** ([WeightsDB Phase 1–2](../../packages/weightsdb/development-plan.md)) from
  FreeWeight's storage layer, generalizing as it moves.
* LoadCoach models and migration `0001`: `models`, `model_capabilities`, `runtime_profiles`,
  `task_profiles`, `settings`, `api_tokens`.
* `Host` validation and the non-loopback refusal set — bind + token + acknowledgement +
  `server.allowed_hosts` ([ADR-0026](../../adr/0026-local-http-hardening.md)) — from the first phase,
  as FreeWeight does, because this is the application most likely to be exposed.
* `loadcoach db upgrade|status|backup|restore`.

**Files/subsystems**
```text
src/loadcoach/{__main__,__about__,config,bootstrap}.py
src/loadcoach/infrastructure/db/{models,repositories/*,migrations/**}.py
src/loadcoach/web/{app,routes/system}.py  src/loadcoach/cli/{main,commands/{system,config,db}}.py
py/WeightsDB/**            (new repository)
tests/integration/test_migrations.py  tests/e2e/test_server_boot.py
```

**Tests**
* Config precedence and refusals; health shape; CLI exit codes.
* Migrations on both dialects; parity check; failure + restore.
* WeightsDB integration test: two schemas (FreeWeight's and LoadCoach's) side by side.

**Acceptance criteria**
1. `loadcoach serve` starts with zero configuration and reports degraded health with no provider.
2. LoadCoach's database migrates on SQLite and PostgreSQL through WeightsDB.
3. `weightsdb 0.2.0` is published and FreeWeight's adoption checklist is written.

**Known risks:** extracting FreeWeight-shaped assumptions. **Likely failure modes:** pragmas lost;
version-table naming collisions. **Gold standards:** shared plumbing, zero shared schema.
**Deferred:** routing, queue, execution.

---

## Phase 2 — Registry and task profiles

**Goal:** `loadcoach models list` and `loadcoach tasks list` show the real registry and the shipped
task profiles, with declared capabilities and constraints validated.

**Prerequisites:** P1.

**Work**
* Discovery through ModelRack; registry service; declared-capability extraction; availability tracking.
* Task profile definitions as configuration, imported into `task_profiles` with versioning and
  validation (weights sum to 1.0; capabilities exist in the SetSpec vocabulary; schema references
  resolve).
* Manual capability scores from configuration, marked `source: manual`.
* `GET /models`, `GET /task-profiles`; CLI equivalents; first UI pages (plain, pre-MirrorWall).

**Files/subsystems**
```text
src/loadcoach/domain/{task_profile,registry}.py  src/loadcoach/services/{models,task_profiles}.py
src/loadcoach/config/task_profiles.toml  src/loadcoach/web/routes/{models,task_profiles}.py
tests/unit/{test_task_profile_validation,test_registry}.py
```

**Tests**
* Invalid profiles rejected at startup: weights not summing to 1.0, unknown capability, missing schema
  file, contradictory constraints.
* Discovery idempotent; unavailable models flagged with a reason, not deleted.
* Declared capabilities extracted correctly from Ollama fixtures and from an OpenAI-compatible
  provider (fewer flags, honestly reported).

**Acceptance criteria**
1. All **fifteen** shipped task profiles load and validate, including `content.review` — the prose
   review intent IdeaPress's audit stages route to ([Routing §2](routing.md)).
2. Models list shows declared capabilities and availability with reasons.
3. A malformed profile refuses startup with the file, key and problem named.

**Known risks:** profile sprawl. **Likely failure modes:** silently ignoring an unknown capability.
**Gold standards:** validated configuration; honest availability. **Deferred:** evidence, routing.

---

## Phase 3 — Routing without evidence, and `/route`

**Goal:** `POST /api/v1/route` and `loadcoach route explain` produce a complete, correct explanation
using declared capabilities and manual scores — with **no FreeWeight in the picture at all**.

**Prerequisites:** P2; SweatMeter for resource constraints.

> **Sequencing note.** The VRAM constraint in [Routing §4](routing.md) needs the estimate specified in
> [Queue §5](queue-and-scheduling.md), which was scheduled for P5. The **estimator** therefore lands
> here, as a pure function over a descriptor, a runtime profile and a telemetry snapshot — which is
> exactly what a constraint filter needs and what a unit test can drive. P5 adds the *admission
> policy* around it: deferral, re-evaluation, the `waiting_resources` state and the aggregate check.
> Splitting estimation from policy this way avoids a throwaway constraint in P3.

**Work**
* **Runtime profile resolution** and the `served_context` derivation
  ([ADR-0023](../../adr/0023-runtime-profile-resolution.md)); every candidate is an execution subject,
  and `runtime_profiles` rows are written here.
* Pure scoring domain: constraint filter, capability scoring with the absent-evidence rule, adjustment
  factors (reliability neutral for now), ranking with a total order, fallback selection.
* VRAM/KV estimator as a pure function, evaluated **per GPU** and never summed
  ([ADR-0027](../../adr/0027-multi-gpu-semantics.md)).
* Context estimation and budgeting against `served_context`.
* Explanation assembly and persistence (`routing_decisions`, `routing_candidates`).
* `POST /route`; `loadcoach route explain`; a Routing page rendering the explanation readably.

**Files/subsystems**
```text
src/loadcoach/domain/routing/{constraints,scoring,ranking,explanation,context_budget}.py
src/loadcoach/services/routing.py  src/loadcoach/web/routes/routing.py
tests/unit/test_routing_*.py  tests/integration/test_route_endpoint.py
```

**Tests**
* Each hard constraint rejects for the right reason with the right numbers.
* Absent evidence excluded from numerator **and** denominator; never scored 0.
* `low_evidence` flag raised below the present-weight floor.
* Determinism: identical inputs ⇒ identical decision (golden test).
* Total ordering: no tie is resolved nondeterministically.
* `NO_ELIGIBLE_MODEL` lists every candidate with its rejection reason.
* Context budgeting: fits, reduces output tokens where permitted, rejects with numbers otherwise;
  never truncates input silently.
* A model advertising 131 072 tokens whose resolved profile serves 4 096 is rejected by
  `context_too_small` for a profile requiring 16 384 — **not** admitted and truncated by the provider.
* Where the served context can only be assumed, the decision carries `assumed_context`.
* On a two-GPU fixture, a model larger than either device's free VRAM but smaller than their sum is
  rejected, with the per-device numbers in the rejection detail.

**Acceptance criteria**
1. With no benchmark evidence, `/route` selects a sensible model and the explanation says
   `evidence: none`.
1a. Every decision names its resolved `runtime_profile_hash`, its `served_context` and that context's
   source.
2. Every routing decision is persisted and retrievable.
3. A decision is reproducible from its stored inputs.

**Known risks:** priors that are arbitrary. Mitigated by low fixed confidence and explicit `source`
labelling. **Likely failure modes:** absent evidence treated as zero; nondeterministic ordering.
**Gold standards:** explainable routing; deterministic scoring; works with no FreeWeight.
**Deferred:** execution, queue, evidence import.

---

## Phase 4 — Execution, streaming, validation, and the MirrorWall extraction

**Goal:** `POST /generate` routes, executes, streams and validates — end to end, synchronously — and
the UI runs on the newly extracted MirrorWall.

**Prerequisites:** P3.

**Work**
* Executor: the caller's `system`/`prompt` (or `messages`) passed to the provider **unmodified**;
  LoadCoach's own prompt records used only for the structured-output corrective retry and the
  circuit-breaker re-probe, each recorded on the attempt that used it
  ([Spec §9](spec.md)). Provider call through ModelRack via `stream()` in **both** the streaming and
  non-streaming endpoints, so cancellation, the idle timeout and partial-response preservation are
  uniform ([API §5](api.md)). Usage and timing captured with provider time and overhead separate.
* Validation: JSON, JSON Schema, required fields, regex, length; corrective retry using a versioned
  prompt.
* `POST /generate`, `POST /generate/stream`; job records for synchronous requests too (so every
  execution has an explanation and a history).
* **Extract MirrorWall** ([MirrorWall Phases 1–2](../../packages/mirrorwall/development-plan.md));
  rebuild LoadCoach's pages on it; telemetry bar live.
* **Extract `setspec.prompts`** from FreeWeight's prompt module
  ([ADR-0028](../../adr/0028-prompt-pack-granularity.md)); LoadCoach uses it from the start, and a
  golden test asserts FreeWeight's existing pack hashes identically before and after the move.

**Files/subsystems**
```text
src/loadcoach/services/execution.py  src/loadcoach/domain/validation.py
src/loadcoach/prompts/**  src/loadcoach/web/routes/generate.py
py/MirrorWall/**            (new repository)
tests/integration/{test_generate,test_streaming,test_validation}.py
```

**Tests**
* Generation against `FakeProvider`: success, provider error, timeout, malformed output, cancellation.
* Streaming: token deltas ordered, terminal `result`, disconnect/replay, cancellation within one chunk.
* A **non-streaming** `POST /generate` is cancellable within one chunk too, because the executor
  streams internally; a provider that cannot stream records `cancellation_deferred_to_completion`.
* The caller's prompt reaches the provider byte-for-byte: a test asserts the transcript ModelRack
  received equals what the caller sent.
* Frame shapes: every SSE frame except `token` parses through `setspec.load_envelope`; `token` does not.
* Validation: each kind passes and fails correctly; a corrective retry is recorded as a separate
  attempt.
* Overhead ≤ 15 ms excluding provider time (performance test).
* Live (marked): real Ollama generation and streaming.

**Acceptance criteria**
1. `POST /generate {"task": "content.article_draft", "prompt": "…"}` returns a real result with
   routing metadata, usage and separated timings.
2. Streaming works in the browser and the CLI, and survives a reconnect.
3. `mirrorwall 0.2.0` published; LoadCoach's UI runs on it.

**Known risks:** conflating provider and overhead timings. **Likely failure modes:** a validation
retry that loses the original attempt record; leaked connections on cancel.
**Gold standards:** separated timings; recorded attempts; cancellable streaming.
**Deferred:** the queue.

---

## Phase 5 — Queue, scheduling and recovery

**Goal:** asynchronous jobs with priorities, admission control, ageing, cancellation and restart
recovery — with the scheduling simulator proving the properties.

**Prerequisites:** P4.

**Work**
* Tables: `jobs` (full), `job_attempts`, `job_events`, `residency`.
* Worker threads with atomic lease-based claiming (the claim does **not** touch `attempt`), adaptive
  polling and an enqueue wake-up; a **lease keeper** on the scheduler thread renewing the leases of
  executing jobs, because a worker inside a blocking provider call cannot renew its own
  ([ADR-0029 §4](../../adr/0029-queue-mechanics.md)).
* Admission **policy** around P3's estimator: the `admitted` and `waiting_resources` states, lease
  release on deferral, re-evaluation on unload or headroom, per-device aggregate checks, and
  FreeWeight's measured KV bytes per token where present.
* The **ageing sweep** — one set-based `UPDATE` every `ageing_interval_seconds` — plus
  `max_wait_seconds` and the starvation counter; retries, fallback chain, circuit breaker.
* Cancellation at every state, with the `cancelling` watchdog.
* Startup recovery, idempotent, with a reconciliation summary.
* `POST /jobs`, job endpoints, `GET /queue`, queue control commands; Jobs and Queue UI pages.
* **The scheduling simulator** as a first-class test harness.

**Files/subsystems**
```text
src/loadcoach/domain/{queue_state,priority,admission,circuit_breaker}.py
src/loadcoach/services/{queue,worker,recovery,residency}.py
src/loadcoach/web/routes/{jobs,queue}.py  src/loadcoach/cli/commands/{job,queue}.py
tests/simulation/{simulator,test_scheduling_properties}.py
tests/integration/{test_queue,test_recovery,test_cancellation}.py
```

**Tests**
* Atomic claiming under concurrent workers: no double-claim (stress test).
* Lease expiry: idempotent work requeues; non-idempotent fails with `worker_lost`.
* Ageing bound with the process **running**, not merely across a restart: with continuous
  `interactive` load and a simulated clock, a `background` job's wait stays under the bound. A
  startup-only recomputation passes every other test and fails this one.
* Attempt numbering: claim → in-lease corrective retry → lease loss → re-claim produces attempts
  1, 2, 3 with no collision, and `max_attempts` bounds the total.
* Lease renewal across an attempt longer than `lease_seconds`: not reclaimed while the worker is
  inside a blocking call; reclaimed once the keeper stops.
* Every legal transition in [Queue §2](queue-and-scheduling.md) is exercised and every illegal one
  rejected — including `leased → waiting_resources` (which releases the lease) and
  `leased → cancelling`.
* Admission: defers with numbers when VRAM is short; resumes when it frees.
* Circuit breaker: opens, excludes, re-probes after cool-down; visible in explanations.
* Cancellation from every state; the watchdog forces `cancelling` to terminate.
* Kill -9 at five different lifecycle points; recovery loses nothing and duplicates nothing.
* Dispatch latency and enqueue budgets met.

**Acceptance criteria**
1. Jobs survive a kill -9 with no lost, duplicated or stuck job.
2. The starvation bound is proven by simulation, not asserted in prose.
3. Insufficient VRAM defers with a reason rather than failing or thrashing.
4. Cancelling an executing job stops it within one chunk and leaves no orphaned resident model.

**Known risks:** the scheduler is the most concurrency-sensitive component in the suite. Mitigated by
the simulator, by atomic claiming and by kill-point recovery tests.
**Likely failure modes:** double execution after a lease race; jobs stuck in `cancelling`;
priority inversion.
**Gold standards:** durable queue; bounded waits; safe cancellation; idempotent recovery.
**Deferred:** evidence import, production feedback.

---

## Phase 6 — Evidence import and evidence-driven routing · **M4 beta**

**Goal:** importing a FreeWeight bundle visibly changes routing, and the explanation shows exactly how.

**Prerequisites:** P5; SetSpec P4; FreeWeight P11 evidence export.

**Work**
* Importer: file upload and pull-from-URL (through the fetch allowlist,
  [ADR-0026 §3](../../adr/0026-local-http-hardening.md)); schema-version negotiation; provenance
  validation; per-record reporting of imported / updated / **unmatched** / rejected.
* Identity binding per [ADR-0022 §4](../../adr/0022-capability-evidence-record-contract.md): evidence
  for an undiscovered model is retained and bound later; a digest in the bundle upgrades a local
  `name_only` row; `name_only` evidence against a locally-digested model stays `ambiguous_name_only`
  and never scores.
* Evidence matched to an execution only on `runtime_profile_hash`
  ([ADR-0023](../../adr/0023-runtime-profile-resolution.md)); a mismatch is named in the explanation
  with both hashes and the FreeWeight invocation that would fix it.
* `[evidence] freeweight_api_key_env` so a FreeWeight that requires a token can be pulled from.
* `capability_evidence` and `evidence_sources` tables; staleness evaluation; scheduled refresh.
* Scoring switches to measured evidence where present, retaining declared/manual as a labelled
  fallback.
* `POST /evidence/import`, `GET /evidence`, `GET /evidence/sources`; CLI; a Benchmarks (evidence) UI
  page showing source, age, confidence and coverage per capability.

**Files/subsystems**
```text
src/loadcoach/services/evidence.py  src/loadcoach/domain/evidence_policy.py
src/loadcoach/infrastructure/freeweight_client.py     # thin httpx client over the public API
src/loadcoach/web/routes/evidence.py
tests/contract/{test_evidence_import,test_schema_rejection}.py
tests/integration/test_evidence_routing_change.py
```

**Tests**
* Import of SetSpec goldens; unsupported major rejected with both versions and existing evidence
  untouched.
* Records with mismatched machine fingerprints kept but flagged; performance evidence from another
  machine not used for performance-sensitive constraints.
* Before/after routing test: importing evidence changes the selected model, and the explanation shows
  the capability, score, confidence, age and source responsible.
* FreeWeight unreachable: last import retained, marked stale, health degraded, routing continues.
* Import is `admin`-scoped; oversize import rejected.
* Evidence for a model that has not been discovered imports successfully, is reported as unmatched,
  contributes nothing, and binds automatically on the next discovery pass.
* Freshness uses `measured_at`: a producer that re-aggregates old runs does not gain confidence here.
* A `file://` URL, a host outside `allowed_source_hosts`, a link-local address and a cross-host
  redirect are each refused with `EVIDENCE_SOURCE_REFUSED` before any bytes are parsed.

**Acceptance criteria**
1. A bundle exported by FreeWeight imports with no FreeWeight code and no database access.
2. Routing changes visibly and explicably after import.
3. With FreeWeight absent, everything still works; the explanation says so.

**Known risks:** contract drift between the applications. Mitigated by SetSpec goldens tested in both
repositories. **Likely failure modes:** silently accepting a newer major; merging evidence across
benchmark versions.
**Gold standards:** versioned contracts; optional integration; explainable change.
**Deferred:** production feedback.

---

## Phase 7 — Production feedback, reliability and regression detection

**Goal:** LoadCoach learns from what actually happened, and says so.

**Prerequisites:** P6.

**Work**
* `feedback` and `reliability_stats` tables; incremental recomputation.
* `POST /jobs/{id}/feedback` (idempotent per source); `GET /reliability`.
* `reliability_factor` becomes live; circuit breaker driven by real statistics.
* Regression detection against a model's own baseline, surfaced in health and the UI.
* Reliability page: per model and task profile, with acceptance, validation pass rate, latency
  distribution and trend.

**Files/subsystems**
```text
src/loadcoach/services/feedback.py  src/loadcoach/domain/reliability.py
src/loadcoach/web/routes/reliability.py  tests/unit/test_reliability_math.py
tests/integration/test_feedback_affects_routing.py
```

**Tests**
* Feedback idempotency per `(job, source)`; conflicting sources both retained.
* Reliability factor neutral below the minimum sample count, then applied.
* A model that starts failing is deprioritized, then excluded, then re-probed.
* Regression detection fires on a synthetic degradation and not on noise (threshold test).
* Production evidence never overwrites benchmark evidence (both remain visible).

**Acceptance criteria**
1. Repeated validation failures for one model visibly change routing, with the reason in the
   explanation.
2. Feedback from IdeaPress is accepted, idempotent and reflected in `GET /reliability`.
3. Regression warnings appear in health.

**Known risks:** over-reacting to small samples. Mitigated by minimum sample counts and bounded
factors. **Likely failure modes:** a feedback loop that permanently excludes a good model after a bad
day (mitigated by the re-probe path).
**Gold standards:** evidence separation; bounded adaptation; explainability.
**Deferred:** exploration routing (post-1.0).

---

## Phase 8 — UI completion and operations

**Goal:** LoadCoach is operable by a person: dashboard, jobs, queue, routing, models, evidence,
reliability, system, settings.

**Prerequisites:** P7.

**Work**
* Dashboard: current activity, queue health, recent decisions, model mix, degradations.
* Jobs list and detail with the full attempt history and the explanation rendered readably.
* Queue page live over SSE with pause/resume/drain controls.
* Models page with evidence coverage, reliability and residency.
* System page: telemetry, residency, threadpool, dispatch latency, starvation, circuit breakers.
* Settings page (runtime-changeable only); token management CLI.
* Accessibility and UI checklist pass.

**Files/subsystems**
```text
src/loadcoach/web/routes/{dashboard,system,settings}.py  src/loadcoach/web/templates/**
tests/e2e/{test_dashboard,test_job_detail,test_queue_controls}.py
tests/accessibility/test_ui_checklist.py
```

**Tests**
* Explanation page shows every candidate, score, factor and rejection with its numbers.
* Queue page updates live and controls take effect.
* Every UI checklist item passes; `—` for unsupported values.
* Empty, loading, error and populated states for every view.

**Acceptance criteria**
1. A user can answer "why did it pick that model?" entirely from the UI in under a minute.
2. Every acceptance item in [UI/UX Standards §13](../../standards/ui-ux-standards.md) passes.

**Known risks:** explanation rendering that is technically complete but unreadable. Mitigated by the
one-minute usability criterion above.
**Likely failure modes:** an explanation page that dumps JSON instead of explaining; queue controls
that appear to take effect but do not; live pages that drift from the underlying state after a
reconnect.
**Gold standards:** explainable routing, visibly; accessible dense UI.
**Deferred:** hardening.

---

## Phase 9 — Hardening and 1.0 · **M5**

**Goal:** LoadCoach 1.0 — safe to expose on a LAN, fast enough, documented, and complete against its
acceptance criteria.

**Prerequisites:** P8.

**Work**
* Auth hardening: tokens, scopes, rate limits, queue-depth caps per token; the LAN-exposure path
  reviewed end to end (this is the application most likely to be exposed).
* Performance pass against every budget; scheduling simulation at scale.
* Security pass: full checklist, plus verification that no tool call is ever executed, that `Host`
  validation precedes authentication, and that the evidence-import fetch allowlist holds.
* Documentation: README, quickstart, configuration reference, API docs, routing guide, operations
  guide, troubleshooting, backup/restore, upgrade notes.
* Publish `loadcoach 1.0.0`.

**Files/subsystems**
```text
src/loadcoach/web/{auth,rate_limit}.py
docs/{quickstart,configuration,routing,operations,troubleshooting,upgrading,security}.md
tests/security/**  tests/performance/**
```

**Tests**
* Every security test in [Security Standards §14](../../standards/security-standards.md).
* Non-loopback bind without tokens refuses to start; scoped endpoints reject wrong scopes.
* Rate limit and queue cap enforced per token.
* Every performance budget met.
* Clean-machine install and full journey with and without FreeWeight.

**Acceptance criteria**
1. All 12 acceptance criteria in the [spec §20](spec.md) pass.
2. All LoadCoach gold standards in [Gold Standards §2](../../standards/gold-standards.md) are met.
3. Documentation complete; `loadcoach doctor` diagnoses every documented failure mode.
4. `loadcoach 1.0.0` published; **IdeaPress may begin its LoadCoach integration phase.**

**Known risks:** LAN exposure being the sharpest edge in the suite. Mitigated by refusal-by-default,
scopes and the security checklist.
**Likely failure modes:** a scope checked at the route but not in the service; a rate limit that
starves a legitimate caller; documentation drift from the generated configuration reference.
**Gold standards:** every LoadCoach gold standard, measured.
**Deferred to post-1.0:** multi-machine execution, exploration routing, learned routing, batch
affinity beyond the current heuristic, cost-aware remote routing, speculative execution,
`LoadCoachClient` as a package.
