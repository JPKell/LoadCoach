# MirrorWall — Development Plan

**Sequence position:** extracted during **LoadCoach Phase 4**, from FreeWeight's web layer
([ADR-0011](../../adr/0011-shared-package-boundaries.md)). FreeWeight adopts it in FreeWeight
Phase 12.
**Target:** `mirrorwall 0.2.0` by the end of Phase 3.

**Precondition for starting:** FreeWeight has a complete, polished UI in production use, and LoadCoach
needs the same shell, components and streaming. The components have therefore been designed against
one real application and are about to be validated against a second — which is the only honest moment
to extract a UI toolkit.

---

## Phase 1 — Tokens, layout shell and core components

**Goal:** LoadCoach renders its first page using MirrorWall's shell and components, and it looks like
FreeWeight without containing any FreeWeight concept.

**Prerequisites:** FreeWeight P11 complete (a shipped UI); `baseaicore`, `setspec`.

**Work**
* Repository skeleton with package data configured for templates, CSS, JS and vendored assets.
* Move and generalize from FreeWeight: `tokens.css`/`tokens.json` (accent parameterized), reset,
  layout CSS, base template (header slot, telemetry bar slot, content block, theme bootstrap),
  and the component macros listed in the [spec](spec.md) §4.
* Vendored assets: charting library, Inter font with its licence, inline SVG icon set, with a
  `THIRD_PARTY_NOTICES.md` and SHA-256 records.
* `create_template_environment` with shared filters: `bytes_human`, `duration_human`, `timestamp`,
  `measurement` (renders `—` plus reason for `UNSUPPORTED`), `truncate_middle`, `json_pretty`.
* Term scan test: no application vocabulary anywhere in the package.

**Files/subsystems**
```text
src/mirrorwall/{__init__,__about__,templating,filters}.py
src/mirrorwall/templates/mirrorwall/{base.html,components.html,telemetry_bar.html}
src/mirrorwall/static/{css/{tokens,reset,layout,components,tables,charts}.css,
                       js/{theme,table,drawer,dialog,toast}.js,
                       vendor/**,fonts/**,icons/**}
tests/unit/{test_templating,test_filters}.py
tests/snapshot/test_components.py
tests/test_no_application_vocabulary.py
```

**Tests**
* Snapshot render of every component in light and dark; required ARIA attributes present.
* `measurement` filter renders `—` with a tooltip for `UNSUPPORTED` and never `0`.
* `StrictUndefined`: a missing variable raises rather than rendering blank.
* Escaping: content containing `<script>`, `{{ }}` and quotes renders inert.
* Contrast: every token pair meets 4.5:1 (text) / 3:1 (UI) in both themes.
* Term scan finds no application vocabulary.
* Package data present in the built wheel and loadable via `importlib.resources`.

**Acceptance criteria**
1. LoadCoach renders a real page (its Models list) with MirrorWall's shell and table.
2. FreeWeight's existing look is reproduced by tokens alone, with only the accent differing.
3. Coverage ≥ 95 %; strict typing clean.

**Known risks:** extracting FreeWeight's *pages* along with its components. Mitigated by the term-scan
test and by building against LoadCoach first.
**Likely failure modes:** components with FreeWeight-shaped required parameters; package data missing
from the wheel.
**Gold standards:** no application vocabulary; accessible components; offline assets.
**Deferred:** SSE, envelopes, telemetry-bar JS, FreeWeight adoption.

---

## Phase 2 — Backend helpers: envelopes, request IDs, SSE, static

**Goal:** both applications share one streaming and JSON convention, and a browser that disconnects
loses nothing.

**Prerequisites:** Phase 1.

**Work**
* `responses.py`: `json_response`, `error_response`, `paginated_response`, built on SetSpec models.
* `middleware.py`: `RequestIdMiddleware` — validate or generate, bind to the logging context, echo in
  `X-Request-ID`, add `X-Response-Time-Ms`.
* `sse.py`: `EventSource` protocol and `sse_response` — subscribe-before-replay, dedupe by sequence,
  bounded queue with drop, heartbeat, terminal event on source failure, clean close on disconnect.
  **Every call into the (synchronous, database-backed) `EventSource` is dispatched with
  `anyio.to_thread.run_sync`**, here and only here, so no application can put a blocking `SELECT` on
  the event loop ([ADR-0003 §6–8](../../adr/0003-sync-vs-async-strategy.md)). Replay reads in bounded
  batches; the steady-state stream is served from the in-memory fan-out without touching the database.
* Frame shape: the SetSpec event envelope with the event as `payload`, except `event: token`, which is
  bare — the one documented exception ([ADR-0025 §3](../../adr/0025-envelope-boundaries.md)).
* `middleware.py` also carries `HostValidationMiddleware` and `CsrfMiddleware`
  ([ADR-0026](../../adr/0026-local-http-hardening.md)), shared so all three applications behave
  identically and the check runs before routing and before authentication.
