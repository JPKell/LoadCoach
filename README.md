# LoadCoach

Turns FreeWeight's measurements (or declared capabilities) into routed, queued, validated inference execution with a fully explainable decision for every job.

**Status:** specified, not yet implemented. This repository currently holds the project scaffold
(directory structure, tooling configuration, and a local copy of the relevant suite documentation) —
see [development plan](docs/apps/loadcoach/development-plan.md) for what each phase adds.

Part of the **Local AI Suite** — see [docs/architecture/executive-summary.md](docs/architecture/executive-summary.md)
for how LoadCoach fits with the suite's other applications and packages.

## Install

```bash
pip install loadcoach
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

This repository carries its own copy of the relevant suite documentation under [`docs/`](docs/README.md),
so it can be read and implemented independently of the other eight suite repositories. Start with
[`docs/README.md`](docs/README.md).

| Read this | For |
|---|---|
| [docs/apps/loadcoach/spec.md](docs/apps/loadcoach/spec.md) | Purpose, scope, non-goals, public contracts, configuration, acceptance criteria |
| [docs/apps/loadcoach/development-plan.md](docs/apps/loadcoach/development-plan.md) | The phased build plan: goals, work, tests, acceptance criteria per phase |
| [docs/standards/](docs/standards/) | Coding, testing, security, API, database and packaging standards every phase follows |
| [docs/adr/](docs/adr/README.md) | The architectural decisions this design rests on |

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
