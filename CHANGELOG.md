# Changelog

All notable changes to `loadcoach` are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning follows
[Semantic Versioning](https://semver.org/), pre-1.0 per
packaging and release standards §3.

## [Unreleased]

## [1.5.0] — 2026-09-10

### Added

- **`POST /generate/stream` and `GET /jobs/{id}/stream` stream thinking live** as an
  `event: thinking` frame, `{"delta", "index"}` inside the event envelope, one per reasoning delta
  the provider streams (ADR-0132). ModelRack's Ollama and llama.cpp providers already yielded
  these deltas; LoadCoach collected them into `result.reasoning` and forwarded none, so a caller
  saw a thinking model's reasoning only after its answer. `index` counts thinking frames alone, so
  `token` indices are unchanged; a provider with no reasoning channel sends no frame; like
  `token`, the frame is live and not replayed, and `result.reasoning.summary` is unchanged.
  Additive inside `/api/v1`: a client that ignores unknown events, as API standards require, sees
  no difference. Found by WeightRoomGym row W6 capturing a real stream on the reference machine.

## [1.4.0] — 2026-09-09

### Changed

- **BREAKING (CLI JSON): `loadcoach token list --json` wraps the list in `items`, not `tokens`** —
  the collection envelope API and contract standards require, which CLI standards carry to
  `--json` output (ADR-0131). Each record is unchanged. A script reading `.tokens` reads `.items`.
- Released as a **minor**, not a major, by the operator's explicit decision recorded in ADR-0131:
  there are no users of this output yet, and its one consumer (WeightRoomGym) reads both names.
  This is a deliberate, recorded exception to packaging and release standards §3.2.

## [1.3.1] — 2026-09-09

### Added

- **`loadcoach config schema --json`** (ADR-0127 rule 1): one versioned document —
  `Settings.model_json_schema()`, the runtime-changeable registry, the security-relevant keys,
  every other key, which provider form (`singular` or `plural`, ADR-0077) is effective, the source
  of every leaf, and any unknown key in the file, reported rather than dropped. Built for
  WeightRoomGym's settings form (row WS2), which hardcodes none of LoadCoach's configuration
  surface.
- **`loadcoach config validate --file <path>`** (ADR-0127 rule 2): validates an arbitrary
  candidate file through the same parse, validation and security refusals as startup, without
  touching the installation's own `config.toml`. Without `--file` the verb keeps its present
  meaning.
- `loadcoach.config.leaf_keys()` and `load_settings_tolerant()`, and
  `loadcoach.services.settings.database_overlay()` and `config_schema_document()` — the objects
  the two new verbs are built from, reused by `config show` and the configuration reference
  rather than duplicated.

## [1.3.0] — 2026-09-09

### Added

- **Per-model `kv_cache_precision` and `flash_attention`** in `[runtime.models."<canonical id>"]`,
  merged through the existing resolution chain (ADR-0120, row N6). Two new hard constraints,
  evaluated on the *resolved* profile right after `model_disabled`: `runtime_setting_unhonoured`
  (an Ollama registration asked for either — its settings are daemon-wide) and
  `kv_cache_needs_flash_attention` (a quantized cache without flash attention, which llama.cpp
  would silently serve at f16). Both surface in `route explain` as named rejections. `[runtime]
  kv_cache_precision` is now one of `""`, `f16`, `q8_0`, `q4_0`, and a quantized default without
  `flash_attention = true` is refused at load.
- **`loadcoach models show` prints `runtime_profile`** — the profile the two configuration levels
  resolve to for that model, with its `profile_hash` — and `runtime_profile_refusal` when its
  provider cannot serve it as stated.
- **`memory_max_bytes` / `memory_high_bytes` on a `llamacpp` registration**: the host-memory cap
  ModelRack 0.8.0 applies to every server it launches (ADR-0119). Requires `modelrack>=0.8`.
  `--fit` stays at llama-server's default here (ADR-0121 §3): a served model that runs slowly beats
  one that does not run; the cap is what protects the host.

- A **Providers** page and `GET`/`PUT`/`DELETE /api/v1/providers`: the `[providers.<name>]`
  registrations are now editable from the web admin, and the configuration file stays the source of
  truth (ADR-0117). A write edits only the provider tables — comments, key order and formatting
  everywhere else survive it — is validated by loading the candidate document through the ordinary
  precedence chain, keeps the previous file as `config.toml.bak`, and re-registers the running
  server so the change is live without a restart. `providers.allow_remote` stays config-only: a
  registration marked remote can be added here and stays unroutable until a human edits the file.
  A file that changed since the page was rendered is a `409 CONFLICT`, not an overwrite.
- A discovered model can be **disabled** (ADR-0118): `POST /api/v1/models/{model_ref}/enabled`, and
  a button per row on the Models page. Routing rejects a disabled candidate as `model_disabled`
  before it evaluates availability, so an explanation names the person who excluded it; asking for
  one by name is refused rather than answered by a substitute. The row, its evidence and its
  history all stay, and discovery never writes the flag.
- The Models page gained a **Scan** button over the existing `POST /models/discover`, and a **Warm**
  button that loads a model by enqueuing one small pinned `general.chat` job — the ordinary path,
  which already owns admission, residency and eviction.
- `close_registrations` moved from `web/app.py` to `infrastructure/providers/factory.py`, which is
  where a provider handle is built and therefore where releasing one belongs. Shutdown behaviour is
  unchanged; the provider admin calls it too, so an edit that replaces a supervising provider does
  not orphan the `llama-server` it was supervising.

### Changed

- One `loadcoach.cli._backend` (`load_settings_or_exit`, `open_database`, `open_loaded_database`)
  replaces the eleven copies of "resolve configuration and open the database, or exit 3" the CLI
  command modules each carried. Exit codes and messages unchanged.
- `circuit_breaker` and `models` docstrings describe the shipped application (the breaker is fed
  from `reliability_stats`; discovery writes `models`), not the phase plan that built it.
- Small tightenings ruff's SIM/PERF/C4 families flagged. No behaviour change.

## [1.2.0] — 2026-09-08

### Added

- `FreeWeightClient.version()` negotiates FreeWeight's `GET /version` (ADR-0013) before any
  evidence is read, cached with a TTL on the client — the same shape PromptCadence's and
  IdeaPress's LoadCoach clients already use, transcribed a third time. A served-majors list
  excluding `v1`, or a FreeWeight too old to serve `/version` at all (a 404), is refused as
  `EvidenceSourceIncompatible` (`API_VERSION_UNSUPPORTED`), naming both versions, before the export
  endpoint is ever fetched. Wired into the evidence-import CLI command, the
  `POST /evidence/import` route, and `refresh_from_freeweight`'s periodic pull; a refusal there
  leaves the previous import untouched, exactly as a refused URL does — no staleness claim about
  measurements that are not the problem. Closes the last `untested — behaviour not implemented`
  cell in `graceful-degradation.md`'s row index, found at row L8 (row M2).

## [1.1.6] — 2026-09-07

### Changed

- `setspec` floor raised from `0.5` to `0.6`: this application imports the `capability.evidence`
  1.1 sibling classes (`EvidenceBundleV1_1In`/`Out`, ADR-0068), which setspec 0.6 introduced, so
  a resolver honouring the old floor installed a build that failed at import. Found by the
  cross-repository compatibility matrix's `lowest` cell (row L6).

### Added

- `tests/unit/test_readme_version.py` — asserts the version README.md states after its `Status:`
  line equals `__about__.__version__`, so a release cannot leave the README stale (M9 re-audit,
  row L7).

### Fixed

- README's `Status:` line still read `1.1.3` in the repository with `v1.1.2` tagged and PyPI
  serving `1.0.0`; it now states `1.1.5` in the repository, tagged locally as `v1.1.5` (not yet
  pushed), with PyPI still serving `1.1.4`.
- `docs/upgrading.md`'s migration-notes table stopped at `1.1.3`; it now covers `1.1.4` and
  `1.1.5` (both dependency-only, no operator behaviour change).

## [1.1.5] — 2026-09-07

### Changed

- `mirrorwall` floor raised from `0.2` to `0.2.2`: the two earlier releases pin `setspec<0.5`,
  below this application's own floor, so the declared lowest range could never resolve (found by
  the cross-repository compatibility matrix, row L6).

