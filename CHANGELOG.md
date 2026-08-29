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
- Phase 2: registry, task profiles, `GET /models`, `GET /task-profiles`, plain HTML pages.
  - `config/task_profiles.toml`: all fifteen shipped profiles (routing.md §2), each validated at
    startup (weights sum to 1.0, capabilities in the SetSpec vocabulary, referenced JSON schemas
    under `config/schemas/` resolve, no contradictory `response_format`/`require_schema` vs.
    `json_schema_ref`) — a malformed profile refuses startup naming the file, profile ID and
    problem.
  - `services/models.py`: discovery through ModelRack; unavailable models are flagged with a
    reason, never deleted; declared `ModelCapabilityFlag`s honestly translated into SetSpec
    capability rows (only the flags with an unambiguous counterpart — never padded).
  - `config/manual_capability_scores.toml`: optional operator-entered capability scores, marked
    `source="manual"`; shipped empty.
  - `cli/`: `models list|show|refresh`, `tasks list|show|validate`.
  - `web/rendering.py` and `web/templates/`: the first UI pages, deliberately plain
    (pre-MirrorWall) — `/models` and `/task-profiles`.

### Fixed
- `services/database.py`: `ensure_ready()`/`get_status()` misused `redact_url()` on an arbitrary
  exception message instead of a URL, which raised instead of reporting the real error whenever
  the message wasn't itself a parseable URL.
- `cli/commands/tasks.py`: `tasks list`/`tasks show` read the `task_profiles` table directly and
  saw it empty on a fresh install where `loadcoach serve` (which imports the shipped profiles) had
  never run; both commands now import before reading.
