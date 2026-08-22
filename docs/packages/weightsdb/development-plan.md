# WeightsDB — Development Plan

**Sequence position:** extracted during **LoadCoach Phase 1**, from FreeWeight's in-application
storage layer. Not built speculatively ([ADR-0011](../../adr/0011-shared-package-boundaries.md)).
**Target:** `weightsdb 0.2.0` by the end of Phase 3; FreeWeight adopts it in FreeWeight Phase 12.

**Precondition for starting:** FreeWeight is shipping with a working `freeweight.infrastructure.db`
that has already been through real migrations, and LoadCoach needs the same mechanics. Two consumers
exist; extraction is justified.

---

## Phase 1 — Extract engine, session and type plumbing

**Goal:** LoadCoach builds its database on WeightsDB, and FreeWeight's equivalent code is proven
identical (not yet swapped).

**Prerequisites:** FreeWeight P11 complete (FreeWeight 1.0-rc); `baseaicore>=0.4,<0.5`.

**Work**
* Repository skeleton.
* Move (not copy) from FreeWeight, generalizing as it goes: `create_engine_for`, SQLite pragma event
  listener, PostgreSQL settings, `session_factory`, `session_scope`, `transaction`.
* Portable types: `UtcDateTime`, `PortableJSON`, `ulid_primary_key`, `measurement_columns`
  (returning `<name>` and `<name>_unavailable_reason` — the names are fixed, not suggested).
* `upsert(session, model, values, *, index_elements)` — the suite's one sanctioned
  `INSERT … ON CONFLICT DO UPDATE`, because select-then-insert is a race under both dialects and a
  hand-written clause is how a dialect-specific variant gets in
  ([ADR-0006](../../adr/0006-sqlite-and-postgresql-roles.md)).
* Errors: `DatabaseUnavailable`, `StorageBusy`, `SchemaAhead`, `MigrationRequired`,
  `MigrationFailed`.
* Credential redaction helper used by every error and log path.
* `weightsdb.testing`: `temporary_sqlite`, `temporary_postgres`.

**Files/subsystems**
```text
src/weightsdb/{__init__,__about__,engine,session,types,errors,redaction,testing}.py
tests/unit/{test_engine,test_session,test_types,test_redaction}.py
tests/integration/test_two_schemas.py
```

**Tests**
* Pragmas applied on a fresh connection **and** after a forced pool reconnect.
* `session_scope` rollback on exception, including `KeyboardInterrupt`; connection always returned.
* `UtcDateTime` round-trips on both dialects; naive datetimes rejected with the column named.
* `PortableJSON` round-trips nested structures and unicode on both dialects.
* Two independent `MetaData` objects with different schemas coexist against one WeightsDB.
* `upsert`: concurrent writers of one natural key produce one row and no error, on both dialects.
* `measurement_columns` produces the documented pair; a metadata scan finds no unpaired measurement
  column.
* Redaction: no password in any error message, log record or repr.

**Acceptance criteria**
1. LoadCoach's first migration runs on WeightsDB against SQLite and PostgreSQL.
2. `weightsdb`'s own `MetaData` is empty (asserted).
3. Coverage ≥ 95 %; both dialects green in CI.

**Known risks:** extracting FreeWeight-shaped assumptions along with the code. Mitigated by writing
LoadCoach's schema against it *first* and treating any FreeWeight-specific concept found as a defect
to leave behind.
**Likely failure modes:** pragmas silently lost on reconnect; a type decorator that behaves
differently across dialects.
**Gold standards:** no application concept inside the package; identical behaviour on both dialects.
**Deferred:** migrations, backup, health, FreeWeight adoption.

---

## Phase 2 — Migration runner, backup and restore

**Goal:** both applications share one migration and backup mechanism, with failure recovery proven.

**Prerequisites:** Phase 1.

