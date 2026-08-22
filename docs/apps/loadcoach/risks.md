# LoadCoach — Risk and Failure Analysis

Suite-wide risks: [Risk Register](../../architecture/risk-register.md).

---

## 1. Technical risks

| # | Risk | L | I | Mitigation | Early signal |
|---|---|---|---|---|---|
| T1 | **Queue correctness** — double execution, lost jobs, stuck states | Medium | High | Atomic lease claiming; idempotency declared per job; kill-point recovery tests; the `cancelling` watchdog; scheduling simulator | Duplicate outputs; jobs stuck in a non-terminal state |
| T2 | **Starvation** of low-priority work | Medium | Medium | Ageing with a capped overflow; `max_wait_seconds`; starvation counter in health; simulation-proven bound | Starvation counter non-zero; oldest-queued age climbing |
| T3 | **Routing that cannot be explained** | Low | High | Explanation persisted for 100 % of decisions; pure, deterministic scoring; a golden-decision test | A user asking "why?" and the UI not answering |
| T4 | **Bad routing from thin evidence** | High | Medium | Absent evidence excluded rather than zeroed; `low_evidence` flag; low fixed confidence for priors; production evidence accrues quickly | `low_evidence` on most decisions |
| T5 | **VRAM estimation wrong** → OOM or chronic under-use | High | High | Prefer FreeWeight's measured KV bytes/token; conservative headroom; unknown estimate treated as "does not fit"; record estimate vs actual on every job and report the error | Estimate/actual divergence in job records; provider OOM errors |
| T6 | **Model-load thrash** — alternating models on every job | Medium | Medium | Residency bonus; affinity batching with a streak cap; `unload_idle_seconds` | Load events per hour far exceeding job count/2 |
| T7 | **Circuit breaker excludes a good model permanently** | Medium | Medium | Bounded open duration; scheduled re-probe with a low-priority job; state and reason visible | A model absent from candidacy for a long period |
| T8 | **Context estimation error** truncates or overflows | Medium | High | Provider tokenizer when available; documented character-ratio estimate otherwise with the ratio recorded; safety margin; never truncate input silently | `CONTEXT_LIMIT_EXCEEDED` after a successful admission check |
| T9 | **Overhead grows** and dominates short requests | Medium | Medium | Overhead measured and reported separately on every job; budget asserted in performance tests | `loadcoach_overhead_ms` trending up |
| T10 | **Feedback loop instability** — reliability oscillating | Low | Medium | Minimum sample counts; bounded factor range; windowed statistics | Model selection flapping between two candidates |

## 2. Integration risks

| # | Risk | L | I | Mitigation |
|---|---|---|---|---|
| I1 | Evidence contract drift with FreeWeight | Low | High | SetSpec goldens tested in both repositories; explicit major rejection; nightly compatibility matrix |
| I2 | IdeaPress depending on undocumented behaviour | Medium | Medium | OpenAPI snapshot as the contract; documented client guidance; additive-only v1 |
| I3 | Provider capability differences producing surprises | Medium | Medium | Capability flags as hard constraints; honest `unsupported`; conformance suite in ModelRack |
| I4 | Callers not sending feedback, leaving reliability blind | High | Low | Validation outcomes count as implicit feedback; documented benefit; UI shows coverage |
| I5 | Two applications competing for the same GPU (FreeWeight benchmarking while LoadCoach serves) | High | High | Documented as unsupported concurrency; LoadCoach's admission control sees the VRAM shortfall and defers; FreeWeight's runs are single-workload; guidance to pause the queue during benchmarking (`loadcoach queue pause`) |

## 3. Security risks

| # | Risk | L | I | Mitigation |
|---|---|---|---|---|
| S1 | **LAN exposure** of an inference gateway | Medium | High | Loopback default; bind + token + acknowledgement; startup refusal; scopes; rate limits |
| S2 | Malicious evidence bundle | Low | Medium | Schema validation; size limits; provenance checks; import cannot alter task profiles or execute anything |
| S3 | Caller-supplied tool definitions abused | Low | High | LoadCoach never executes a tool call; it returns the request to the caller |
| S4 | Prompt content persisted unintentionally | Medium | Medium | Hashes by default; content storage opt-in; redaction in logs |
| S5 | One caller starving others | Medium | Medium | Per-token rate limits; queue-depth caps; class bands |
| S6 | Remote provider egress not noticed | Low | High | Off by default; egress badge in UI and in every routing explanation that selects a remote model |

## 4. Portability risks

| # | Risk | L | I | Mitigation |
|---|---|---|---|---|
| P1 | VRAM-based admission unavailable on unsupported platforms | Medium | Medium | Degrades to RAM-only or unconstrained with a recorded reason |
| P2 | Residency control unavailable on some providers | High | Low | Capability-gated; degrades to load-on-demand |

## 5. Performance risks

| # | Risk | L | I | Mitigation |
|---|---|---|---|---|
| F1 | Routing slow with many models and much evidence | Low | Medium | Evidence cache with explicit invalidation; budget asserted cold and warm |
| F2 | Queue polling wasting CPU | Low | Low | Adaptive backoff plus enqueue wake-up; idle CPU budget asserted |
| F3 | Explanation storage growth | Medium | Low | Configurable retention (default forever, deliberately); candidate rows are small |

## 6. Model and provider risks

| # | Risk | L | I | Mitigation |
|---|---|---|---|---|
| M1 | Provider changes sampling or template defaults, changing behaviour silently | Medium | High | Provider version recorded on every job; environment drift reduces evidence confidence; regression detection |
| M2 | A model that benchmarks well performs badly in production | Medium | Medium | Production evidence and the reliability factor exist precisely for this; both sources shown separately |
| M3 | Structured output unreliable on a chosen model | Medium | Medium | Validation with corrective retry, then fallback; `structured_output` is a weighted capability and a hard constraint where required |

## 7. Maintenance risks

| # | Risk | L | I | Mitigation |
|---|---|---|---|---|
| G1 | Task profile sprawl | Medium | Medium | Profiles are configuration with validation; the shipped set is deliberately small |
| G2 | Scoring parameters becoming folklore | Medium | Medium | Strategy and policy versions recorded on every decision; parameters are configuration; changes are reviewable |
| G3 | Explanation format churn breaking consumers | Low | Medium | The explanation is part of the v1 API; additive only |

---

## 8. Deliberate trade-offs

* **Single machine, single process** — simplicity and durability over horizontal scale.
* **Database-backed queue** — a little polling latency in exchange for zero infrastructure.
* **Explanations for 100 % of decisions** — storage spent on the product's core promise.
* **Conservative admission** — occasional under-use of the GPU rather than OOM thrash.
* **Small residency bonus** — capability outranks convenience.
* **No tool execution** — LoadCoach stays an inference gateway, not an agent runtime.

## 9. Explicit non-goals (restated as risk control)

Benchmarking, content workflows, agent loops, multi-machine scheduling, model management. Each would
either duplicate another component's responsibility or turn LoadCoach into a platform it is not.

## 10. Premature optimizations to avoid

* A broker or worker processes before one thread pool is proven insufficient.
* Learned routing before there is production data to learn from.
* Speculative or parallel execution before GPU contention is understood.
* Caching routing decisions before the routing budget is missed.
* A plugin architecture for routing strategies before a second strategy exists.

## 11. Architectural traps

* Computing capability scores here instead of consuming FreeWeight's — the fastest route to two
  sources of truth.
* Reading FreeWeight's database "just for evidence".
* Letting task profiles carry prompts (they carry intent; prompts are records).
* Storing a routing score on the model row instead of on the decision.
* Executing tool calls "just this once" for a caller's convenience.
