# Changelog

All notable changes to `loadcoach` are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning follows
[Semantic Versioning](https://semver.org/), pre-1.0 per
packaging and release standards §3.

## [Unreleased]

### Added

- `[server] trusted_proxies` (CIDR list, config-only): reverse-proxy networks whose
  `X-Forwarded-For` is believed. Behind ADR-0014 §7's mandated TLS proxy every caller shared
  the proxy's address, so twenty bad bearers a minute from anyone braked every user — correct
  tokens included — for the rest of the minute. With the proxy listed, the failed-auth brake
  and the unauthenticated rate bucket key on the last untrusted hop of the header; from any
  other peer the header is ignored entirely. The brake stays keyed per address, deliberately:
  keyed per `(address, credential)` a guesser would mint a fresh bucket with every guess.
  api.md §11 now says what the brake does to valid tokens.
- `serve` now warns at startup (`server.plain_http_exposure`) on a non-loopback bind with no
  `trusted_proxies` configured — ADR-0014 §7's "no evidence of a proxy" warning. The 401 page,
  api.md §11 and docs/security.md now say plainly that the tokened-bind browser flow needs
  HTTPS or loopback: its cookies are `Secure` (the CSRF cookie `__Host`-prefixed), so on a
  plain-HTTP non-loopback origin the browser stores neither and `POST /token-cookie` is refused
  with `CSRF_FAILED`. The cookie flags are unchanged, deliberately; the API's `Authorization`
  header is unaffected.

### Fixed

- The bare `pytest` console script — the invocation CI, the README and CONTRIBUTING use — could
  not collect the suite at all (`ModuleNotFoundError: No module named 'tests'`, 24 collection
  errors), because test modules import shared fixtures as `tests.…` and only `python -m pytest`
  put the repository root on `sys.path`. `[tool.pytest.ini_options]` now sets
  `pythonpath = ["."]`, so both invocations agree; every CI run before this fix failed at its
  pytest step for this reason.
- A half-open circuit breaker admitted more than one probe: two workers could both pass
  routing's exclusion check before either marked the probe, and both executed on the model —
  queue §7 promises "a single low-priority job". The worker now gates at execution: a candidate
  whose probe another job already holds is skipped like a `recently_failing` rejection, falling
  to the next candidate; with none left the job is requeued and deferred (`PROBE_IN_FLIGHT`,
  `waiting_resources`) until the probe reports, then woken by the breaker's verdict. An open
  breaker's rejection still fails admission as before.
- The synchronous path — `POST /generate`, `POST /route`, `loadcoach generate`,
  `loadcoach route explain` — ignored the circuit breaker and residency: it could select a
  model the queue had opened the breaker on, and a synchronous request on a half-open model was
  a second, unmarked probe. The web entry points now pass the runtime's breaker state and
  residency into routing (rejections show `recently_failing` with the breaker record), and the
  synchronous executor marks and releases the half-open probe exactly as the queue worker does.
  A decision made where no breaker registry exists — the CLI's one-shot process — now carries a
  `breaker_state_unavailable` flag instead of silently assuming no breakers are open.
- Candidate capability tables on `/routing/{decision_id}` now carry a row-count, as UI
  standards §5 requires of every table; before, a decision with two or more candidates rendered
  them bare. The accessibility checklist's server fixture also pins the deterministic telemetry
  snapshot itself — it is built before the autouse pin applies, and its pages used to depend on
  the developer's real GPU (passing on a busy card, failing on a machine with none).

## [1.0.0] — 2026-08-30

LoadCoach 1.0: the M5 exit — an explainable, durable, secure routing service. Phases 7 through 9
of the [development plan](docs/apps/loadcoach/development-plan.md): production feedback and
reliability, the complete operator UI, and hardening for a LAN.

**Prepared, not published.** No tag, no upload. Both extracted packages — `weightsdb 0.2.0` and
`mirrorwall 0.2.0` — are on PyPI as of 2026-08-30, so `pip install .` resolves from the index in a
clean virtualenv, `requirements/ci.lock` is hash-pinned against it, and every CI job installs from
that lock non-editably (see [`requirements/README.md`](requirements/README.md)). The tag is the
remaining step.

### Added
- Phase 7, unit 1: the feedback and reliability schema and its pure statistics (data model §2–§4,
  routing §6 and §11, ADR-0016).
  - Migration `0006` adds `feedback` and `reliability_stats` and nothing else. `feedback` is unique
    per `(job_id, source)`; `reliability_stats` is unique per `(model_id, task_profile_id, window)`,
    which is also the lookup index data model §4 requires, and carries a sample count beside every
    statistic (ADR-0016 rule 6).
  - `loadcoach.domain.reliability`: windows (`7d`, `30d`, `all`) with one boundary rule, counts by
    outcome using the circuit breaker's own success rule, bounded rates, nearest-rank latency
    percentiles, throughput, caller acceptance with its own weight, the `reliability_factor`
    (`0.5–1.0`, exactly `1.0` and saying why below `PRODUCTION_MINIMUM_SAMPLES = 20`), regression
    detection against a model's own baseline (an absolute drop **and** a two-proportion z-score),
    and a fold-based ledger whose statistics are required to equal a from-scratch computation on
    every field — a property test, not an example.
- Phase 7, unit 2: caller feedback (api.md §6, spec §14).
  - `POST /jobs/{id}/feedback`, `write`-scoped: `201` with the stored record on a source's first
    verdict, `200` on an update; idempotent per `(job_id, source)`, and two sources that disagree
    are both kept. `source` is the token's name, else `X-Client-Name`, else the body, else
    `anonymous` — never the body when a token is present. `GET /jobs/{id}` lists every source's
    record under `feedback`.
  - `loadcoach job feedback JOB --accepted|--rejected [--quality] [--edited] [--notes] [--source]`,
    through the same service.
  - `loadcoach.services.reliability`: per-pair recomputation of the three `reliability_stats`
    windows from `job_attempts` and `feedback` (the incremental path), `recompute_all` (the full
    path it must equal), the breaker's sample source classified by the statistics' own success
    rule, and persistence of breaker verdicts onto the rows.
- Phase 7, unit 3: production evidence live in routing (routing §6, §11; queue §7).
  - `reliability_factor` is applied on every decision from the freshest `reliability_stats` window
    with at least twenty counted attempts; the explanation carries `factors.reliability_detail`
    (window, attempts, rates, acceptance, one line saying why) whether the factor is live or
    neutral.
  - Every attempt row the worker writes, and every feedback record, recomputes the one
    `(model, task_profile)` pair it touched; `recompute_all` is the full path it is tested to
    equal over random sequences.
  - The circuit breaker's samples come from `services.reliability`, classified by the statistics'
    own success rule, and a changed verdict is persisted onto the model's rows.
- Phase 7, unit 4: the reliability surface (api.md §7; spec §7.2, §17).
  - `GET /reliability`: every tracked `(model, task_profile)` with its `7d`/`30d`/`all` statistics
    (each value with its sample count and a reason when absent), the factor routing applies with
    its inputs, the regression verdict against the model's own baseline, and the breaker's
    persisted state; filter by `task` and `model`.
  - The Reliability page (`/reliability`): acceptance, validation pass rate, latency distribution
    and trend per pair, an em dash carrying the reason for every absent value, and the regressions
    listed by name. `loadcoach reliability show [--task] [--model] [--json]` reads the same report.
  - `/health` gains the `reliability` component: degraded, naming the pair and the numbers, when
    a model's recent validated-success rate has regressed against its own history.
  - The breaker's probe is marked when execution starts (not when routing merely ranked the
    model), released if that attempt is cancelled, and presumed lost after one cool-down.
