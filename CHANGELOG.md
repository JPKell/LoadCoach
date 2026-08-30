# Changelog

All notable changes to `loadcoach` are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning follows
[Semantic Versioning](https://semver.org/), pre-1.0 per
packaging and release standards §3.

## [Unreleased]

### Added
- Phase 5, unit 5: admission and residency (`domain/admission.py`, `services/residency.py`;
  queue §5–§6, ADR-0027, ADR-0036 §3).
  - Admission is built around P3's estimator, not instead of it. Routing evaluates a
    **reservation-adjusted** snapshot: every other in-flight job's estimate is subtracted from
    *its* device (never summed across devices), a model already resident there is not reserved
    twice, and an idle resident model's memory counts as reclaimable. A model resident on a device
    fits there whatever the estimate says — the one exception to "unknown does not fit" — and its
    device is preferred over another that merely has room (`ConstraintInputs.resident_devices`).
  - When routing rejects every candidate and at least one rejection is resource-shaped, the job
    moves `leased → waiting_resources` with the lease released and the numbers recorded on the
    event (required bytes, headroom, free per device, unknown reasons); when none is, it fails
    with `NO_ELIGIBLE_MODEL`. The scheduler re-evaluates waiting jobs by admission's own rule —
    never more optimistically, so nothing bounces — on its cadence and at once when a job leaves
    flight or a model unloads.
  - `ResidencyService`: loads a candidate on its target device before execution, evicting the
    least-recently-used *idle* resident while the device holds `max_resident_models` or lacks
    the room; unloads after `unload_idle_seconds`, per device; reconciles with the provider's
    own report; degrades to load-on-demand with `residency_unmanaged` recorded where the provider
    declares no residency control. Every episode is a `residency` row.
  - Affinity batching is now fed: a pinned model is recorded at enqueue, routing's residency
    tie-break and the affinity claim both read the table.
  - Properties proven by simulation: insufficient VRAM defers with the device's numbers and
    resumes when it frees, with no claim-defer thrash in between; the two-GPU fixture (larger
    than either device, smaller than their sum) is deferred, not admitted; above one concurrent
    job, a second job on the same device waits while the first holds it and runs once the first
    is idle and evicted; jobs on different devices run concurrently; affinity batching cuts model
    loads from ten to two without breaching the wait bound (and the mutation without affinity
    reloads nearly every job); idle unload and per-device LRU eviction; and the starvation bound
    under a **running** clock with continuous interactive load — which the same scenario with the
    sweep switched off fails, exactly as ADR-0029 §1 predicts of a startup-only recomputation.
- Phase 5, unit 4: workers, the scheduler thread and the lease keeper (`services/worker.py`,
  queue §3, §9, ADR-0029 §4).
  - `Worker`: claims atomically, then runs one job through `leased → admitted → executing →
    validating → completed`, with corrective retries and fallback through `retrying → admitted`.
    Every step is fenced on the lease, so a worker whose lease was reclaimed has its next
    transition or attempt write refused and stops; it never overwrites the reclaimer's work.
    Polling is adaptive (50 ms after a claim, doubling to 1 s idle) plus the enqueue wake-up.
  - `Scheduler`: one thread, never blocking on a provider, that runs whatever is due each
    `poll_interval_ms`: the **lease keeper** renews every in-flight lease this process holds
    every `lease_renewal_interval_seconds`; the reaper, max-wait expiry and the ageing sweep run
    on their own cadences; pause/drain flags are read from the settings table.
  - `InFlightRegistry`, keyed by `(owner, job_id)`: after a lease race the stale holder and the
    reclaimer both appear, the keeper renews for the owner and marks the other lost.
  - The executor now exposes `run_attempt` (one provider call plus validation), `corrective_turns`
    and `write_attempt` — the **only** place `jobs.attempt` is incremented, in the transaction
    that writes the `job_attempts` row (ADR-0029 §2). `provider_facts_for` moved into services so
    the worker can use it. Attempt outcomes use the data model's own vocabulary (`timeout`,
    `context_exceeded`) rather than folding everything into `provider_error`.
  - The runtime starts in the application lifespan and stops with it; the simulator drives the
    same `Worker.run` and `Scheduler.tick` over its fake clock.
  - Properties proven by simulation: the pipeline end to end; dispatch on the enqueue wake-up
    rather than the next poll; priority ordering across classes; the concurrency limit under a
    burst; a lease renewed across a 300 s attempt under a 60 s lease, and — with the keeper
    stalled — expiry, reclaim and exactly one completion; attempt numbering 1, 2, 3 across an
    in-lease corrective retry and a lost lease; provider failure falling back; non-idempotent
    work failing with `worker_lost` instead of re-running.