## [1.1.4] — 2026-09-07

### Added

- `tests/unit/test_every_command_has_help.py`, walking `typer.main.get_command(app)`
  recursively so every command and option must carry help text (M9 audit Group 5, item D4).

- `tests/unit/test_troubleshooting_covers_doctor.py`, ported from FreeWeight, holding
  `docs/troubleshooting.md` to naming every check `doctor` runs (M9 audit Group 5, item D6).

- `docs/upgrading.md` refreshed to cover every shipped version (M9 audit Group 5, item D9).

- `.github/workflows/release.yml` now writes `SHA256SUMS` over `dist/*` and attaches it
  alongside the wheel and sdist on the GitHub release (M9 audit Group 5, item R4).

- A `## Compatibility` table in `README.md` listing every declared suite package range
  from `pyproject.toml`, and `tests/unit/test_readme_compatibility.py` asserting the two
  cannot drift (M9 audit Group 5, item R3).

- `tests/integration/test_downgrade_and_schema_ahead.py` drives the packaging standards §6.1
  downgrade drill end to end: upgrade, write a row, back up, jump `alembic_version` ahead,
  `SchemaAhead` refuses and names both revisions and the backup directory, restore the backup,
  start again (M9 audit Group 3, item O2). `ensure_ready` did not previously detect this case at
  all for LoadCoach — it does now.
- `tests/integration/test_backup_restore.py` (new), a backup/restore round trip parametrized over
  `weightsdb.testing.temporary_sqlite`/`temporary_postgres` directly — previously only the CLI
  verb was tested, and only on SQLite (M9 audit Group 3, item O4).
- `tests/fixtures/databases/loadcoach-1.0.0.sqlite3`, a real `loadcoach==1.0.0` PyPI install
  migrated through `0006` and seeded through its own repository layer, exercised by
  `test_1_0_0_database_migrates_to_head_and_keeps_its_rows`, which proves the real `0007..0014`
  upgrade path (M9 audit Group 3, item O1). `.gitignore` now keeps `tests/fixtures/databases/*`
  out of the blanket `*.sqlite3` exclusion, the same way FreeWeight's does.

### Removed

- `pydantic-settings` from `dependencies` — declared but never imported (ADR-0114); each
  application performs its own layered configuration merge. `requirements/ci.lock`
  recompiled with pip-tools 7.6.1 on Python 3.13 (M9 audit Group 5, the pydantic-settings
  finding from row L2).

## [1.1.3] — 2026-09-07

### Changed

- **BREAKING (inside `/api/v1`): `usage.input_tokens` and `usage.output_tokens` render the string
  `"unsupported"` for an unreported count, never `null`.** All five token classes —
  `input_tokens`, `output_tokens`, `cache_write_tokens`, `cache_read_tokens`, `thinking_tokens` —
  now share one spelling for "not reported" (ADR-0016 rule 4). This reverses this morning's
  ADR-0105, which kept `null` on these two fields specifically because `/api/v1` is additive-only
  (ADR-0013). ADR-0112 records the operator's decision to make the change now anyway, as a
  one-time, explicitly-scoped exception: the surface has no external consumer yet. A client that
  modeled `input_tokens` as `int | None` will now fail on the string rather than silently reading
  `null` as if it were a measured absence; treat `"unsupported"` on any of the five classes as
  unavailable, never total it, on the job document (`GET /jobs/{id}`), the synchronous response
  (`POST /generate`), and the stream's terminal `result` frame (`POST /generate/stream`). Storage
  is unchanged: `jobs.input_tokens`/`jobs.output_tokens` and the attempts-table equivalents stay
  nullable integer columns; only the wire representation moves. `docs/openapi.json` regenerated.

## [1.1.2] — 2026-09-07

The precedence LoadCoach published, now the one it implements. Configuration standards §7 puts a
database-backed setting *between* the file and the environment — `defaults → file → database → env
→ CLI` — and `read_runtime_settings` took a stored row whenever one existed, so a `settings` row
beat a `LOADCOACH_*` variable an operator had pinned. ADR-0100 recorded that divergence against
this application while PromptCadence built the same surface correctly; this release closes it, with
PromptCadence's vocabulary transcribed key for key so an operator moving between the two consoles
reads one thing. A shadowed row is kept, not deleted: unsetting the variable makes it effective
again. No schema change, no new runtime-changeable key.

### Changed

- **The environment beats a stored row** (configuration standards §7, ADR-0100).
  `loadcoach.services.settings.read_runtime_settings` ignores a stored value whose key is set in
  the environment, and `shadowing_source` names the variable that beats it. `loadcoach serve`
  applies its flags as environment variables before the loader runs, so the check covers the CLI
  layer too; `tests/unit/test_runtime_settings.py::test_no_source_module_passes_cli_overrides`
  fails the day a module under `src/` starts passing `load_settings(cli_overrides=…)`.
- **`GET /settings` reports a row that does nothing.** Each entry in `definitions` gains `stored`
  (the row, or `null`), `source` (`"database"` or `"configuration"`) and `shadowed_by` (`"env
  LOADCOACH_…"`, or `null`); the Settings page prints the same beneath the field it belongs to.
  api.md §9 documents all three.
- **`loadcoach config show` marks database-sourced values `(database)`** and prints the stored
  value, the third of configuration standards §7's rules. A stored row the environment shadows is
  named beside the variable that beats it. The overlay never raises and never creates a missing
  SQLite file: an absent, unmigrated or unreachable database means the configured values are the
  right answer, and an inspection command must not leave a database behind that `db status` would
  call unmigrated.
- **`docs/configuration.md`** states the precedence it now implements, the `(database)` mark, and
  why `queue.paused` and `queue.draining` have no row in its tables — they are not fields of the
  settings model, so no `LOADCOACH_QUEUE__PAUSED` exists and a variable of that name is refused as
  an unknown key. Their stored row is therefore always the effective value, through the same code
  path as every other key: one rule, no per-key exception. That shape — a runtime-changeable
  setting that is **not** a configuration key — is recorded as ADR-0101, which also states the
  test for admitting another: a pause is operational state, a threshold is configuration.

### Added

- `loadcoach.config.env_var_for(path)` — the one spelling of a key's environment variable, shared
  by the loader's source tracking, the generated reference and `shadowing_source`, so no two of
  them can disagree about which variable pins a key.

## [1.1.1] — 2026-09-06

A render and a lever. `GET /models` now carries `provider_name` and `is_remote` on every entry, so
a consumer reading the remote-provider fact from the registry reads a true one — LoadCoach 1.1
recorded both columns and rendered neither in the listing, which made every remote registration
invisible until its first turn. And a task profile's `execution` block may now carry `think`,
ModelRack's three-state thinking control, overridable per request by `sampling.think` and enforced
at routing rather than at the provider edge. The five PromptCadence harness profiles ship with it
**unset**, measured rather than assumed: six runs per cell against Ollama 0.32.13 with
`gpt-oss:20b` pinned, `tools.plan` delivered 3 of 6 with `think` unset and 0 of 6 with
`think = false`, and `tools.agent.local_fast` delivered 6 of 6 either way — that model accepts the
control and does not honour it. `pyproject.toml`'s dependency ranges are unchanged; the CI lock
pins `modelrack 0.7.1`.

