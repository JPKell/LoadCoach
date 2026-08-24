# LoadCoach

Turns FreeWeight's measurements (or declared capabilities) into routed, queued, validated inference execution with a fully explainable decision for every job.

**Status:** specified, not yet implemented. This repository currently holds the project scaffold
(directory structure, tooling configuration, and the project documentation) —
see [development plan](docs/apps/loadcoach/development-plan.md) for what each phase adds.

Part of the **Local AI Suite**.

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

Project documentation lives under [`docs/`](docs/README.md). Start with [`docs/README.md`](docs/README.md).

| Read this | For |
|---|---|
| [docs/apps/loadcoach/spec.md](docs/apps/loadcoach/spec.md) | Purpose, scope, non-goals, public contracts, configuration, acceptance criteria |
| [docs/apps/loadcoach/development-plan.md](docs/apps/loadcoach/development-plan.md) | The phased build plan: goals, work, tests, acceptance criteria per phase |

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
