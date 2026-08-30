# Operations

## Where things live

| What | Where |
|---|---|
| Configuration | `$XDG_CONFIG_HOME/loadcoach/config.toml` (`loadcoach config path`) |
| Database, backups | `$XDG_DATA_HOME/loadcoach/loadcoach.sqlite3`, `…/backups/` |
| Logs | stdout, structured JSON off a TTY (`logging.format`) |

PostgreSQL is supported (`storage.database_url = "postgresql+psycopg://…"`); nothing else is.

## Day to day

```bash
loadcoach health                  # ok / degraded / unavailable, per component
loadcoach doctor                  # every documented failure mode, with a remedy each
loadcoach queue status            # depth, ages, starvation, residency, breakers
loadcoach reliability show        # production evidence per model and task profile
loadcoach models list             # what discovery sees, available or not
```

The **Dashboard** (`/`) shows current activity, every degradation with a link to its page, the ten
most recent decisions and jobs, and the model mix over 24 hours. The **System** page shows
telemetry, residency, the thread pool, dispatch latency, starvation and breakers.

## Health, and what degrades it

`GET /api/v1/health` reports `database`, `provider`, `queue`, `evidence`, `reliability`. Only the
database is required: LoadCoach serves with no provider and no evidence, degraded. `queue` degrades
on starvation, depth past 80 % of `max_depth`, or an open breaker; `evidence` when a configured
FreeWeight is unreachable (the last import is retained and marked stale); `reliability` when a
model's recent validated-success rate has regressed against its own history.

## The queue controls

```bash
loadcoach queue pause      # stop claiming; in-flight work finishes; nothing is dropped
loadcoach queue resume     # clears pause and drain
loadcoach queue drain      # finish in-flight work, claim nothing new — for a clean shutdown
```

The flags are durable (the `settings` table), so a pause from another process reaches the running
scheduler within a second and survives a restart. The Queue page has the same three buttons.

**Restart.** `SIGTERM` stops the workers after their current chunk; a kill mid-attempt is
recovered on the next start: leases are reaped, idempotent work is re-queued, non-idempotent work
is failed explicitly with `WORKER_LOST`, and nothing is duplicated (queue §10). The recovery summary
is logged and shown on the Queue page.

## Runtime-changeable settings

`PUT /api/v1/settings`, the Settings page, or the table directly: `queue.paused`,
`queue.draining`, `routing.prefer_resident_bonus`, `routing.min_present_weight`,
`routing.min_confidence`, `routing.remote_cost_factor`, `storage.content_retention_hours`. Applied
within a second. Everything else is `config.toml`/environment and a restart; the security-relevant
keys are refused by name.

## Retention

A finished job keeps its prompt and response text for `storage.content_retention_hours` (24) and
then loses it — hashes, tokens, timings, model, decision and events stay, so the job is still
explicable. A queued job keeps its transcript until it has run. `storage.retain_content = true`
keeps everything (config-only). Routing decisions are kept for ever by default
(`routing.explanation_retention_days = 0`).

## Backups

```bash
loadcoach db backup                        # SQLite: an online copy under …/backups/, rotated
loadcoach db backup --output /mnt/nas/loadcoach-$(date +%F).sqlite3
loadcoach db restore --source <file> --confirm
```

Every migration takes a backup first and restores it if the migration fails
(`storage.backup_retention`, 5). On PostgreSQL the command prints the `pg_dump`/`pg_restore`
invocation rather than running it.

## Evidence

```bash
loadcoach evidence sources        # configured and observed sources, last import, status
loadcoach evidence refresh        # pull from [evidence] freeweight_url now
loadcoach evidence import --file bundle.json
```

With `freeweight_url` set, the scheduler refreshes every `import_interval_hours` (24). An
unreachable FreeWeight keeps the last import and marks it stale; routing continues on it and the
explanation says so.

## What to watch

* `queue.starving` above zero for minutes: the ageing policy is working, the machine is saturated.
* A breaker open on the Queue page: that model is failing; it will be re-probed after the cool-down.
* `reliability: degraded` in health: a model's recent quality dropped against its own baseline;
  the Reliability page names it and the numbers.
* `evidence: degraded`: FreeWeight is down; routing uses the retained evidence.
* `429` in your client's logs: it is past its rate limit or its queue cap; the response says which
  and `Retry-After` says when.