**Work**
* `migrations.py`: `MigrationRunner` (`current`, `heads`, `is_at_head`, `upgrade`, `downgrade`,
  `stamp`, `check_parity`), with automatic pre-migration backup and automatic restore on failure.
* `backup.py`: SQLite backup API path, `pg_dump` path, `integrity_check`, restore with verification
  before swap, `.pre-restore` retention.
* `testing.migration_harness` for application migration tests.

**Files/subsystems**
```text
src/weightsdb/{migrations,backup}.py
tests/integration/{test_migrations,test_backup_restore}.py
```

**Tests**
* Fresh database → head; stepwise revisions; idempotent re-run; downgrade where defined.
* Deliberately failing migration on **SQLite**: backup restored, original byte-identical, both
  outcomes reported.
* Deliberately failing migration on **PostgreSQL**: transactional DDL rolls back where possible;
  otherwise `MigrationOutcome` reports the exact revision reached, refuses to continue, and names the
  backup and the restore command. The suite does not claim an automatic restore it cannot perform
  under a live database with a least-privileged role
  ([WeightsDB §11](spec.md)).
* `check_parity` detects a drifted model (a test adds a column to metadata only).
* Backup of a database with an open write transaction succeeds and is consistent.
* Restore of a truncated/corrupt backup is refused before the live database is touched.
* Disk-full simulated during backup: partial file removed, error raised.
* Both dialects.

**Acceptance criteria**
1. A failed migration never leaves a half-migrated database.
2. `check_parity` fails CI in both applications when a model changes without a migration.
3. Backup/restore round-trips on both dialects.

**Known risks:** `pg_dump` availability and version skew. Mitigated by detecting it, reporting
clearly when it is absent, and documenting the SQL-level alternative.
**Likely failure modes:** backing up by copying a live SQLite file (wrong — the backup API is used);
restore that swaps before verifying.
**Gold standards:** safe migrations; verified restores; parity enforced in CI.
**Deferred:** health reporting; FreeWeight adoption.

---

## Phase 3 — Health, publication and FreeWeight adoption handshake

**Goal:** the package is complete, published, and ready for FreeWeight to adopt.

**Prerequisites:** Phases 1–2.

**Work**
* `health.py`: `database_health()` — dialect and version, revision vs head, journal mode, file size,
  free space, last backup age, integrity status, degraded conditions.
* Network-filesystem detection for SQLite paths, with a startup-worthy warning result.
* Performance tests for the §15 budgets.
* README, quickstart, migration guide for adopters; publish `weightsdb 0.2.0`.
* Write the adoption checklist that FreeWeight Phase 12 follows (what to delete, what to replace,
  which tests must still pass unchanged).

**Files/subsystems**
```text
src/weightsdb/health.py
tests/unit/test_health.py
tests/performance/test_db_overhead.py
docs/{quickstart.md,adoption-checklist.md}
```

**Tests**
* Health output for: at head, behind head, ahead of head, integrity warning, low disk, stale backup.
* Network-filesystem detection with a mocked mount table.
* Performance budgets: engine creation, session acquisition, pragma cost, backup throughput.

**Acceptance criteria**
1. Every §20 criterion in the [spec](spec.md) is met.
2. LoadCoach's health endpoint reports its `database` component entirely from `database_health()`.
3. `weightsdb 0.2.0` published; the adoption checklist is written and reviewed.
4. FreeWeight Phase 12 can proceed with no further changes required here.

**Known risks:** the adoption pass revealing FreeWeight needs something the extraction dropped.
Mitigated by the adoption checklist being written *before* FreeWeight starts, and by both consumers'
test suites being run against the package in CI.
**Likely failure modes:** health output that differs between applications; a package that only fits
the application it was extracted from.
**Gold standards:** two applications, one package, zero shared schema; safe migrations; verified
backups; both dialects; ≥ 95 % coverage.
**Deferred:** read replicas, scheduled backups, query-plan helpers, retention utilities, DuckDB
analytics.
