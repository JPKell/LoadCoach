# Quickstart

LoadCoach routes, queues and executes inference against local models, and explains every decision.
It needs a model provider (Ollama by default) and nothing else: no configuration, no GPU, no
FreeWeight. Each of those absences is a documented degraded state, never a failure to serve.

## Install and start

```bash
pip install loadcoach          # once v1.0.0 is tagged; every dependency is on PyPI already
loadcoach serve                # web UI and API on http://127.0.0.1:8766
```

`loadcoach serve` migrates its SQLite database under `$XDG_DATA_HOME/loadcoach/` on first start,
imports the fifteen shipped task profiles, and discovers whatever models the provider reports.
Open **http://127.0.0.1:8766/** for the dashboard.

Check the installation before anything else:

```bash
loadcoach doctor               # every documented failure mode, ✓ / ! / ✗, with what to do
loadcoach health --json        # the same components GET /api/v1/health reports
```

## The first request

```bash
curl -s http://127.0.0.1:8766/api/v1/generate \
  -H 'Content-Type: application/json' \
  -d '{"task": "general.chat", "prompt": "Name three uses for a paperclip."}'
```

The response carries the output, the model that produced it, `routing.decision_id`, usage and
timings. Everything the router considered is retrievable:

```bash
curl -s http://127.0.0.1:8766/api/v1/jobs/<job_id>/explanation | jq .
```

or, without leaving the terminal:

```bash
loadcoach route explain --task general.chat          # the decision, with no execution
loadcoach job submit --task general.chat --prompt 'hello'
loadcoach job wait <job_id>
```

## Reading a decision

Open **`/jobs/<job_id>`** in the browser. The page answers *why this model?* before any table:
the headline names the model, the final score and the margin over the runner-up; the sentence
beneath says what decided it — a factor, or the capability that moved the score most; the tables
list what carried the score (with each score's source and age), what could not be scored and the
command that would fix it, and why every other candidate was set aside. The stored JSON stays at
the bottom as the raw source.

With no FreeWeight evidence the explanation says `evidence: none` and flags `low_evidence`:
routing on declared capabilities and priors is reasonable, and clearly labelled.

## Queued work and feedback

```bash
loadcoach job submit --task content.article_draft --prompt-file brief.txt --class background
loadcoach queue status
loadcoach job feedback <job_id> --accepted --quality 0.8      # feeds the reliability factor
loadcoach reliability show
```

Feedback is idempotent per `(job, source)`; a second verdict from the same source replaces the
first. `GET /api/v1/reliability` and the **Reliability** page show what production evidence has
accumulated per model and task profile, and whether any model has regressed against its own
history.

## Benchmark evidence

If FreeWeight is running, import its evidence and watch routing change:

```bash
loadcoach evidence import --url http://127.0.0.1:8765
loadcoach route explain --task code.review
```

Set `[evidence] freeweight_url` to keep it refreshed. A FreeWeight elsewhere on the network must
be named in `evidence.allowed_source_hosts` first (ADR-0026 §3).

## Next

* [configuration.md](configuration.md) — every key, generated from the settings model.
* [security.md](security.md) — before exposing LoadCoach beyond this machine.
* [operations.md](operations.md) — backups, retention, the queue controls, what to watch.
* [troubleshooting.md](troubleshooting.md) — every error code and what it means.