- Phase 5, unit 3: enqueue, the atomic claim, leases and the ageing sweep (`services/queue.py`,
  queue §3–§4, ADR-0029 §1–§2, ADR-0010).
  - `enqueue`: durable idempotency on `(source, idempotency_key)` with `idempotency_expires_at`
    written from `queue.idempotency_ttl_hours`; an expired key is released for reuse and a raced
    duplicate resolves to the row that won the unique index; `QUEUE_FULL` above `max_depth`.
  - `claim`: one `UPDATE … WHERE id = (SELECT … ORDER BY effective_priority DESC, created_at
    LIMIT 1) RETURNING` under `BEGIN IMMEDIATE` (`FOR UPDATE SKIP LOCKED` on PostgreSQL). It never
    touches `attempt`. Affinity batching prefers a resident model's job within the top-priority
    tie only, bounded by `max_affinity_streak`.
  - `transition`/`move`: every state change is a compare-and-set on `state` (and `lease_owner`
    for a worker) plus its event in one transaction; a lost lease is a refusal, not an overwrite.
    Only lease-holding states carry a lease, so reaping selects on `lease_expires_at` alone.
  - `ageing_sweep`: the one set-based `UPDATE` over `queued`/`waiting_resources` with `queued_at`
    as the origin, in each dialect's own date arithmetic, sharing `AGEING_EPSILON_POINTS` with
    the domain formula so SQL and Python agree at exact minute boundaries.
  - `renew_leases` (the keeper's statement), `reap_expired_leases` (idempotent work requeues,
    non-idempotent fails with `worker_lost`), `expire_max_wait` (`MAX_WAIT_EXCEEDED`),
    `queue_snapshot` (depth by state and class, oldest age, starvation counter), `get_job`,
    `list_jobs`.
  - `services/job_events.py`: persisted job events with one gap-free sequence per job, published
    only after commit; token deltas are fanned out live from the same sequence and never stored.
  - Query plans asserted on the real compiled statements: the claim walks its index with no temp
    B-tree, the sweep uses the state index, reaping uses `lease_expires_at`; none scans `jobs`.
  - Stress test: eight threads claiming two hundred jobs, every job claimed exactly once.
- Phase 5, unit 2: the scheduling simulator (`tests/simulation/simulator.py`, queue §12), built
  before the scheduler it will drive. A settable `FakeClock`; a discrete-event `Driver` that runs
  real worker threads through handshakes so exactly one thread runs at a time and the interleaving
  of workers, scheduler ticks and arrivals is fixed by `(time, insertion order)`; a
  `SimulatedWakeup` with `threading.Event`'s semantics; a `SimulatedProvider` whose generations
  take simulated seconds, chunk by chunk, load on demand (counted) and are cancellable within one
  chunk; and a `Simulation` composition over a real migrated database, simulated per-device VRAM
  and the shipped settings. Its own mechanics — ordering, wake-ups, cancellation, timeout, failure
  injection, load accounting, determinism — are tested in `test_simulator_mechanics.py`.