- Phase 8, unit 1 (dashboard): the Dashboard at `/` — current activity, queue health,
  degradations (every unhealthy component, open breaker, regression and control flag, with a link
  each), the ten most recent decisions and jobs, and the model mix over 24 hours; every figure
  links to the page that owns it. The telemetry bar is now on every page, fed by
  `GET /api/v1/system/telemetry/stream` (one `telemetry.sampled` frame per `[telemetry]
  interval_ms`, `"unsupported"` for what this machine cannot read). A page that fails renders an
  error state — code, request ID, what to do next — instead of the JSON envelope.
- Phase 8, unit 2 (jobs and the explanation): the explanation is rendered as an explanation.
  `domain/routing/narrative.py` turns the stored document into "why this model": the score
  arithmetic, what separated the winner from the runner-up (a factor, or the capability that moved
  it most), the capabilities that carried the score with their sources, what could not be scored
  and the remedy for it, every flag in words, and every rejection in words with its numbers. It
  leads `/routing/{decision_id}` and is embedded on `/jobs/{id}`; the JSON viewer stays at the
  bottom as the raw source. The jobs list gains filters (state, class, task, source), the model
  and source columns, and cursor pagination shared with the API; the job page lists every
  source's feedback.
- Phase 8, unit 3 (live queue, models, system): the Queue page updates live over
  `GET /api/v1/queue/stream` — every frame is the whole current report plus the page fragment
  rendered from it, published on change, so a reconnect is correct after one frame — with
  pause/resume/drain as forms behind MirrorWall's CSRF middleware (now wired; a forged post is
  `403 CSRF_FAILED`) and `admin` scope on the controls. `GET /models` and the Models page carry
  the evidence summary, reliability and residency api.md §2 names, and `model_ref`. The System
  page (`/system`): telemetry with dashes for what cannot be measured, residency, the thread
  pool, dispatch latency, starvation, health components and breakers.