### Added

- **A task profile may ask for reduced thinking.** `execution.think` — `true`, `false` or unset —
  is ModelRack's `SamplingParameters.think`, name for name and state for state, and
  `sampling.think` overrides it per request exactly as `sampling.temperature` and
  `sampling.max_output_tokens` do. Unset on both sides sends no control and builds the request
  1.1.0 built, byte for byte. A `think` that is set **requires `thinking_control` of every
  candidate at routing**: a provider that cannot carry the control is rejected with
  `capability_unsupported`, `details.capability = "thinking_control"` and `details.required_by`
  naming the profile or the request — never a `CapabilityUnsupported` raised after a model has
  been chosen, and never a request that quietly differs from its profile
  ([ADR-0099](docs/adr/0099-a-task-profile-may-ask-for-reduced-thinking.md)). `thinking_control`
  is a provider flag and stays out of `requires_capabilities`, which is validated against the
  SetSpec capability vocabulary. A `sampling.think` that is neither boolean nor `null` is a
  `VALIDATION_ERROR`.

### Changed

- **`requirements/ci.lock` pins `modelrack 0.7.1`** (was `0.7.0`), one fix over it: a GGUF
  descriptor now states `head_dim` when the file omits it, without which every GGUF-served
  candidate was ineligible on any machine with GPU telemetry. `pyproject.toml`'s range
  (`modelrack>=0.7,<0.8`) already admitted it and is unchanged — the lock is what CI installs, and
  it now installs the fix by default. Recompiled on Python 3.13, as `requirements/README.md` says.
- **The five PromptCadence harness profiles ship with `think` unset**, and the `tools.plan`
  comment records the measurement that decided it rather than an assumption. Six runs per cell
  through a real LoadCoach against Ollama 0.32.13 with `gpt-oss:20b` pinned: `tools.plan`
  delivered 3 of 6 with `think` unset and **0 of 6** with `think = false`; `tools.agent.local_fast`
  delivered 6 of 6 either way. gpt-oss:20b does not honour the control — it emits its reasoning as
  content rather than on the thinking channel, which then fails `require_valid_json`. The lever is
  reachable from configuration for a model that does honour it; no shipped profile's values
  change, so no profile version moves ([ADR-0099](docs/adr/0099-a-task-profile-may-ask-for-reduced-thinking.md)
  rule 6).

### Fixed

- **`GET /models` renders `provider_name` and `is_remote` on every entry**, under the names the
  generate response's `model` block already uses, and `loadcoach models list --json` renders them
  too. LoadCoach 1.1 recorded both columns and rendered neither in the listing, so a consumer
  reading the remote-provider fact from the registry — PromptCadence does, by
  [ADR-0098](docs/adr/0098-promptcadence-1-0-ships-with-remote-tiers-refusing-honestly.md) rule 1 —
  read `false` whatever was registered, and a remote registration stayed invisible until its first
  turn. `""` and `false` still mean *not recorded* for a row discovered before registrations had
  names, and are never guessed at from the provider kind
  ([ADR-0055](docs/adr/0055-loadcoach-registers-providers-by-name-and-kind.md) rule 4,
  [ADR-0099](docs/adr/0099-a-task-profile-may-ask-for-reduced-thinking.md)). `docs/openapi.json`
  does not move: `GET /models` returns an untyped mapping and describes no entry field.

## [1.1.0] — 2026-09-06

LoadCoach 1.1: **LA2 and LA3's consumer half** — more than one provider, an operator's adapter
directory, adapter subjects that route and pin like any other candidate, a residency model in which
switching adapters on a warm base is free, and **imported evidence that binds to the subject it was
measured on**. Phases 10 and 11 of the
[development plan](docs/apps/loadcoach/development-plan.md).

**LA3's exit was demonstrated across two applications.** A `benchmark.evidence_bundle` `1.1`
exported by FreeWeight on the same machine was carried as a file — nothing else crossed, no shared
code and no shared database — imported through `loadcoach evidence import`, and its three records
bound to three subjects with zero rejections. A measured adapter subject was then selected because
of that evidence, with `benchmark` as the signal's source in the explanation, while the sibling
measured nowhere was rejected `adapter_unmeasured` in the same decision (I18).

**LA2's exit was demonstrated too, and separately.** Three requests pinning three LoRA adapters on one base
were answered by **one** `llama-server` process — asserted from the supervisor's pid, from
`list_resident`, and from three visibly different answers to one prompt — and every attempt
recorded the subject that answered. The same weights registered a second time under a registration
**declared** remote left three `adapter_classification_conflict` rows in `routing_candidates`, each
carrying the classification arithmetic, while the bare base stayed servable: I16 and I19, proved
where the behaviour lives.

**Two unreleased fixes ship in this release rather than riding silently:** `2c7d740` — a
synchronous generation records the model it made resident, so the *next* request can apply the
residency exception instead of being refused `insufficient_vram` by memory the previous one is
still holding — and G2's tool wire, which is what makes model-directed sandboxed tool use reachable
on a real provider at all.

### Added
- **The evidence uniqueness key carries the adapter** ([ADR-0085](docs/adr/0085-the-evidence-uniqueness-key-carries-the-adapter.md),
  [ADR-0086](docs/adr/0086-the-consumers-adapter-key-column-is-not-nullable.md); migration `0013`).
  `capability_evidence` gains `adapter_artifact_digest`, and a base and every adapter subject
  measured on it are now separate rows rather than one key the duplicate detector collapses. A real
  FreeWeight `1.1` bundle carrying three subjects previously imported one record and rejected two.
  The column is `NOT NULL` with `''` for the bare base: this table is written through an
  `INSERT … ON CONFLICT` upsert, and a conflict target containing a `NULL` never fires, so a
  nullable column would insert a second row on every re-import instead of updating the first.
  Existing rows are bare-base subjects and are unchanged.
- **Adapter-bearing evidence binds to its subject** ([ADR-0058](docs/adr/0058-the-execution-subject-gains-an-adapter-axis.md) §4;
  migration `0014`). `capability_evidence` gains a nullable `adapter_id`, and binding resolves both
  axes of a subject: the base by ADR-0022 §4's four rules, the adapter by artifact digest — never by
  name. A record naming an adapter this operator does not hold is retained `unmatched` with a note
  naming it and binds on the next directory scan with no re-import, exactly as evidence for an
  undiscovered model does. It replaces the rule that retained *every* adapter-bearing record
  `unmatched`, and it never attaches an adapter's measurement to the bare base.
- **A subject's own evidence scores it** ([ADR-0081](docs/adr/0081-an-adapter-subject-inherits-no-evidence-from-its-base.md)).
  Routing reads imported evidence keyed on the subject, `(model_id, adapter_key)` — the shape
  `reliability_stats` already uses — so a base candidate takes the base's measurements and an
  adapter candidate takes its own. A measured adapter now passes `require_adapter_evidence`, which
  is unchanged; an unmeasured sibling on the same base is still rejected `adapter_unmeasured` in the
  same decision. `loadcoach evidence show`, `GET /evidence` and the evidence page name the
  **subject** rather than the base, so two measurements on one base read as two things.
- **`adapters.measured`, a twenty-first shipped task profile.** The profile to route with after
  importing FreeWeight adapter evidence. Its weights are the A-2 regression panel's two fixed
  suites — the only capabilities FreeWeight measures on *every* adapter subject it measures at all —
  so it finds a measurement whatever an adapter was trained for. It sets **no**
  `min_context_tokens`, deliberately: a minimum makes LoadCoach configure a served context, which
  enters `runtime_profile_hash`, and evidence measured under a different profile is excluded by name
  ([ADR-0023](docs/adr/0023-runtime-profile-resolution.md)). A deployment that pins a context here
  must pin the same one in FreeWeight's `[runtime]`.
