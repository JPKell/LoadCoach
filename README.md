# LoadCoach

Turns FreeWeight's measurements (or declared capabilities) into routed, queued, validated inference execution with a fully explainable decision for every job.

**Status:** `0.9.0b0` — beta. Phases 1–6 of the
[development plan](docs/apps/loadcoach/development-plan.md) are built: the registry and task
profiles, evidence-weighted routing with a full explanation for every decision, synchronous and
streaming generation with validation and corrective retries, a durable priority queue with leases,
ageing, cancellation, recovery and a circuit breaker, and FreeWeight evidence import that visibly
changes routing. Phases 7–9 — production feedback and regression detection, the dashboard, and
auth/rate-limit hardening for 1.0 — are not.

What that means in practice:

* `loadcoach serve` starts with zero configuration and needs no provider, no GPU and no FreeWeight.
  Each of those absences is a documented degraded state, never a failure to serve.
* Every routing decision is persisted in full and retrievable, with per-capability scores,
  confidences, evidence ages and the reason for every rejection and every absence.
* Importing a FreeWeight bundle changes routing, and the explanation says exactly how.
* **Not yet ready to expose on a LAN.** Bearer tokens and the cumulative scope rule exist and gate
  evidence import; per-token rate limits and queue-depth caps do not. That is Phase 9's work.

**Not installable from an index yet:** `weightsdb` and `mirrorwall`, two of this package's runtime
dependencies, are extracted from this repository's own work and are not published. See
[`requirements/README.md`](requirements/README.md).

Part of the **Local AI Suite**.

## Install

```bash
pip install loadcoach     # not yet — see the status note above
loadcoach serve
```

Starts on `127.0.0.1:8766` with zero configuration. See [docs/apps/loadcoach/spec.md](docs/apps/loadcoach/spec.md) §12 for the full configuration surface and `LOADCOACH_*` environment variables.

## Quickstart

```bash
pip install loadcoach
loadcoach serve            # starts the web UI + API on 127.0.0.1:8766
loadcoach health --json     # same health data the API reports, from the CLI
loadcoach --help
```

## Documentation

Project documentation lives under [`docs/`](docs/README.md). Start with [`docs/README.md`](docs/README.md).

| Read this | For |
|---|---|
| [docs/apps/loadcoach/spec.md](docs/apps/loadcoach/spec.md) | Purpose, scope, non-goals, public contracts, configuration, acceptance criteria |
| [docs/apps/loadcoach/development-plan.md](docs/apps/loadcoach/development-plan.md) | The phased build plan: goals, work, tests, acceptance criteria per phase |
| [docs/apps/loadcoach/routing.md](docs/apps/loadcoach/routing.md) | How a model is chosen, and what the explanation contains |
| [docs/apps/loadcoach/queue-and-scheduling.md](docs/apps/loadcoach/queue-and-scheduling.md) | Priority, ageing, leases, admission and recovery |
| [docs/apps/loadcoach/api.md](docs/apps/loadcoach/api.md) | Every endpoint, its shape and its scope |

## Development

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pre-commit install
pytest -m "not live and not performance"
```

See [`CONTRIBUTING.md`](CONTRIBUTING.md) for the full workflow and [`SECURITY.md`](SECURITY.md) for
how to report a vulnerability.

## License

Apache-2.0 — see [`LICENSE`](LICENSE).