- Phase 8, unit 4 (settings, tokens, retention, the checklist): `GET`/`PUT /api/v1/settings`
  and the Settings page over one registry of runtime-changeable keys — the two queue flags, four
  routing knobs and `storage.content_retention_hours` — applied by the scheduler within a second;
  a security-relevant key is `403 FORBIDDEN` naming it, an unknown one `400` listing the set.
  `loadcoach token create|list|revoke` (the token shown once; only its SHA-256 stored). Content
  retention (spec §14, P5-15): a finished job's prompt and response text is removed after
  `[storage] content_retention_hours` (default 24) leaving hashes, tokens, timings and routing;
  a queued job keeps its transcript until it has run; `retain_content = true` (config-only)
  keeps everything. `tests/accessibility/test_ui_checklist.py` holds every page to the UI/UX
  §13 items a test can hold, and names what a person must still check.
- Phase 9, unit 1 (auth and limits): scopes are checked at the route **and** inside every
  mutating service (ADR-0014 §5) through one pure rule, `loadcoach.domain.authorization`; a
  contract test holds every API route except `/version` to declaring the principal. On a tokened
  bind the UI carries the same bearer token in an `HttpOnly` cookie set once from the 401 page.
  Per-token rate limits (`[server] rate_limit_per_minute`, `rate_limit_burst`) answer
  `429 RATE_LIMITED` with `Retry-After`; failed authentications are braked per address; the
  per-source queue cap (`[queue] max_active_per_source`) refuses with `QUEUE_FULL` naming the
  source. Host validation runs before authentication and before the limiter on every bind.
- Phase 9, unit 2 (security pass): `tests/security/**` holds every Security Standards §14 item
  — held here, held by a named P6 test (the map asserts each exists), or shown not to apply by
  asserting the surface (no endpoint accepts a path or an archive; a tool call the provider
  requests is returned to the caller and never run). New refusals: an oversize body is
  `413 PAYLOAD_TOO_LARGE` before buffering (`[server] max_body_bytes`); a cross-origin JSON
  write is `403 CSRF_FAILED`; a task profile's `json_schema_ref` cannot resolve outside the
  schemas directory.
- Phase 9, unit 3 (performance pass): every spec §15 budget is measured by a `performance`
  test — enqueue, dispatch, execution overhead, cancellation, idle poll CPU and recovery since
  P4/P5, and now the two routing budgets (twenty candidates with bound evidence, warm and cold)
  and the added latency per streamed chunk. `tests/simulation/test_scale.py` drives a thousand
  mixed-class jobs through the real queue on the fake clock: every job completes, interactive
  work waits least, and the background wait stays inside the starvation bound.

