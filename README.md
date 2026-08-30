# LoadCoach

Turns FreeWeight's measurements (or declared capabilities) into routed, queued, validated inference
execution with a fully explainable decision for every job.

**Status:** `1.0.0` — the M5 release, **prepared and not yet published**. Every phase of the
[development plan](docs/apps/loadcoach/development-plan.md) is built: the registry and task
profiles; evidence-weighted routing with a readable explanation for every decision; synchronous and
streaming generation with validation and corrective retries; a durable priority queue with leases,
ageing, cancellation, recovery and a circuit breaker; FreeWeight evidence import; production
feedback, reliability and regression detection; a complete operator UI (dashboard, jobs, live
queue, models, reliability, system, settings); and the hardening a LAN bind needs — scopes checked
at the route and in the service, per-token rate limits, per-source queue caps, CSRF, Host
validation, body limits, content retention.

**Not installable from an index yet:** `weightsdb 0.2.0`, one of this package's runtime
dependencies, is tagged but its PyPI release was still awaiting approval when 1.0.0 was stamped
(`mirrorwall 0.2.0` is published). Until it lands, `pip install loadcoach` cannot resolve and CI's
install jobs stay red; see [`requirements/README.md`](requirements/README.md). Locally the
repository runs against editable installs of the sibling packages.

Part of the **Local AI Suite**.

## Install

```bash
pip install loadcoach          # once weightsdb 0.2.0 is on PyPI
loadcoach serve                # web UI + API on http://127.0.0.1:8766, zero configuration
loadcoach doctor               # every documented failure mode, ✓ / ! / ✗, with what to do
```

No provider, no GPU, no FreeWeight is required to start; each absence is a documented degraded
state. Ollama on `127.0.0.1:11434` is the default provider.

## What it does

* **Routes explainably.** Every decision is persisted in full — candidates, per-capability scores
  with their source and age, the four adjustment factors with their inputs, every rejection with
  its numbers — and `/jobs/<id>` answers *why this model?* before it shows a table.
* **Learns from production.** Attempt outcomes and caller feedback become bounded per-model
  reliability statistics that deprioritize a failing model, exclude it through a circuit breaker
  with a re-probe, and flag a regression against the model's own history.
* **Queues durably.** Priority classes, ageing with a proven starvation bound, leases, cancellation
  within a chunk, VRAM-aware admission, residency management, and recovery after a kill that loses
  and duplicates nothing.
* **Exposes safely.** Loopback and open by default; on a LAN, tokens with cumulative scopes checked
  twice, Host validation before authentication, CSRF on forms, rate limits with `Retry-After`,
  and a `doctor` that names what is wrong.

## Documentation

| Read this | For |
|---|---|
| [docs/quickstart.md](docs/quickstart.md) | The first request, reading a decision, feedback |
| [docs/configuration.md](docs/configuration.md) | Every key, generated from the settings model |
| [docs/routing.md](docs/routing.md) | How a model is chosen and how to change the answer |
| [docs/operations.md](docs/operations.md) | Health, queue controls, retention, backups, what to watch |
| [docs/troubleshooting.md](docs/troubleshooting.md) | Every error code and what to do |
| [docs/upgrading.md](docs/upgrading.md) | Migrations, behaviour changes, the downgrade path |
| [docs/security.md](docs/security.md) | The LAN-exposure path end to end |
| [docs/openapi.json](docs/openapi.json) | The API, as a committed OpenAPI snapshot |
| [docs/apps/loadcoach/](docs/apps/loadcoach/) | The specification, routing, queue and API documents (mirrored from the suite) |

## Development

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pre-commit install
ruff format --check . && ruff check . && mypy src tests && lint-imports
pytest -m "not live and not performance"
pytest -m performance            # every spec §15 budget, measured
pytest tests/security            # Security Standards §14, item by item
```

Licensed under the Apache License 2.0.