- **`loadcoach adapters sync`.** Registers the reviewed manifests and binds any evidence that was
  waiting for them, reporting both counts. The same pass already ran at server startup and before
  every routed decision, so nothing here is new behaviour — but the sequence an operator actually
  performs (import a bundle, review a manifest, expect the evidence to attach) previously depended
  on a routing call happening next, which is a side effect rather than an instruction.
- **A caller may declare its own data classification, and the effective classification is the join**
  (ADR-0065 rule 2). `POST /generate` and `POST /jobs` take an optional `data_classification` —
  `public`, `internal` or `confidential` — and LoadCoach records `max(caller, adapter)` as the
  attempt's `effective_data_classification` and in the detail of any
  `adapter_classification_conflict` rejection, where `caller_classification` was previously always
  `null`. The field is **optional** and a body that omits it behaves byte-identically to before it
  existed, because the join with an absent left-hand side is the adapter's own value. A value
  outside the vocabulary is refused rather than ignored: ignoring it is the one direction that can
  only *lower* the effective classification. Nothing about the field widens anything — a `max()`
  can only make an adapter candidate less eligible, never more.
- **`[routing] task_profiles_path`** — a `task_profiles.toml` of the deployment's own, imported at
  startup instead of the shipped one. Until now `bootstrap` read the file inside the installed
  package and no key named another, so a deployment whose routing policy differed from the shipped
  profiles was not expressible: the only ways to change it were to edit an installed package or a
  stored row. The import is the same upsert per `(profile_id, version)`, so a file naming a shipped
  profile replaces it and one naming a new id adds it, and every command that imports profiles —
  `serve`, `tasks`, `job`, `route`, `generate` — reads the same resolved path. A configured path
  that is not a file is refused at startup rather than falling back to the shipped profiles: an
  operator who named a file and silently got the defaults would be routing under a policy they did
  not write, with no way to tell from the outside.
- **`kind = "llamacpp"` is a provider LoadCoach can construct.** It launches and supervises its own
  server over a directory of GGUF weights (`model_directory`, required — a wrong directory is a
  server serving weights nobody asked for, and there is no default worth guessing; plus optional
  `state_dir` and `server_path`). It is the only kind that can hot-swap adapters, and therefore the
  only kind an adapter subject can ever be served by, which is what makes ADR-0065's local-only
  rule hold by construction rather than by a check.
- **Adapter subjects are routing candidates, and three new constraints reject them by name**
  (ADR-0058, ADR-0064, ADR-0065, ADR-0079, ADR-0080). A candidate is now the triple
  `(identity, adapter | none, resolved runtime profile)`. Where a registration's provider declares
  `adapter_hot_swap`, the candidate list gains one subject per `(base, adapter)` pair **beside**
  the bare base — never instead of it — and a provider that cannot hot-swap contributes none at
  all, which is what keeps an adapter local by construction. With no adapter the canonical subject
  string is byte-for-byte the model's canonical ID.

  Three rejections join routing §4's table, each naming a different remedy:
  `adapter_incompatible` (the manifest's base digest is not the base being served, or the provider
  cannot hot-swap — no configuration makes it eligible), `adapter_unmeasured` (from the new
  `[routing] require_adapter_evidence`, **on by default**: until FreeWeight measures adapters every
  adapter subject is unmeasured, so adapters are invisible to *routed* selection while pins keep
  working) and `adapter_classification_conflict` (an adapter is a local-only artifact, so a
  candidate a remote registration would serve is refused — deliberately not `excluded_by_policy`,
  because that one is fixed by turning remote on and this one cannot be fixed by any flag). Every
  rejection is persisted with the numbers that caused it and is queryable.

  An adapter subject inherits **no** evidence from its base: its only signals are the vocabulary
  terms its manifest declares. A benchmark taken on bare weights describes bare weights, and
  attributing it to a subject running a LoRA nobody measured is the mis-binding ADR-0058 §4 refuses.
- **An adapter pin selects a subject, and every attempt records what answered** (ADR-0064 rule 4,
  ADR-0080). `overrides.adapter` names an adapter by its manifest name and has `model`-pin
  semantics: it bypasses scoring — every other subject, the bare base included, leaves the pool —
  and bypasses `require_adapter_evidence`, because that gate filters *routed* selection and a pin
  is not routed selection. It does **not** bypass a hard constraint: an incompatible pin is
  refused by name with both digests, and a pin naming an adapter no provider holds is
  `ADAPTER_NOT_FOUND` listing what does exist, never a silent fall back to the bare base.
  `overrides.ignore_residency` is accepted and recorded alongside it.

  `job_attempts` and `jobs` gain the subject (migration `0010`): `adapter_id`,
  `subject_canonical_id`, and the adapter's own and effective data classifications — written on
  every attempt that used an adapter even though the local-only rule makes the lattice hold by
  construction, because an invariant nothing records is an invariant nobody can check
  (ADR-0065 rule 4). The `/generate` response and the job document carry `subject_canonical_id`
  and an `adapter` object read through the foreign key, never parsed out of the subject string.
- **`loadcoach route explain --adapter NAME`**, and `--ignore-residency`. The command now builds
  every registration and syncs the adapter directory first, so a one-shot explanation describes
  the same pool a running server would.
- **A permanent provider refusal ends the job instead of falling back.** `ADAPTER_NOT_FOUND` and
  `PROFILE_MISMATCH` from a provider are permanent for the request as written, so they are never
  retried and never trigger a fallback — the job fails with its attempts written (api.md §10).
- **The `adapters` table** (migration `0009`), the projection of the operator's directory that
  routing reads — reading the directory means hashing every artifact, which no routing decision may
  do. Identity is the artifact hash; a row whose adapter has left the directory is kept and marked
  unavailable rather than deleted, because a stored decision names it.
- **Every routing row names its subject twice** (ADR-0080): `adapter_id`, which answers *which
  adapter* after a rename, and `subject_canonical_id`, the string written at decision time so an
  explanation still reads correctly after the directory has changed underneath it. Existing rows
  are backfilled from `models.canonical_id`, which is what a bare base's subject string is.
- **`output.tool_calls_assembled`, beside the fragments** (ADR-0078). A provider emits one call as
  several deltas and not every delta carries the id — Ollama's adapter sends id and name first and
  the argument text second with no id at all — so grouping on the id splits one call into a named
  call with no arguments and a nameless one with the arguments. That is a defect a real caller
  shipped against a real model, so the assembled shape is now the response's own: one entry per
  call keyed on `call_index`, with `arguments` parsed where they parse and kept as the raw string
  where they do not. **`output.tool_calls` is superseded** — kept, documented, and removed at
  LoadCoach `2.0`; a minor does not break a shipped field.
- **The models view groups adapter subjects under their base**, each row naming the subject, the
  evidence source routing would use for it (`declared` where the manifest claims vocabulary terms,
  `absent` where it claims none — never `benchmark`, because nothing measures adapters until LA3),
  its base-identity confidence, its classification and the registration that serves the base.
  `GET /models` carries the same under `adapters`.
- **The `/generate` response names the subject**: `model.subject_canonical_id`, `model.adapter`,
  `model.provider_name` and `model.is_remote` beside the canonical ID, and every candidate in a
  persisted explanation carries its subject string, its provider name and its adapter.