### Changed
- `loadcoach doctor` diagnoses every documented failure mode by name (spec §13 and §5), with a
  remedy each, and exits 4 when one is present; `--json` prints every finding.
- `loadcoach config reference` generates `docs/configuration.md` from the settings model;
  `--check` fails on drift and a test holds the committed file to it (configuration standards §8).
- `loadcoach generate --task … [--prompt|--prompt-file] [--stream]` — spec §7.2's synchronous
  command, which the beta listed but did not ship.
- `docs/openapi.json` is the committed OpenAPI snapshot, held by a contract test.
- Seven operator documents under `docs/`: quickstart, configuration, routing, operations,
  troubleshooting, upgrading, security.

## [0.9.0b0] — 2026-08-30

The M4 beta. Phases 1 through 6 of the
[development plan](docs/apps/loadcoach/development-plan.md): the registry and task profiles,
evidence-weighted routing with a persisted explanation for every decision, synchronous and
streaming generation with validation and corrective retries, a durable priority queue, and
FreeWeight evidence import that visibly changes routing.

**Prepared, not published.** No tag, no upload. `weightsdb` and `mirrorwall` — two runtime
dependencies extracted from this repository's own work — are not on any index, so this package
cannot yet be installed from one; see [`requirements/README.md`](requirements/README.md).

### Added
- Phase 6, unit 1: the evidence schema and its pure rules (data model §2–§4, ADR-0022, ADR-0023,
  ADR-0017, ADR-0032 §6).
  - Migration `0005` adds `capability_evidence` and `evidence_sources` and nothing else, with the
    three indexes data model §4 names and a uniqueness key carrying `policy_version` so two
    confidence policies coexist during a policy change.
  - `loadcoach.domain.evidence_policy`: identity binding (ADR-0022 §4's four rules, in both
    directions), freshness from `measured_at` alone, staleness with its four reasons, environment
    drift, the machine and runtime-profile hard separations, the `user.*` opt-in gate, and a
    selection rule that never averages two records.
- Phase 6, unit 2: the importer (api.md §7, spec §14, ADR-0022, ADR-0025 §2).
  - `loadcoach.services.evidence.import_bundle`: size guard, then schema-version negotiation,
    then per-record validation — every one of them before the transaction opens, so an
    unsupported major leaves existing evidence byte-identical. Per-record reporting of
    imported / updated / unmatched / ambiguous / rejected, with a `DUPLICATE_RECORD` rejection
    where two records in one bundle would otherwise merge onto a single row.
  - A complete bundle marks the rows it omits `superseded`; an incremental one removes nothing.
  - `rebind_evidence_in` runs inside every discovery pass, so evidence imported before its
    models were discovered binds with no re-import.
  - `[evidence] accept_schema_majors` may narrow what this build reads and never widen it — a
    major with no payload models cannot be handed to a v1 reader.
- Phase 6, unit 3: the fetch path (ADR-0026 §3 and §4, ADR-0022 §5).
  - `loadcoach.infrastructure.freeweight_client`: scheme, host allowlist, literal and resolved
    link-local addresses, same-host-only redirects capped at three, `Content-Type` verified
    before parsing, and a **streaming** size cap — every one of them decided before a byte of
    the body is interpreted, and each with `EVIDENCE_SOURCE_REFUSED`.
  - `freeweight_api_key_env` / `freeweight_api_key_file` resolve through the ordinary secret
    chain, and `credential_for` refuses to send a token to any origin but the one it was
    configured for.
  - `refresh_from_freeweight` sends the producer's own `generated_at` back as `?since=` and
    never LoadCoach's clock; the scheduler runs it on `import_interval_hours`.
  - Degradation: an unreachable source retains its last import and badges those rows
    `source_unreachable`; `freeweight_url = ""` is *not configured*, which attempts nothing.
