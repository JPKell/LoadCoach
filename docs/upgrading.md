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
| 1.0.0 | `0006` | `feedback` and `reliability_stats` (P7). Purely additive; no existing column changes. |
| 0.9.0b0 | `0001`–`0005` | The beta's schema. |

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
