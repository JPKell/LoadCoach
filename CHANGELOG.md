# Changelog

All notable changes to `loadcoach` are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning follows
[Semantic Versioning](https://semver.org/), pre-1.0 per
packaging and release standards §3.

## [Unreleased]

### Added
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