* `static.py`: `mount_static`, `asset_url` with content hashing, cache headers, containment checks.
* `health.py`: `ComponentStatus`, `ComponentHealth`, `health_payload`, `worst_status`.

**Files/subsystems**
```text
src/mirrorwall/{responses,middleware,sse,static,health}.py
src/mirrorwall/static/js/{sse.js,telemetry.js}
tests/unit/{test_responses,test_middleware,test_static,test_health}.py
tests/integration/test_sse.py
tests/js/test_sse_client.py            # DOM harness
```

**Tests**
* SSE: ordered delivery; replay from `Last-Event-ID` with no gap and no duplicate across the
  replay/live handoff; heartbeat at the configured cadence; slow consumer dropped once its queue
  fills; source exception yields a terminal event; 200 concurrent subscribers within the memory
  budget.
* Client module: reconnects after a drop, sends `Last-Event-ID`, and applies events idempotently.
* Envelopes validate against SetSpec models; `request_id` present on success and error paths.
* Request ID: a hostile header value (too long, control characters) is rejected and replaced.
* `Host`: an allowed value passes, a disallowed one gets 421 before routing, and the check runs before
  any authentication dependency.
* CSRF: a forged form post is rejected; a valid one succeeds; a cross-origin JSON post is rejected.
* SSE threading: an `EventSource` whose `replay` blocks for 200 ms does not delay the event loop,
  measured with a lag probe — the test that keeps the async edge honest.
* Frame shape: every non-`token` frame parses through `setspec.load_envelope`; `token` does not.
* Static: hashed URL, cache headers, traversal refused, symlink escape refused.

**Acceptance criteria**
1. A browser refresh mid-stream resumes with no missing or duplicated event.
2. Both applications produce byte-compatible error envelopes.
3. Memory per idle subscriber within budget under a 200-subscriber test.

**Known risks:** the replay/live handoff is the subtlest code in the package. Mitigated by porting the
proven approach (subscribe before replay, dedupe by sequence) and testing it under injected races.
**Likely failure modes:** duplicated events at the handoff; unbounded queues; a heartbeat that keeps a
dead connection open forever.
**Gold standards:** gap-free replay; bounded memory; identical envelopes across applications.
**Deferred:** the interactive table/chart JS; FreeWeight adoption.

---

## Phase 3 — Interactive modules, accessibility hardening, publication

**Goal:** dense data views are usable and accessible in both applications, and the package ships.

**Prerequisites:** Phases 1–2.

**Work**
* JS modules: `table.js` (sort over the full dataset via the API, filter, column visibility,
  persistence), `drawer.js`, `dialog.js` (focus trap and restore), `toast.js`, `theme.js`,
  `charts.js` (chart container, theming, accessible table alternative), `telemetry.js`
  (bar updates without layout shift).
* Accessibility pass: keyboard traversal, focus management, live regions, reduced-motion support,
  skip link.
* Performance tests: render time, table sort, JS payload size.
* Component gallery page for development mode.
* README, component documentation with usage examples; publish `mirrorwall 0.2.0`.
* Write the adoption checklist FreeWeight Phase 12 follows.

**Files/subsystems**
```text
src/mirrorwall/static/js/{table,charts}.js
src/mirrorwall/gallery.py               # dev-mode component gallery
tests/js/{test_table,test_theme,test_telemetry}.py
tests/performance/test_render.py
tests/accessibility/test_keyboard_and_aria.py
docs/{components.md,adoption-checklist.md}
```

**Tests**
* Table: sort/filter/column visibility; preferences persist; sort applies to the whole dataset, not
  the page; 1 000-row sort within budget.
* Dialog/drawer: focus trapped, `Esc` closes, focus restored, `aria-modal` correct.
* Telemetry bar: updates with zero layout shift (asserted on measured widths); `—` for unsupported.
* Charts: re-theme on toggle; accessible table alternative present; units in tooltips.
* Reduced motion: transitions disabled when the media query matches.
* JS payload size within budget; no external URL in any asset.

**Acceptance criteria**
1. Every §20 criterion in the [spec](spec.md) is met.
2. LoadCoach's full UI runs on MirrorWall.
3. Both applications' page suites render against the release candidate in CI, obtained from their
   published distributions as a **test-only** dependency of MirrorWall's `dev` extra — never by
   importing application code, which `lint-imports` continues to forbid
   ([Testing Standards §8](../../standards/testing-standards.md)).
4. `mirrorwall 0.2.0` published; the adoption checklist is written and reviewed.

**Known risks:** JS complexity growing past the "islands" budget. Mitigated by the size budget being a
CI gate and by pushing logic into Python where possible.
**Likely failure modes:** components that only work with LoadCoach's data shapes; accessibility
regressions in later changes (mitigated by the automated a11y suite).
**Gold standards:** upgradeable without page changes; accessible; offline; no application vocabulary;
≥ 95 % coverage.
**Deferred:** chart-spec wrapper, print stylesheet, density modes, additional icons.