- **Reliability and the circuit breaker key on the subject, never the base** (ADR-0067). A failing
  `(base, adapterA)` is deprioritized and eventually broken **as that subject**: it never breaks
  the bare base and never breaks a sibling adapter, which is the whole point — one bad adapter must
  not take a base and its four other adapters out of service. `reliability_stats`' uniqueness moves
  to `(model_id, adapter_key, task_profile_id, window)` (migration `0012`), the breaker's samples
  and verdicts key on the subject string, and `GET /reliability` reports one entry per subject,
  ordered so an adapter sorts immediately after the base it runs on.

  Existing rows are base subjects and the migration says so rather than guessing: every one was
  computed from attempts on a bare base, because no adapter could be applied, and not a single
  count moves. The cost is sample fragmentation, and it is reported rather than hidden — the
  20-sample minimum applies per subject, so an adapter carries `low_evidence` until it has earned
  its own numbers, and nothing is pooled from a neighbour to fill the gap.
- **Residency is two-level: the base is the expensive switch** (ADR-0066). The factor is
  `1 + prefer_resident_bonus` for a candidate on the resident base **whatever adapter it names**,
  `1 - base_switch_penalty` for one that would need its own base loaded while another is resident,
  and exactly `1.0` where nothing is resident — residency unknown and residency empty are the same
  evidence, and there is no swap to charge for. `overrides.ignore_residency` zeroes both terms and
  is recorded, not merely acted on: every candidate carries a `residency_detail` naming the level
  applied and both knobs' values, in the explanation and in `routing_candidates`
  (migration `0011`).

  An adapter switch on a resident base writes no residency row and triggers no unload: the episode
  already there is updated to name the subject that last used it (`adapter_id` and `adapter_key`,
  which the unique key now includes). `adapter_key` is the empty string for a bare base rather than
  `NULL`, because `NULL`s in a unique index do not constrain and a key that admits duplicates is
  not a key (ADR-0080 rule 5) — a deliberate wart, documented beside the key it exists for.
- **`[routing] base_switch_penalty`** (default `0.10`, chosen and not measured) and
  **`[routing] require_adapter_evidence`** (default `true`).
- **`RuntimeProfile.adapters_registered` is set, never guessed** (ADR-0074): `True` for a
  hot-swapping registration holding adapters, `False` for one holding none, and left unstated for a
  provider that has no concept of adapters — so every profile hash a deployment without adapters
  has ever stored is unchanged. It is derived from what LoadCoach handed the provider, never from a
  `list_adapters()` snapshot, which moves while a restart is pending.
- **The adapter registry is an operator's directory and a reviewed manifest** (ADR-0061).
  `[adapters] directory` — empty by default, and **empty means the whole feature is off**. The
  directory holds artifacts and one reviewed `model.adapter_manifest` 1.0 per adapter, read and
  validated through SetSpec rather than re-defined; the conversion into ModelRack's
  `AdapterRegistration` happens here, in the application, because ModelRack never reads a
  directory. Every registration whose provider declares `adapter_hot_swap` is offered what the
  directory holds, so a provider that cannot hot-swap — every remote one — is never handed an
  adapter at all.

  **Identity is the artifact hash and the path is a locator.** A renamed artifact whose manifest
  still names the old path makes that adapter unavailable, named, until a rescan; an *edited*
  artifact is a different adapter and is refused, because measurements attached to the old hash
  must never be re-attributed to new weights. Both are fail-closed, and `loadcoach doctor` names
  each one.

  `loadcoach adapters scan|list|show`. **`scan` drafts; a person keeps**: it hashes each artifact,
  reads a sibling `adapter_config.json` for a base *name* (never a proof), writes
  `data_classification: confidential` so a reviewed value can only be relaxed on purpose, and
  leaves `declared_capabilities` empty because a capability claim decides what gets benchmarked
  and routed and is a person's assertion, not a scanner's guess. A draft is never registered —
  enforced by its `.manifest.draft.json` suffix, not by a flag inside the document.

- **Providers are registered by name and kind** (LC-E1, ADR-0055). `[providers.<name>]` blocks each
  carry a `kind`, their connection settings and a **declared** `remote` flag — never inferred from
  the kind or the URL, so an OpenAI-compatible endpoint on loopback is local and the same kind
  pointed at a hosted API is remote. Discovery from every registration enters one registry, tagged:
  `models` rows record the registration that served them and its egress class, routing evaluates
  each candidate against *its own* registration's capabilities, and execution calls the provider
  that serves the selected model. Residency is per registration, because each provider loads and
  evicts its own models.

  **The singular `[provider]` block keeps working, and both forms together are refused** (ADR-0077).
  A 1.0 configuration is exactly one registration named after its kind, declaring `remote = false`,
  and produces a byte-identical registry — asserted by a compatibility golden, not argued. A file
  carrying both shapes is a startup error naming the singular block, every named block and the
  one-line fix: the half-migrated file is the case a precedence rule would answer silently.

  `[providers] allow_remote` keeps its meaning as cross-provider policy and is evaluated **above** a
  registration's own flag, so a remote registration in a deployment that disallows remote is
  configured, visible, and rejected by the existing `excluded_by_policy` constraint.

  **One unreachable registration no longer empties a working registry.** Discovery marks a model
  unavailable only when a registration that *answered* stopped reporting it; a registration that
  could not be listed is named in the outcome's new `unreachable` field and its models are left
  exactly as they were. With a single registration that raises, the 1.0 behaviour is unchanged.
  `loadcoach doctor` reports each registration's reachability by name.
- **`POST /generate` and `POST /jobs` carry tool definitions** (G2). A body may now supply
  `tools` — a list of `{"name", "description", "parameters"}` — and they reach the provider
  unmodified: LoadCoach does not validate a tool's `parameters` schema, rewrite it, or execute a
  call (ADR-0041, spec §14). The calls a model requests come
  back at `output.tool_calls`, as they have since M4; what did not exist until now was any way to
  tell the model which tools exist, so a model invented names out of its own vocabulary and every
  call it made was refused.

  **A request carrying tools requires `tool_use` of every candidate** (ADR-0075). A non-empty
  `tools` imposes the capability as a hard constraint on that request, on top of whatever the task
  profile requires, so a candidate whose provider cannot use tools is rejected by routing with
  `capability_unsupported` and `details.required_by = "request"` — never served a request whose
  tools quietly evaporate, and never a `CapabilityUnsupported` from the provider edge after a model
  has already been chosen. `tools: []` is identical to `tools: null` and to the field's absence.

  **Additive within `/api/v1`**: no field removed, no existing field's type changed, no new route.
  A body with no `tools` builds byte-for-byte the `GenerationRequest` it built at `dfbf2d8`, pinned
  by a contract golden captured from that commit
  (`tests/fixtures/contract/generation_request_dfbf2d8.json`).

- **A transcript carries an assistant turn's tool calls** (G2). `messages[].tool_calls` — a list of
  `{"id", "name", "arguments"}` — replays what a model asked for, so a turn that answered with
  calls and no text goes back on the wire as it happened instead of being rendered as text. That
  turn is the one that could not be replayed at all before: ModelRack refuses an assistant message
  with neither content nor calls. `arguments` may be the parsed object or the raw text the model
  produced, and the raw form is kept as `raw_arguments` rather than smoothed into an empty mapping.

  The transcript's consistency is checked at the edge and refused as `VALIDATION_ERROR` with
  `details.fields` naming the turn: `tool_calls` on a non-assistant turn, a `tool` turn with no
  `tool_call_id`, a turn with neither content nor calls (ModelRack's three rules, surfaced rather
  than reaching the provider), and a `tool_call_id` naming no call in an earlier assistant turn
  (LoadCoach's own — an unmatched id is a caller bug a provider would turn into a confusing model
  failure). `jobs.request_json` carries the calls, so a queued job replays what it was submitted
  with; a row written before the field existed reads back exactly as it did.
