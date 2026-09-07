# LoadCoach documentation

Operator documents, written for this release:

* [quickstart.md](quickstart.md) — install, the first request, reading a decision, feedback.
* [configuration.md](configuration.md) — every key, generated from the settings model.
* [routing.md](routing.md) — how a model is chosen and how to change the answer.
* [operations.md](operations.md) — health, queue controls, retention, backups, what to watch.
* [troubleshooting.md](troubleshooting.md) — every error code and what to do.
* [upgrading.md](upgrading.md) — migrations, behaviour changes, the downgrade path.
* [security.md](security.md) — the LAN-exposure path end to end.
* [openapi.json](openapi.json) — the API, as a committed snapshot.

The specification set — `apps/loadcoach/{spec,routing,queue-and-scheduling,api,data-model,
development-plan,risks}.md` and the standards it cites — is **mirrored** from the suite's
`docs/` repository, which is the single source of truth. Edit there; this copy is downstream.
