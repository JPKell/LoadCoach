# Routing guide

How a model is chosen, and how to read — and change — the answer. The normative document is
[the routing specification](apps/loadcoach/routing.md); this is the operator's view.

## The pipeline in one paragraph

Every request names a **task profile** (`loadcoach tasks list`), which carries capability weights,
hard constraints and an execution policy. Routing takes every model discovery knows, resolves each
one's runtime profile and served context, drops the ones that fail a hard constraint (context,
VRAM, a required capability, a policy exclusion, an open circuit breaker), scores the rest on the
profile's capabilities from the best evidence available, multiplies by four bounded factors, and
orders the result. The whole thing is persisted for every decision and shown at
`/routing/<decision_id>` and `/jobs/<job_id>`.

## Where a score comes from

Per capability, in order of precedence: FreeWeight **benchmark** evidence measured under the same
runtime profile (with its confidence and age); a **manual** score from
`manual_capability_scores.toml`; a **declared** provider flag (a neutral 0.5); the **parameter-band
prior** (0.40–0.60). A capability with nothing behind it is *absent* — left out of both numerator
and denominator, never scored zero — and named as such. A decision whose measured weight is below
`routing.min_present_weight` is flagged `low_evidence`.

Production evidence enters through the **reliability factor**, not the capability scores: after
twenty counted attempts on a `(model, task profile)` pair, the factor is
`0.5 + 0.5 × answered × validated × feedback_term`, from the freshest of the 7-day and 30-day
windows, so a model that keeps erroring or answering wrongly loses up to half its score — and a bad
day ages out within thirty days. The explanation carries the window, the rates and one sentence
saying why, under `factors.reliability_detail`.

## The four factors

`final_score = task_fit × reliability × availability × residency × cost`. Reliability is above;
residency is a small bonus (`prefer_resident_bonus`) for an already-loaded model; cost is
`remote_cost_factor` for a remote provider; availability is 1.0 in this release.

## Reading an explanation

On `/jobs/<id>`, "Why this model" answers three questions: which model and by how much (the
headline and the arithmetic); what decided it (a factor, or the capability that moved the score
most, with both models' numbers); and what is missing (each absent capability, why, and — for a
runtime-profile mismatch — the exact `freeweight run start` command that would produce matching
evidence). Every rejection is listed with its reason in words and the numbers behind it.

## Changing the answer

* **Constraints in the request** may only tighten the profile's: `min_context_tokens`,
  `requires_capabilities`, `min_capability_scores`, `exclude_models`, `allow_remote_providers`.
* **Overrides**: `model` pins a candidate; `disallow_fallback` fails rather than falling back;
  `require_evidence` scores only on benchmark evidence; `runtime_profile` changes the resolved
  profile for this request.
* **Task profiles** are yours to edit (`config/task_profiles.toml`); `loadcoach tasks validate`
  checks them, and every job records the profile version it used.
* **Feedback** (`loadcoach job feedback`, `POST /jobs/{id}/feedback`) moves the reliability factor
  once five verdicts exist in the window.
* **Runtime settings** (`PUT /api/v1/settings`): `prefer_resident_bonus`, `min_present_weight`,
  `min_confidence`, `remote_cost_factor`, applied within a second.

## Determinism

Given the same registry, evidence, telemetry snapshot, reliability statistics and request, routing
produces the same decision; `POST /route` makes one without executing, and the stored telemetry
snapshot on every decision is what makes it reproducible after the fact.