- **Five shipped task profiles for PromptCadence's harness tiers**, taking the shipped set from
  fifteen to twenty: `tools.agent.local_fast`, `tools.agent.local_large`,
  `tools.agent.remote_cheap`, `tools.agent.remote_frontier` and `tools.plan`. They are namespaced
  specializations of `tools.agent` in `src/loadcoach/config/task_profiles.toml` and they exist here
  rather than in PromptCadence because [ADR-0047 §1] makes a harness tier configuration over
  exactly one LoadCoach task profile — the harness performs no routing maths, so its tier
  vocabulary has to be expressible in this application's profile grammar or not at all.

  What the four `tools.agent.*` profiles differ in is what LoadCoach can actually express: a
  latency ceiling (60 / 300 / 120 / 300 s), a `min_context_tokens` equal to the tier's context
  budget (16384 / 32768 / 128000 / 200000), minimum capability scores, and
  `allow_remote_providers`. There is no vocabulary here for model size, so those three constraints
  *are* the distinction between a fast and a large local tier rather than an approximation of one;
  `local_large` additionally leans its weights towards `reasoning` and doubles `max_output_tokens`.
  The two remote profiles ship now and route to `NO_ELIGIBLE_MODEL` until a remote provider is
  registered, which a caller sees rather than silently getting a local model.

  `tools.plan` is the planner's intent: `response_format = "json"` with
  `validation.require_valid_json`, and deliberately **no** schema — the plan document's shape stays
  the harness's, validated there, so a corrective retry against a schema this application does not
  own is not offered.

  Configuration and its documentation only; `src/loadcoach/**/*.py` was not touched. No version
  bump — this rides the next release.

  [ADR-0047 §1]: docs/adr/0047-a-tier-is-configuration-and-a-model-never-sizes-its-own-budget.md
- **The ADR-0026 §3 fetch vectors are now one fixture shared byte-for-byte with ToolYard**
  (`tests/fixtures/fetch/adr0026_vectors.json`, driven by
  `tests/integration/test_adr0026_shared_vectors.py`). The same outbound checks defend against the
  same class of request in both places — this application pulling an evidence bundle from a URL a
  request body supplied, and ToolYard's `http_fetch` fetching a URL a *model* supplied — and until
  now they were proven by two independent suites that agreed only as long as nobody edited one.
  Twenty-three cases (scheme, host allowlist, literal and resolved link-local addresses, the
  per-hop redirect re-check, the hop cap, the declared and the streamed size cap, the content-type
  check and the refused-versus-failed line) now run against `FreeWeightClient` here and against
  ToolYard's tool there, from the same bytes; the file's sha256 is asserted in both repositories,
  so a one-sided edit fails a test rather than becoming a divergence somebody finds in production.

  **`FreeWeightClient` passed all twenty-three unchanged** — `src/` was not touched, no reason
  string moved, and no behaviour changed. Sizes in the fixture are written relative to the
  configured cap rather than as numbers, because the cap is 128 MiB here and 8 MiB there; vectors
  belonging to only one implementation are deliberately outside the shared set (this application's
  credentials, `since` and bare-origin export path; ToolYard has no credential surface at all).

  Tests and a fixture only. No version bump: this rides the next release.
- **The declared `finish_reason` and the validation checks on the wire.** `POST /generate`'s
  response and the job document (`GET /jobs/{id}`, every `GET /jobs` item, and a replayed
  `idempotency_key`) now carry `output.finish_reason` — the provider's declared reason for the
  attempt that produced the output (`stop`, `length`, `tool_calls`, `content_filter`,
  `cancelled`, `error`, `unknown`), recorded on `job_attempts.finish_reason` since 1.0.0 but
  rendered nowhere until now — and the job document's `validation` block gains `performed` and
  `checks`, the same shape the synchronous response has always rendered. A caller that advances
  on an answer can now tell one the model chose to end from one cut off at the token limit, and
  can read the same facts back for a job it lost track of, instead of inferring either from the
  text. Surfaced by PromptCadence's Phase 3 (`docs/history/D2_HANDOFF.md` §2), whose advance contract refuses
  to read an undeclared finish as success.

  **Additive within `/api/v1`**: no field removed, no existing field's type changed, no new API
  version. A client written against 1.0.0 reads the new response unchanged.
- **All four token classes on the wire and on the rows** (ADR-0070 decision 7). `jobs` and
  `job_attempts` gain nullable `cache_write_tokens` and `cache_read_tokens` (migration `0007`),
  the `usage` object in the `POST /generate` response and in the job document
  `GET /jobs/{id}` returns gains the same two fields, and both execution paths — the synchronous
  one and the queue worker — capture them through the existing `_count` helper.

  **Additive within `/api/v1`**: no field removed, no existing field's type changed, no new API
  version. A client written against 1.0.0 reads the new response unchanged.

  The three answers a cache class can give are deliberately distinct and none renders as another:
  a number is a count the adapter reported; `0` is a real count, meaning the provider's protocol
  could not have billed that class; the string `"unsupported"` (ADR-0016 rule 4) means the class
  was never reported and is not a number. In storage the last of these is `NULL`. LoadCoach does
  not decide which of the three a response gets — ADR-0070 puts that decision in the adapter, and
  re-deciding it here would put one rule in two places.

  Without this, PromptCadence would rebuild a `TokenUsage` whose cache classes are unsupported and
  a strict money ceiling would trip on every remote turn (ADR-0069 §"Not decided here"). Until
  `modelrack 0.7.0` is released and installed here, every real adapter still reports the cache
  classes `UNSUPPORTED` and these fields render `"unsupported"` — correct, honest, and the interim
  ADR-0070 decision 8 sequences for.

  Migration `0007` adds four nullable columns with **no backfill and no server default**: an
  existing row genuinely has no value for these, `NULL` is how this schema already says "not
  reported", and a `0` default would be a fabricated zero applied retroactively to every
  historical row. The downgrade uses `batch_alter_table`, so it works on SQLite — which cannot
  drop a column with a plain `ALTER` and is the dialect a downgrade is most likely to be run on.

  The job event stream carries `summary.as_json()`, so the new fields reach job events for free.

### Changed
- **The `setspec` pin moves to `>=0.5,<0.7`, and the evidence reader adopts `capability.evidence`
  `1.1`** (H2). The adapter registry reads SetSpec's `model.adapter_manifest` 1.0 rather than
  defining a second manifest shape (ADR-0061), and that payload ships in `setspec 0.5.0`, so the
  pin E5 deliberately left at `>=0.4,<0.5` moves here rather than at H4. Moving it alone would have
  turned three local-only reds into CI reds — a bare `CapabilityEvidenceOut` permanently means
  `1.0` (ADR-0068 rule 3) and refuses the `adapter` block a `1.1` golden carries — so the reader
  and the contract test now import `CapabilityEvidenceV1_1In`/`Out` and `EvidenceBundleV1_1In`/`Out`.
  It is an import change and nothing else: every `1.0` record validates through the `1.1` model
  unchanged, and a record with no adapter dumps byte-identically.

  **Adapter-bearing evidence is retained, never attached to the base.** Evidence measured on
  `(base, adapterA)` applies to that subject and to nothing else (ADR-0058 §4), so a record
  carrying an `adapter` block binds `unmatched` with a note naming the adapter rather than raising
  the score of weights that were never measured. H4 is where FreeWeight starts producing such
  records.

- **`baseaicore` moves to `>=0.4.2`** for ADR-0074's `RuntimeProfile.adapters_registered`, and
  **`modelrack` to `>=0.7,<0.8`** for `LlamaCppProvider`, `list_adapters()` and
  `register_adapters()`. Both carry a `TODO: re-pin on publish` until the operator publishes them.