- Phase 6, unit 4: scoring on measured evidence (routing §5, §5.1, §8, ADR-0023 §3, ADR-0032 §6).
  - Routing reads `capability_evidence` filtered on `match_state = 'bound'`, collapses several
    records for one subject to the one that scores rather than averaging them, and carries the
    measurement's age, sample count and staleness into the explanation.
  - `evidence_foreign_machine`: a performance, memory or energy measurement from another machine
    is absent with a named reason and a remedy; a quality measurement from elsewhere is used and
    badged (ADR-0017's last hard separation).
  - `user.*` capabilities never become a signal unless the active task profile names them, and a
    decision that used one states the goal slug, `kappa_w` and `n_holdout` in the rendered note.
  - `evidence_summary` gains routing §8's documented fields — `imported_at`,
    `oldest_measured_at`, `bundle_schema_version`, `policy_version`, `vocabulary_version`,
    `stale`, `unmatched_records` — plus a `status` and a sentence saying, in words, what state
    the evidence source is in.
  - `loadcoach.services.machine`: this machine's fingerprint, from SweatMeter, so LoadCoach and
    FreeWeight agree on it without either knowing about the other (spec §10).
- Phase 6, unit 5: the surface (api.md §7, spec §7.2, §17).
  - `POST /api/v1/evidence/import` (a bundle body or `{"url": …}`, `admin`-scoped),
    `GET /api/v1/evidence` (a collection envelope whose items are real `capability.evidence`
    SetSpec envelopes, filterable and cursor-paged) and `GET /api/v1/evidence/sources`.
  - The Benchmarks (evidence) page: coverage per capability, then every record with its source,
    age, confidence and staleness badge, and the sources table.
  - `loadcoach evidence import|show|sources|refresh`, all usable on a fresh install with no
    `serve` ever having run.
  - The `evidence` health component, whose `not_configured` state is healthy rather than
    degraded because LoadCoach is designed to run without FreeWeight.
  - `loadcoach.web.auth`: bearer tokens and api.md §11's cumulative scope rule, the minimum that
    makes "import is `admin`-scoped" enforceable. Full auth hardening remains Phase 9's.
  - `capability_evidence.record_json` keeps the payload as it arrived, so `GET /evidence`
    re-emits the producer's document rather than a reconstruction (ADR-0025 §2).
- Phase 6, unit 6: the contract tests (testing standards §8, HR6) and integration milestone I4.
  - `tests/contract/test_evidence_import.py` reads every published `benchmark.evidence_bundle`
    golden from the installed SetSpec, at every published version, and asserts each round-trips
    through the store unchanged.
  - `tests/contract/test_schema_rejection.py` pins the rejection: both versions named, and no row
    and no source touched — asserted column by column, not by counting.
  - `pytest -m contract` is green in LoadCoach for the first time; MirrorWall's was already
    closed by its 0.2.0 release.
- Phase 6, unit 7: the pre-milestone documentation consistency review (roadmap §8).
  - `data-model.md` §2 documents `capability_evidence.record_json`.
  - `spec.md` §12 names its configuration precedence chain and file path, as FreeWeight's does.
  - `api.md` §7 documents `GET /evidence`'s `summary` object, its six `status` values, and that
    the schema version is decided before the transaction opens.
  - The mirrored copies under `docs/` are refreshed from the suite's source of truth.
- Phase 6, unit 7: LoadCoach's release plumbing, which had never existed.
  - `requirements/release.in` and `requirements/release.lock` — the hash-pinned build and publish
    chain, byte-identical to the one WeightsDB, ModelRack and SetSpec release from, and clean
    under `pip-audit --require-hashes`.
  - `release.yml` replaces an 803-byte scaffold stub: tag-only publishing through an
    `environment: pypi` trusted publisher, a manual `workflow_dispatch` TestPyPI dry run, and the
    same pinned build chain in both.
  - `ci.yml`'s `build` job now uses that pinned chain with `--no-isolation`, and `security` audits
    the lock rather than an environment containing only `pip-audit`.

### Changed
- `pytest>=9.0.3,<10`, up from `>=8,<9`: PYSEC-2026-1845 affects pytest through 9.0.2, and the
  old range admitted only vulnerable versions. Matches the pin every other repository in the
  suite carries.
- `[tool.coverage.run] source` names the importable package `loadcoach` rather than
  `src/loadcoach`, with a `[tool.coverage.paths]` mapping. A path-based source measures nothing
  against the non-editable install CI uses.

### Removed
- `freeweight` from the `dev` extra. It was never importable, is not published, and LoadCoach's
  contract tests do not read FreeWeight's OpenAPI snapshot — they read SetSpec's goldens. Its
  presence made `pip install -e ".[dev]"` unresolvable in CI.

### Fixed
- The PostgreSQL job had never executed a query: it set `DATABASE_URL`, while
  `weightsdb.testing.temporary_postgres` reads `WEIGHTSDB_POSTGRES_URL`, and under
  `WEIGHTSDB_REQUIRE_POSTGRES=1` the unused default is a hard failure rather than a skip. The
  service container's credentials now match the URL the code actually reads.
- Four tests read the developer's real GPU through the application's own telemetry collector and
  failed with `insufficient_vram` whenever another process held the card. `tests/conftest.py`
  now pins one deterministic machine for the whole suite, which is what coding standards §5's
  injected telemetry reader is for.
- Two evidence-source rows could share one URL: a refresh that failed before anything had been
  imported left a placeholder keyed by the URL, and the first successful import added a second row
  beside it, after which every lookup by URL — routing included — raised `MultipleResultsFound`.
  The placeholder is now adopted, and `source_for_url` is deterministic where two rows do share
  one. Found by running I4 for real.
- A `source_unreachable` badge survived the source coming back. It is a statement about the
  source, never about the measurement, so a successful import now retires it and the row falls
  back to what its own age says.
- The `evidence_profile_mismatch` remedy named only `--context-size`, which sent an operator back
  to a benchmark that produced the same mismatch when the profiles also differed in `keep_alive`.
  It now names every field of the resolved profile — as a flag where FreeWeight has one, and as
  `[runtime]` configuration where it does not. Also found by running I4.
- Phase 5, unit 8: the surface (api.md §5, §8; spec §7.2).
  - `POST /jobs` (202; a repeated key returns the original job with `X-Idempotent-Replay`),
    `GET /jobs` (filters by state, class, task and source; opaque cursor pagination),
    `GET /jobs/{id}` (the full document: state, attempts, routing summary, usage, timings,
    validation, degradations), `GET /jobs/{id}/stream` (replays the persisted events after
    `Last-Event-ID`, follows the live broker, closes on the terminal event — LCX16),
    `POST /jobs/{id}/cancel` (202, or 409 `JOB_NOT_CANCELLABLE`), `GET /jobs/{id}/explanation`
    (a lookup of the routing decision whose `job_id` matches, never a copy — LCX3).
  - `GET /queue` and `GET /system/status`: depth by state and class, oldest queued age, dispatch
    latency over the recent claims, active executions with their models, residency with idle
    times, starvation counter, circuit-breaker states, recent throughput, the control flags and
    the last recovery; `POST /queue/pause|resume|drain` write the durable flags the scheduler
    reads every second. Health gains the `queue` component (degraded on starvation, on depth
    past four fifths of `max_depth`, or on an open breaker).
  - Jobs, job detail and Queue pages on MirrorWall's shell; `loadcoach job
    submit|list|show|cancel|wait`, `loadcoach queue status|pause|resume|drain` and
    `loadcoach models residency` (LC16), all mode local.
  - Durable idempotency for the synchronous endpoints (LCX19): the job row is reserved before
    execution, so a repeated `idempotency_key` finds it through the unique index whether the
    execution is running or long finished, and a key past `queue.idempotency_ttl_hours` is
    released. `POST /generate` returns the original job; `POST /generate/stream` attaches to its
    event stream. `routing` and the terminal `result`/`error` frames are persisted as job
    events and published after commit; token frames are fanned out live and never stored, so a
    reconnect replays the persisted frames it missed and the result that carries the whole
    output. The in-memory 64-entry stream registry is gone.
- Phase 5, unit 7: cancellation and recovery (`services/recovery.py`; queue §8, §10).
  - `cancel_job`: a waiting job is cancelled at once; a job a worker holds moves to `cancelling`
    with `cancel_requested` set and the in-process token cancelled through an `on_request` hook,
    so it stops within one chunk with the partial response preserved on the attempt; a request
    from another process reaches the row and the lease keeper carries it to the token within one
    renewal interval. Idempotent; a terminal job is `JOB_NOT_CANCELLABLE`. The worker also
    honours a cancel between claim and routing (`leased → cancelling`), during a model load
    (`admitted → cancelling`) and during a retry backoff (`retrying → cancelling`, ADR-0036 §2).
  - The `cancelling` watchdog on the scheduler forces `cancelling → cancelled` after
    `queue.cancelling_watchdog_seconds` and records that it did; the worker's late write is
    refused by the state fence.
  - `recover`: queue §10 in order — every lease not held by a worker of *this* process is
    released whether or not it has expired (the process is gone), lease-holding jobs return to
    `queued` or fail with `worker_lost` by their idempotency, `cancelling` jobs complete to
    `cancelled`, waiting jobs are re-evaluated through the scheduler's own function, and the
    same ageing sweep runs. Idempotent, logged as a reconciliation summary, run by
    `QueueRuntime.start()` before any worker can claim, and reported on the runtime as
    `last_recovery`.
  - Proven: cancellation from every state by simulation, including between claim and execution
    and during a load, with no orphaned resident model; the watchdog ending a job whose provider
    never reaches a chunk boundary; restart recovery from seven lifecycle points (`queued`,
    `leased`, `admitted`, `executing`, `retrying`, `cancelling`, `waiting_resources`) with every
    job completed exactly once or, for non-idempotent work, failed with `worker_lost` and never
    re-run; and a **real `kill -9`** of a child process mid-execution and mid-load, recovered in
    the parent with the job completing exactly once. Recovery of 1 000 in-flight jobs is a
    performance test against the 2 s budget.
- Phase 5, unit 6: retries, fallback and the circuit breaker (`domain/retry_policy.py`,
  `domain/circuit_breaker.py`; queue §7).
  - `domain/retry_policy.py` is queue §7's failure table as a pure decision: a timeout retries
    the same model up to the profile's per-candidate limit with exponential, jittered backoff and
    then falls back; a connection error falls back at once; a protocol error retries once; a
    validation failure retries correctively up to the profile's limit; a context overrun never
    retries the same model and falls back only to a candidate serving a larger context;
    cancellation is terminal; exhausting every candidate fails with `ALL_CANDIDATES_FAILED` and
    every attempt in the event. The worker applies the table across the ranked candidates, with
    the job's total attempt bound on top and the jitter draw injected.
  - `domain/circuit_breaker.py`: a per-model `closed → open → half_open` state machine over a
    window of attempt outcomes — opens at half the window's attempts failing over at least five,
    excludes for a five-minute cool-down with the reason and expiry, then lets exactly one job
    through as the probe; a successful probe closes it (and the failures that opened it stop
    counting), a failed one re-opens it with a fresh cool-down. Phase 5 feeds it `job_attempts`
    outcomes (`breaker_samples`); the source is a callable P7 swaps for `reliability_stats`.
    `ConstraintInputs.open_circuit_breakers` is now populated, and the `recently_failing`
    rejection carries the breaker's state, reason and expiry into the routing explanation.
  - Proven by simulation: timeout retry-with-backoff then fallback; connection error falling back
    at once and a protocol error retrying once; context overrun falling back to the wider
    candidate on a provider that serves each model's own maximum; every candidate exhausted with
    every attempt recorded; and the breaker opening after five failures, excluding the model with
    its reason visible in the skipped job's explanation, and re-probing after the cool-down.
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
