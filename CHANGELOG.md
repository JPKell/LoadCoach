# Changelog

All notable changes to `loadcoach` are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning follows
[Semantic Versioning](https://semver.org/), pre-1.0 per
packaging and release standards §3.

## [Unreleased]

### Changed
- Widened the `sweatmeter` pin to `>=0.4,<0.5`. SweatMeter's first published release is `0.4.0`
  (`0.3.0` completed its development plan but never reached the index), and it adds the in-process
  NVML GPU backend, selected automatically wherever the optional `pynvml` extra is installed.

### Added
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