- **`tools.plan`'s output budget stays at 4096, measured** (G2, gate E). G1 reported that
  gpt-oss:20b returns an empty document under this profile about half the time and left the
  question of what to do about it to LoadCoach. Measured against the real model on the reference
  machine with PromptCadence's `planner.draft` 1.1.0 prompt (2.1k characters, six samples per
  setting, straight through Ollama so nothing but the budget varied):

  | `max_output_tokens` | empty | rate | median latency | every empty answer |
  |---|---|---|---|---|
  | 4096 (shipped) | 1 / 6 | 17 % | 58 s | `done_reason=length`, `eval_count` 4096 |
  | 8192 | 3 / 6 | 50 % | 171 s | `done_reason=length`, `eval_count` 8192 |

  Doubling the budget tripled the median latency and made the empty rate worse, because the model
  fills whatever budget it is given with reasoning and simply runs out later. The profile is therefore
  unchanged, with the numbers recorded beside it in `task_profiles.toml`. This also answers the
  question G1 could not: **the finish reason behind an empty planning answer is `length`**, with
  `eval_count` exactly equal to the budget.

  The lever that would work is a thinking control. ModelRack's Ollama adapter declares
  `thinking_control = True` in its capabilities but exposes no request-side way to ask for it —
  neither `SamplingParameters` nor the chat body carries Ollama's `think` key, and
  `runtime_profile.provider_options` merges into `options`, where `think` does not live. Recorded
  as a finding for ModelRack; no task-profile field is added for a control that cannot be sent.
- **The M5C-6/M5C-11 stopgaps are gone, closed by `mirrorwall 0.2.1`.** The job page's
  explanation links live in the definition list itself — `kv_list`'s new `href` item shape
  renders the value as a real anchor with label and value still escaped text — so the
  "Explanation" paragraph folded back into the list as two linked rows, and both page-level
  `overflow-wrap` stopgaps (`/system`, `/jobs/{id}`) are deleted: `.kv-list dd` wrapping now
  comes from MirrorWall's own `components.css`. The e2e tests assert the stopgaps *absent* and
  the decision link rendered inside a `<dd>`; `requirements/ci.lock` moves to `mirrorwall 0.2.1`
  and `weightsdb 0.2.1` (both resolve under the unchanged `>=0.2,<0.3` pins).

### Fixed
- **The adapter evidence gate admits only a signal that scores**
  ([ADR-0087](docs/adr/0087-the-evidence-gate-admits-only-a-signal-that-scores.md)).
  `require_adapter_evidence` read the candidate's raw signal list, so a benchmark that scoring
  then excluded — an unbound record, another machine, a mismatched runtime profile — still
  satisfied it and the subject was routed on whatever was left. Observed live: two adapters whose
  manifests merely *declared* the top-weighted capability scored `0.500 declared` and outranked
  the bare base, whose real measurement had been excluded and scored nothing. The gate now reads
  the **resolved** capability score, so it and the scorer cannot disagree about what counts as
  measured, and the `adapter_unmeasured` rejection carries `resolved_source`, both profile hashes
  or the foreign machine's fingerprint, and the `freeweight run start …` remedy — "nobody has
  benchmarked this" and "the benchmark does not describe this execution" are told apart, because
  they have different remedies. **An adapter measured under a runtime profile that has since moved
  is now unroutable by name until it is re-measured**, rather than degrading to a declared claim;
  a pin is unaffected, as it always was.
- **An excluded measurement no longer scores worse than no measurement at all**
  ([ADR-0088](docs/adr/0088-an-excluded-measurement-falls-back-to-the-prior-it-displaced.md)).
  A benchmark set aside by one of the three named exclusions was returned *before* the parameter
  band prior, so a subject somebody had measured scored `absent` while a subject nobody had ever
  measured scored the prior and won. It now scores the prior it displaced, at that prior's fixed
  low confidence, while keeping its own source, note, remedy and measured hash — so the
  explanation is unchanged and only the ranking moves. ADR-0017's and ADR-0023 §3's hard
  separations are untouched, and `low_evidence`, `measured_weight` and the gate above all still
  read the source rather than the number.
- **An adapter scan re-binds evidence.** `sync_adapters` now re-evaluates every evidence row's
  binding in its own transaction, because a directory scan is discovery for the subject's second
  axis. Imported evidence for an adapter this operator had not yet reviewed used to sit `unmatched`
  until something unrelated happened to the *model* registry; it now binds on the pass that first
  sees the adapter, with no re-import ([ADR-0022](docs/adr/0022-capability-evidence-record-contract.md) §4).
- `loadcoach route explain` prints each candidate's **subject**, not its base, so three candidates
  on one base no longer render as the same line three times.
- Evidence coverage counts **subjects** rather than models: a base and two adapter subjects
  measured on it are three things that were measured.
- **The 1.1 migrations run on PostgreSQL, not only on SQLite.** Three defects, each invisible on
  SQLite and each fatal on PostgreSQL, found by CI's PostgreSQL job and reproduced locally
  against `postgres:16`:
  - `0008` added `models.is_remote` with `server_default=sa.text("0")` — an integer literal for a
    boolean column, which PostgreSQL refuses (`DatatypeMismatch`). It is now `sa.false()`, which
    each dialect renders in its own terms.
  - `0012`'s unique constraint took its name from the naming convention and came out 64
    characters, one over PostgreSQL's 63-character identifier limit. It is now named explicitly,
    `uq_reliability_stats_subject_profile_window`, in the migration and in the model together.
  - `0009` and `0011` declared `sa.JSON()` and `sa.DateTime(timezone=True)` where the models use
    `weightsdb.PortableJSON` and `weightsdb.UtcDateTime`. The two agree on SQLite and disagree on
    PostgreSQL (`JSON` vs `JSONB`), so `check_parity` failed there and only there.
- **A stopped server no longer leaks its `llama-server`.** Nothing ever called `close()` on a
  provider: the lifespan released the publisher, the sampler, the queue runtime and the database,
  and dropped every provider handle. A supervising provider owns an operating-system process, and
  `LlamaCppProvider` ends its servers in `close()` and otherwise only in a finalizer — which runs
  at collection or interpreter exit, and not at all when the process is signalled. So every
  restart of `loadcoach serve` left a server behind holding the whole card. The failure that
  causes is worse to read than an out-of-memory error: the *next* candidate is refused
  `insufficient_vram` before its classification or its compatibility is ever considered, so a
  defect in shutdown presents as a defect in routing. Found by IdeaPress's LA2 journey, which left
  six orphans holding 13 GB of a 16 GB card across three runs.
- **A queued job no longer loses its `adapter` and `ignore_residency` overrides.** A leased job's
  submission is rebuilt from `jobs.request_json` and from nothing else, and neither field was
  written to it or read back — so an adapter pin submitted through `POST /jobs` was silently
  dropped between submission and execution and the request was answered by the **bare base**, which
  is precisely the fallback ADR-0064 rule 4 forbids, with no error anywhere. Found while wiring
  IdeaPress's per-stage pins, whose long stages all go through the queue.
- **A migration that adds a foreign key no longer deletes stored routing candidates.** Adding a
  constraint to an existing SQLite table is a table rebuild, and dropping `routing_decisions`
  with `foreign_keys=ON` cascades through `routing_candidates` — the explainability promise
  itself. Foreign keys are now enforced off for the duration of a migration run on SQLite, through
  the raw driver cursor, because the pragma is a documented no-op inside a transaction and the
  connection is in one by the time SQLAlchemy would emit it.
