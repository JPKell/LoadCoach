# Upgrading and downgrading

## Upgrading

```bash
pip install --upgrade loadcoach
loadcoach db status              # current revision, head, integrity
loadcoach serve                  # migrates on start (SQLite) — or:
loadcoach db upgrade
```

Every migration takes a backup first (`…/backups/`, rotated by `storage.backup_retention`). On
SQLite a failed migration restores that backup automatically and reports both outcomes; on
PostgreSQL migrations are not automatic (`auto_migrate` defaults to false there) and a failed one
is reported with the backup to restore.

`loadcoach db status` after the upgrade shows the revision this build expects; health shows the
`database` component `ok`.

### Migration notes

| Version | Migration | What it adds |
|---|---|---|
| 1.1.5 | none | No schema change — a `mirrorwall` dependency-floor fix (so the declared lowest range resolves) only; nothing behaves differently for an operator. |
| 1.1.4 | none | No schema change — test and documentation additions only; nothing behaves differently for an operator. |
| 1.1.3 | none | No schema change — only the `usage` wire spelling moves (below). |
| 1.1.2 | none | No schema change — precedence fix only (below). |
| 1.1.1 | none | No schema change — a render fix and a new task-profile field, `execution.think` (config, not schema). |
| 1.1.0 | `0008`–`0012` | `provider_name` and `is_remote` on `models`; the `adapters` table; the execution subject on every routing, job, attempt, residency and reliability row. Additive throughout — the `residency` and `reliability_stats` uniqueness keys gain `adapter_key`, whose `''` default is a fact about existing rows (they are bare-base subjects) and not a placeholder. No statistic and no explanation changes value. |
| 1.0.0 | `0006` | `feedback` and `reliability_stats` (P7). Purely additive; no existing column changes. |
| 0.9.0b0 | `0001`–`0005` | The beta's schema. |

### Behaviour changes at 1.1.3

* **BREAKING, inside `/api/v1`: `usage.input_tokens` and `usage.output_tokens` render the string
  `"unsupported"` instead of `null` for an unreported count.** All five `usage` token classes now
  share one spelling for "not reported" (ADR-0016 rule 4, ADR-0112). A client that modeled either
  field as `int | None` and read `null` as a measured absence must be updated to expect the string
  instead, on the job document, `POST /generate` and `POST /generate/stream`'s terminal frame.
  Storage is unchanged — the columns stay nullable integers — and no migration is needed.

### Behaviour changes at 1.1.2

* **The environment now beats a stored `settings` row**, as configuration standards §7 always
  said (ADR-0100). Before this release a row in `settings` beat a `LOADCOACH_*` variable pinning
  the same key — the opposite of the documented precedence. If you rely on an environment
  variable to pin a runtime-changeable key over whatever an operator (or a script) set through
  `PUT /settings`, that variable now actually wins; the stored row is kept, not deleted, so
  unsetting the variable makes it effective again.
* **`GET /settings` and `loadcoach config show` name what shadows a stored value.** Each
  `definitions` entry gains `stored`, `source` (`"database"` or `"configuration"`) and
  `shadowed_by` (`"env LOADCOACH_…"`, or `null`); `config show` marks a database-sourced value
  `(database)`.
* No schema change and no new runtime-changeable key.

### Behaviour changes at 1.1.1

* **`GET /models` (and `loadcoach models list --json`) now render `provider_name` and
  `is_remote`** on every entry. Before this release both columns were recorded but never
  rendered, so a consumer reading the remote-provider fact from the registry (PromptCadence does)
  read `false` regardless of what was actually registered, and a remote registration stayed
  invisible until its first turn.
* **A task profile's `execution` block may carry `think`** (`true`/`false`/unset), overridable per
  request by `sampling.think`, and a request that sets it requires `thinking_control` of every
  candidate. The five shipped harness profiles ship with it **unset** — measured, not assumed —
  so no shipped profile's behaviour changes on upgrade.
* `requirements/ci.lock` moves to `modelrack 0.7.1` (a GGUF descriptor fix for `head_dim`);
  `pyproject.toml`'s range is unchanged.

### Behaviour changes at 1.1.0

* **Nothing changes for a deployment with no `[adapters] directory`.** The feature is off by
  default; with no adapters configured, routing produces the pool, the scores and the explanations
  1.0 produced.
* **`[providers.<name>]` blocks are new and the singular `[provider]` block still works** — it is
  exactly one registration named after its kind. Writing **both** forms is refused at startup,
  naming each, rather than resolved by a precedence rule.
* **`output.tool_calls` is superseded** by `output.tool_calls_assembled` and will be removed at
  `2.0`. The old field still carries one entry per streamed fragment; the new one carries whole
  calls. A caller grouping fragments itself should stop.
* **The migration run turns SQLite foreign keys off for its own connection and back on after.**
  Adding a constraint on SQLite rebuilds the table, and a parent rebuild with enforcement on
  deletes its children — which for `routing_decisions` would be every stored explanation.

### Behaviour changes at 1.0.0

* Prompt and response text is scrubbed from finished jobs after `storage.content_retention_hours`
  (24). Set `retain_content = true` before upgrading if you rely on old text staying readable.
* Per-token rate limits and the per-source queue cap are on by default (generous); a client that
  submits hundreds of jobs from one source should raise `queue.max_active_per_source`.
* Scoped endpoints on a tokened bind now require the scope in the service layer too; a `read`
  token that could previously reach a write endpoint through an internal path cannot.
* A cross-origin JSON write is refused; scripts and IdeaPress send no `Origin` and are unaffected.

## Downgrading

Downgrading the application without downgrading the database is **refused**, not attempted: a
database ahead of the code raises `SCHEMA_AHEAD` at startup and names both revisions and the backup
directory (packaging standards §6.1).

The supported path:

1. Stop the application.
2. Restore the automatic pre-migration backup:
   `loadcoach db restore --source <data>/backups/<file> --confirm` — or, where the migration's
   `downgrade()` is lossless, `loadcoach db downgrade <revision>`.
3. Install the older version and start it.

`0006`'s downgrade drops `feedback` and `reliability_stats`; that is a loss of production
evidence, so the backup path is the one to prefer.