- Phase 5, unit 1: the queue's schema and pure domain.
  - Migration `0004`: the `residency` table (data model §2, ADR-0027), with the
    `vram_bytes`/`vram_bytes_unavailable_reason` measurement pair; and the claim index recreated as
    `(state, effective_priority DESC, created_at)` — `0003` had it ascending throughout, which made
    SQLite sort every equal-priority job through a temp B-tree on each claim.
  - `domain/queue_state.py`: the job state machine — queue §2's table plus ADR-0036's six recovery
    and cancellation edges; every unlisted pair is rejected and a test enumerates all 121.
  - `domain/priority.py`: the four classes and their bands, `base_priority` (the band is not
    escapable), `effective_priority` (queue §4's formula, capped at band top + overflow) and the
    starvation threshold (half the job's own `max_wait_seconds`).
  - `queue.idempotency_ttl_hours` (default 24) and `queue.cancelling_watchdog_seconds` (default
    30) in `QueueSettings`; both were assumed by the data model, api.md §4 and queue §9 but were
    missing from the configuration.

### Changed
- `queue.lease_seconds >= 3 x lease_renewal_interval_seconds` is now the settled boundary (LC8):
  the keeper is late by at most one scheduler tick, so exactly 3x survives two consecutive missed
  renewals and is lost only when the scheduler thread has stalled for more than two intervals.

### Changed
- Widened the `sweatmeter` pin to `>=0.4,<0.5`. SweatMeter's first published release is `0.4.0`
  (`0.3.0` completed its development plan but never reached the index), and it adds the in-process
  NVML GPU backend, selected automatically wherever the optional `pynvml` extra is installed.

### Added
- Phase 3: routing without evidence, and `POST /route`.
  - `domain/routing/subject.py`: runtime profile resolution (ADR-0023 §1) and the `served_context`
    derivation (§4). A context set on a provider that declares `context_configurable=False` is
    **not** recorded as `configured` — the provider will ignore it, and a recorded context that
    never happened is a fabricated measurement.
  - `domain/routing/constraints.py`: the VRAM/KV estimator as a pure function (queue §5) and
    routing §4's ten hard constraints, each rejecting with the numbers that caused it. Devices are
    evaluated independently and never summed (ADR-0027 §2). An unknown estimate is `None`, never
    `0`, and never fits.
  - `domain/routing/scoring.py`: capability scoring with the absent-evidence rule — a capability
    with nothing behind it is excluded from the numerator *and* the denominator, never scored
    zero. Benchmark evidence under a different `runtime_profile_hash` is absent with both hashes
    and a remedy, not reused and not zeroed; no prior papers over an excluded measurement.
  - `domain/routing/ranking.py`: routing §7's total order, with the model ULID as the final
    tie-break so the order is total for every input.
  - `domain/routing/context_budget.py`: budgeting against `served_context` only; output tokens
    reduced where `execution.min_output_tokens` permits, rejected with numbers otherwise. The
    caller's input is never shortened.
  - `domain/routing/explanation.py`: routing §8's persisted document, with the `low_evidence` and
    `assumed_context` flags.
  - `services/routing.py`, `web/routes/routing.py`, `cli/commands/route.py`: the pipeline,
    `POST /api/v1/route`, `GET /api/v1/routing-decisions[/{id}]`, the `/routing` pages and
    `loadcoach route explain`.
  - Migration `0002`: `routing_decisions` and `routing_candidates`. `runtime_profile_id`,
    `served_context`, `served_context_source` and `target_gpu_index` sit on the candidate, because
    a candidate *is* the pair `(identity, resolved runtime profile)`.
  - `[routing].remote_cost_factor` and `execution.min_output_tokens` added; discovery now persists
    the descriptor geometry (`layers`, `kv_heads`, `head_dim`, …) the KV estimate needs, omitting
    every field the provider did not report rather than storing a zero.
- Phase 4: execution, streaming and validation.
  - `services/execution.py`: the executor. The caller's `system`/`prompt` (or `messages`) reaches
    the provider **byte-for-byte** — no system prompt of LoadCoach's own is prepended, nothing is
    substituted, and a test asserts the transcript ModelRack received equals what the caller sent.
    The provider is always called through `stream()`, in both endpoints, so cancellation, the
    idle timeout and partial-response preservation are uniform; a provider that cannot stream
    records `cancellation_deferred_to_completion`. Provider time and LoadCoach overhead are
    measured separately and never summed into one figure.
  - `domain/validation.py`: JSON, JSON Schema, required fields, regex and length. The schema
    validator implements the keywords the suite's schemas use and **refuses** a schema using any
    other, because an ignored constraint is a validation that passed for a reason nobody intended.
    Every failing field path is reported, not the first.
  - The corrective retry is a **new attempt row**, never an edit of the previous one, and it
    records the `prompt_id`, `version` and `sha256` of the prompt LoadCoach applied.
  - `prompts/`: the pack LoadCoach originates, through `setspec.prompts`. One record so far —
    `execution.structured_output.retry`.
  - `POST /api/v1/generate` and `POST /api/v1/generate/stream`, the latter on MirrorWall's SSE.
    Every frame carries the SetSpec event envelope except `token`, which is bare (ADR-0025 §3).
    A reconnect with the same `idempotency_key` and a `Last-Event-ID` attaches to the execution
    already running and receives exactly the frames it missed.
  - Migration `0003`: `jobs`, `job_attempts`, `job_events`, `validations`, and `job_id` on
    `routing_decisions`. Every execution gets a job row, synchronous or not, so every execution
    has an explanation and a history.
- MirrorWall adoption (that package's Phases 1 and 2): `RequestIdMiddleware`,
  `HostValidationMiddleware`, `error_body` and `mount_static` now come from the package, and this
  application's own copies are deleted rather than kept in parallel. Assets are served with
  content-hashed, immutable-cacheable URLs, and every response carries `X-Response-Time-Ms`.
- MirrorWall adoption (that package's Phase 1): every page now renders through
  `mirrorwall.create_template_environment` on `mirrorwall/base.html`'s shell and its component
  macros. LoadCoach's own `base.html` is deleted rather than kept in parallel, and MirrorWall's
  assets are served from the installed package at `/static/mirrorwall` — no CDN, no network
  request at page load.
- Repository scaffold generated from the suite's development plan (no functional code yet).
- Phase 1: skeleton, storage and the WeightsDB extraction handshake.
  - `config.py`: typed, source-tracked settings with the full precedence chain and the
    config-level half of ADR-0026's non-loopback refusal set (bind acknowledgement,
    `server.allowed_hosts`).
  - `bootstrap.py`: composition root; adds the database-backed half of the refusal set (at least
    one active, unrevoked `api_tokens` row before a non-loopback bind is allowed).
  - `infrastructure/db/models.py` and migration `0001`: `models`, `model_capabilities`,
    `runtime_profiles`, `task_profiles`, `settings`, `api_tokens`, built on `weightsdb`.
  - `web/app.py`: request-ID and Host-validation middleware, the standard error envelope, and
    `GET /api/v1/health` / `GET /api/v1/version`.
  - `cli/`: `serve`, `health`, `doctor`, `version`, `config show|validate|init|path`,
    `db upgrade|status|backup|restore`.
  - `observability/logging.py`: structured text/JSON logging with request-ID correlation.
- Phase 2: registry, task profiles, `GET /models`, `GET /task-profiles`, plain HTML pages.
  - `config/task_profiles.toml`: all fifteen shipped profiles (routing.md §2), each validated at
    startup (weights sum to 1.0, capabilities in the SetSpec vocabulary, referenced JSON schemas
    under `config/schemas/` resolve, no contradictory `response_format`/`require_schema` vs.
    `json_schema_ref`) — a malformed profile refuses startup naming the file, profile ID and
    problem.
  - `services/models.py`: discovery through ModelRack; unavailable models are flagged with a
    reason, never deleted; declared `ModelCapabilityFlag`s honestly translated into SetSpec
    capability rows (only the flags with an unambiguous counterpart — never padded).
  - `config/manual_capability_scores.toml`: optional operator-entered capability scores, marked
    `source="manual"`; shipped empty.
  - `cli/`: `models list|show|refresh`, `tasks list|show|validate`.
  - `web/rendering.py` and `web/templates/`: the first UI pages, deliberately plain
    (pre-MirrorWall) — `/models` and `/task-profiles`.

### Fixed
- `services/database.py`: `ensure_ready()`/`get_status()` misused `redact_url()` on an arbitrary
  exception message instead of a URL, which raised instead of reporting the real error whenever
  the message wasn't itself a parseable URL.
- `cli/commands/tasks.py`: `tasks list`/`tasks show` read the `task_profiles` table directly and
  saw it empty on a fresh install where `loadcoach serve` (which imports the shipped profiles) had
  never run; both commands now import before reading.