- **The corrective retry no longer crashes on an empty answer, and a refused request writes its
  attempts** (G2, found at G1: `docs/history/G1_HANDOFF.md` §9.2). `corrective_turns` appended
  `Message(ASSISTANT, content=previous_text)` unconditionally, so a model that answered with
  nothing — a reasoning model under JSON mode does, about half the time — produced an assistant
  turn ModelRack refuses. The refusal escaped mid-execution: `/generate` returned
  `VALIDATION_ERROR`, the job stayed `executing` until a watchdog or a cancel, and **its attempts
  were never written**, which is why the `finish_reason` behind those empty answers could not be
  recovered afterwards.

  Two changes, and they are separate. An empty previous answer is now described inside the
  correction prompt's `previous_output` instead of being replayed as a turn (the prompt record
  itself is unchanged — prompts are versioned, ADR-0012). And a request refused while it is being
  *built* now **fails the job with every attempt already made committed**, `error_code`
  `VALIDATION_ERROR`, `completed_at` set. It is not a provider failure and not a routing failure:
  nothing was called and no candidate was rejected. (E6, found at E4:
  `docs/history/E4_HANDOFF.md` §5). `build_provider("fake")` used to construct ModelRack's unscripted
  `FakeProvider()`, whose `DEFAULT_MODEL` declares an 8.5 GB model — so routing's
  `insufficient_vram` hard constraint rejected the only candidate whenever the host had little
  free VRAM, including plain `tools.agent`, unchanged. A provider that exists so the suite and an
  operator can be exercised without a GPU was gated on one.

  `build_provider` now declares a small model instead, built from `DEFAULT_MODEL` by
  `dataclasses.replace` (`infrastructure/providers/factory.py`): `size_bytes=47_000_000`,
  `layers=4`, `kv_heads=2`, `head_dim=64`, `parameter_count=45_000_000` — a coherent tiny-model
  shape, not just a shrunk number. At the worst case this model ever serves (`served_context`
  defaults to its own `max_context`, unchanged at 32 768, so every shipped local task profile's
  `min_context_tokens` is still satisfied), the VRAM estimate is weights 49_350_000 B + kv
  67_108_864 B + activation 268_435_456 B ≈ 385 MB — comfortably under
  `DEFAULT_VRAM_HEADROOM_BYTES` (512 MiB) below even a machine reporting ~1 GiB free. Renamed
  `fake-model:8b-q8_0` → `fake-model:tiny-q8_0`, since nothing in this repository pins the old
  name (checked before renaming) and a shrunk model claiming to be an 8B one would be dishonest.
  `DEFAULT_MODEL` and ModelRack itself are untouched — this is LoadCoach's own construction of the
  fake, not a change to a contract three applications' fakes read.

  The rejection stays reachable on purpose: **`[provider.fake]`** (`size_bytes`, `layers`,
  `kv_heads`, `head_dim`, all optional) lets an operator override the declared model, e.g. back to
  the original numbers, to provoke `insufficient_vram` deliberately and inspect the full `estimate`
  block. All four fields must be set together — the KV term dominates `size_bytes` at any
  interesting context length (`2 × layers × kv_heads × head_dim × 2 bytes` for the assumed f16
  precision, times the served context), so `size_bytes` alone cannot reliably provoke the
  rejection this block exists to reach; `build_provider` refuses a partial set with a
  `ConfigurationError` naming `provider.fake` and the missing fields. The `fake` provider kind is
  **not** exempted from `insufficient_vram` — this block makes the fake keep modelling the
  constraint, on purpose, rather than stop modelling it.
- **A synchronous generation now records the model it made resident**, so consecutive requests on
  one GPU stop refusing each other. Residency was an *input* to `/generate` and never an output:
  the endpoint routed using the exception that lets an already-loaded model be chosen without
  re-checking VRAM, loaded a model into the provider, and wrote no residency episode. The next
  request therefore read an empty residency map, could not apply the exception, and was refused
  `NO_ELIGIBLE_MODEL` / `insufficient_vram` by the memory the previous request was still holding —
  including by the very model that had just answered and was still loaded.

  On a single-GPU machine this failed the **second** stage of any caller whose stages use
  different task profiles, whatever they asked for, and the queue did not rescue it because
  routing refuses before a job reaches `waiting_resources`. Reported by IdeaPress, whose stages map
  to `general.reasoning`, `content.review` and `content.article_draft` seconds apart. The
  synchronous path now calls `ResidencyService.ensure_loaded` exactly as the queue worker does, so
  it both records the load and evicts under `max_resident_models` to make room. Recording failures
  are swallowed: residency is an optimisation and an eviction policy, never a precondition for a
  generation the provider can serve.

## [1.0.0] — 2026-08-30

LoadCoach 1.0: the M5 exit — an explainable, durable, secure routing service. Phases 7 through 9
of the [development plan](docs/apps/loadcoach/development-plan.md): production feedback and
reliability, the complete operator UI, and hardening for a LAN.

**Prepared, not published.** No tag, no upload. Both extracted packages — `weightsdb 0.2.0` and
`mirrorwall 0.2.0` — are on PyPI as of 2026-08-30, so `pip install .` resolves from the index in a
clean virtualenv, `requirements/ci.lock` is hash-pinned against it, and every CI job installs from
that lock non-editably (see [`requirements/README.md`](requirements/README.md)). The tag is the
remaining step.

**M5 closeout (2026-08-30).** The Fable 5 verification of the prepared release returned
*not ready* with fourteen findings; everything in this block acts on them (handoff entries
M5C-1 … M5C-14). CI is green on the real runner from this state.

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

- Migration `0006` was a syntax error on PostgreSQL — `window` is a reserved word there and the
  `reliability_stats` CHECK constraint's raw text never quoted it — so the migration had never
  applied on the second supported dialect (SQLite accepted it, hiding the defect from every
  local run). Found by the first CI run whose pytest step actually executed; the identifier is
  now quoted, which both dialects accept, in the migration and the model alike.
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
- Streamed tokens were quantized to the SSE loop's 10 ms empty-poll: over a real socket the
  added latency per token was ~10 ms p95 against spec §15's 5 ms budget. Both token-carrying
  streams (`POST /generate/stream`, the job event stream) now poll at 2 ms — measured over TCP
  at 1.29 ms added-latency p95 — and a new performance test measures the gap over a real
  loopback socket rather than in-process.
- Spec §17 and api.md §1 promised a `gpu_telemetry` health component no phase ever built.
  Removed, with the decision recorded: a machine without a GPU is not unhealthy (ADR-0016), and
  the readings live on `GET /system/status` and the System page. A test now holds both
  documents and `/api/v1/health` to one component list. The doctor's telemetry check is
  recoded `degraded:telemetry` so it no longer claims the phantom component.
- A job whose text the retention sweep removed showed nothing where its output had been. The
  job document now carries `retention.content_scrubbed_at` and `/jobs/{id}` says "content
  removed by retention" with the instant, what remains, and that a `loadcoach db backup` taken
  before the sweep keeps the text. Spec §14 now states the retention default M5 decided
  (text kept 24 h after completion, then swept) instead of the pre-M5 "hashes by default".
- `discover_models` was the one mutating service without the inner scope check: it now takes a
  `principal` and requires `admin` in the service layer as well as at `POST /models/discover`,
  closing P9's named failure mode for the last writer.
- `/jobs/{id}` rendered its explanation links as escaped text (`&lt;a href=…&gt;`) — the only
  navigation from a job to its full explanation when the narrative is absent, and the unbroken
  blob that made the page scroll at 375 px. The links are now a plain paragraph of real anchors
  under the definition list.
- `/system` (and `/jobs/{id}`) scrolled horizontally at 375 px: a long unbroken value in a
  definition list — the 64-character machine fingerprint, a canonical ID — widened the page.
  Both pages carry a stopgap `overflow-wrap` rule until MirrorWall 0.2.1 puts it in
  `components.css`.
- Candidate capability tables on `/routing/{decision_id}` now carry a row-count, as UI
  standards §5 requires of every table; before, a decision with two or more candidates rendered
  them bare. The accessibility checklist's server fixture also pins the deterministic telemetry
  snapshot itself — it is built before the autouse pin applies, and its pages used to depend on
  the developer's real GPU (passing on a busy card, failing on a machine with none).

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
