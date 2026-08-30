# Troubleshooting

Start with `loadcoach doctor`: it walks every failure mode below, says which apply here, and what
to do. Every API error is the standard envelope — `code`, `message`, `details`, `request_id` — and
the request ID is in the log line that goes with it.

## Startup

| Symptom | Code | What it means and what to do |
|---|---|---|
| Refuses to start naming a field | `CONFIGURATION_ERROR` | `loadcoach config validate` shows the problem; the field is named. |
| Refuses to start on a non-loopback host | `INSECURE_BINDING` | Set `server.allowed_hosts`, create a token (`loadcoach token create`), and set `allow_lan_exposure` only for `0.0.0.0`. See security.md. |
| "database is behind head" | `MIGRATION_REQUIRED` | `loadcoach db upgrade`. Automatic on SQLite unless `storage.auto_migrate = false`. |
| "database was written by a newer version" | `SCHEMA_AHEAD` | Install that version, or restore the pre-migration backup and stay. See upgrading.md. |
| Cannot open the database | `DATABASE_UNAVAILABLE` | The directory, the URL, or the server. `loadcoach db status`. |
| A task profile fails validation | `TASK_PROFILE_INVALID` | The message names the file, the profile and the field. |

## Routing

| Symptom | Code | What it means and what to do |
|---|---|---|
| No model chosen; every candidate listed with a reason | `NO_ELIGIBLE_MODEL` | Read the reasons: `insufficient_vram` (free VRAM by device is in `details`), `context_too_small`, `capability_unsupported`, `excluded_by_policy`, `recently_failing`, `model_unavailable`. `loadcoach route explain --task …` reproduces it. |
| Unknown task | `TASK_PROFILE_NOT_FOUND` | `loadcoach tasks list`. |
| A model you expected is not a candidate | — | `loadcoach models list` shows availability; the explanation's `rejected` list shows the constraint. |
| `low_evidence` on every decision | — | No FreeWeight evidence; routing uses declared flags and priors. Import a bundle. |
| `evidence_profile_mismatch` with a remedy | — | FreeWeight measured under a different runtime profile; the remedy is the exact `freeweight run start` command. |

## Execution

| Symptom | Code | What it means and what to do |
|---|---|---|
| Provider down | `PROVIDER_UNAVAILABLE` (503) | Start it; health shows `provider: unavailable` meanwhile. |
| Provider slow | `PROVIDER_TIMEOUT` (504) | `execution.default_timeout_seconds`; the attempt is retried with backoff, then falls back. |
| Every candidate failed | `ALL_CANDIDATES_FAILED` (502) | `details.attempts` lists each model and error. |
| Structured output invalid after retries | `VALIDATION_FAILED` | The failing paths and attempt count are in `details`; the profile's `validation` policy decides retries. |
| Prompt too long | `CONTEXT_LIMIT_EXCEEDED` (422) | The context budget is in the explanation; a larger-context candidate is tried if one exists. |
| Job waits and waits | `INSUFFICIENT_RESOURCES` after `max_wait_seconds` | VRAM: the deferral names the device and the numbers. Unload an idle model or lower the profile's context. |
| Job refused at submission | `QUEUE_FULL` (429) | `queue.max_depth`, or the per-source cap named in `details.source`. |
| A background job never runs | `MAX_WAIT_EXCEEDED` | Ageing bounds the wait; past `max_wait_seconds` it fails explicitly. The scheduling simulation proves the bound. |

## Access

| Symptom | Code | What it means and what to do |
|---|---|---|
| 401 | `UNAUTHORIZED` | No usable bearer token; on the UI, paste one into the 401 page. |
| 403 | `FORBIDDEN` | The token's scope does not contain the required one; `details.required_scope`. |
| 403 on a form | `CSRF_FAILED` | The double-submit token is missing or wrong, or the JSON write came from another origin. |
| 421 | `MISDIRECTED_REQUEST` | The `Host` header is not in the allowlist; DNS rebinding defence. |
| 429 with `Retry-After` | `RATE_LIMITED` | Past the per-credential limit or the failed-authentication brake. Wait the named seconds. |
| 413 | `PAYLOAD_TOO_LARGE` | Over `server.max_body_bytes`. |

## Evidence

| Symptom | Code | What it means and what to do |
|---|---|---|
| Import refused before fetching | `EVIDENCE_SOURCE_REFUSED` (403) | The URL failed the allowlist (scheme, host, link-local, redirect, size). Add the host to `evidence.allowed_source_hosts`. |
| Bundle rejected | `EVIDENCE_IMPORT_FAILED` (422) | The response names each rejected record and why. |
| Wrong schema major | `SCHEMA_VERSION_UNSUPPORTED` (422) | Both versions are named; nothing was changed. Upgrade `setspec`, or widen `evidence.accept_schema_majors` only if this build can read it. |
| `unmatched` records | — | Evidence for a model discovery has not seen; bound automatically when it appears. |

## Storage

`STORAGE_FULL` (507) — free disk beside the database. `STORAGE_BUSY` (503) — SQLite lock contention
beyond the busy timeout; another process is holding a write. Both are reported, never retried
silently.
