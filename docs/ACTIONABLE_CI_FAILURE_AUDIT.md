# Apex Trader CI Failure Audit

Date: 2026-10-26
Base commit: b92833bf4ff90eb64e1049862346a6934707dcc2
Workflow run: https://github.com/Eugine336/apex-trader/actions/runs/40270654561

## Status

This is a triage record, not a claim that the suite is fixed. The main-branch full-suite run is red. Python 3.12 completed with 177 failures, 3,738 passes, and 12 skips; Python 3.11 was cancelled during the test step. Lint and test collection passed. Do not deploy or enable broker execution on the basis of the focused tests alone.

## High-priority failure clusters observed in the run

1. **Cognitive admission / test-contract mismatch**: several campaign-opening tests now receive `CONTINUE_OBSERVING` because advisor quorum and independent market-domain coverage are unconfirmed. Do not weaken the production fail-closed gates to satisfy legacy fixtures. Repair fixtures to provide authentic, explicit coverage metadata, and preserve negative tests proving missing quorum/domain metadata blocks exposure.
2. **Python dependency/API compatibility**: `tests/test_dashboard_auth.py` passes `client=(host, port)` to Starlette/FastAPI `TestClient`, but the installed TestClient rejects that keyword. Align the test harness with the pinned dependency version or deliberately update the dependency lock and validate compatibility.
3. **Stateful-test isolation and persistent artifacts**: failures mention SQLite schema/foreign-key state, malformed database reuse, learner recency fields, optimizer persistence, and tracked `.db` files. Each test must use isolated temporary paths, explicit schemas, and deterministic clocks; runtime data files must not be tracked.
4. **Contract drift between implementation and legacy tests**: examples include renamed/moved event-driven bootstrap methods, changed intent types, instrument configuration defaults, changed module/timeframe mapping, and order-comment format. Classify each against the current architecture before changing code or tests; do not reintroduce deprecated execution paths just to satisfy stale assertions.
5. **Market-data freshness test fixtures**: Deriv order tests use timestamps that are now rejected as stale. Use explicit current/fixed clocks and assert stale ticks are rejected; do not disable stale-data protection.
6. **OOS/learning validation failures**: optimizer/OOS-gate expectations and persistence tests fail. These are material because learning changes must remain sample-aware, cost-aware, walk-forward validated, and separated from untouched holdout data. Do not relax acceptance thresholds to make tests green.

## Execution-route audit observations

- `execution/action_executor.py` is the intended serialized, risk-gated broker execution boundary.
- `platforms/platform_manager.py` contains connector-level order dispatch and pending-order methods. Such methods are dangerous if reachable outside the approved admission path.
- `event_driven_bootstrap.py` still wires a legacy NewsPlanner callback, but `tests/test_event_driven_identity_contract.py` asserts that the update path and staging callback cannot submit pending orders. Keep that route disabled until it is redesigned around first-class Brain opportunities and whole-book admission.
- `risk/spread_bootstrap.py` is a startup spread-seeding utility, but historical audit notes flag direct connector access. Verify its current call graph before considering the route closed.
- Static tests are not enough to prove every route safe. Add integration tests with a recording fake broker that assert no broker-mutating method is called when any upstream gate rejects or when market data, council coverage, portfolio state, or risk context is unavailable.

## Required completion gates

- [ ] Resolve each failure cluster without weakening safety invariants.
- [ ] Obtain a full green test run on every supported Python version.
- [ ] Add call-graph coverage for all broker mutation entry points, including direct connector calls, pending orders, scale-ins, reversals, and startup helpers.
- [ ] Prove whole-book admission, exposure limits, stale-feed rejection, idempotency, and safe degradation with recording fake-broker tests.
- [ ] Verify position-thesis reassessment and all-instrument shared Brain routing.
- [ ] Validate learning with deterministic walk-forward splits and a truly untouched holdout; include transaction costs and minimum sample thresholds.
- [ ] Only after those gates pass, run controlled demo-account validation with explicit opt-in and telemetry. No live orders.

## Protected configuration

The `.env` file is out of scope and must not be read, copied, recreated, renamed, or modified during this work. This audit does not change broker credentials or send orders.


## CI status refresh — 2026-10-26

The prior statement that CI could not be rerun because of exhausted Actions quota is superseded: new workflows are now running on branch head `cc702c0c353690f4d9d2d3e6b7b61c819688aa78`:
- Full CI: https://github.com/Eugine336/apex-trader/actions/runs/40280936900
- Gold-focused tests: https://github.com/Eugine336/apex-trader/actions/runs/40280936955

These are pending at this update. Do not infer success from a running workflow. The previous completed full-suite result remains red, and the new provenance-gate changes must pass the focused suite before the PR is considered mergeable.


## Verification refresh — 2026-10-26, PR #758

Supersedes the older pending-workflow note above. Rechecked GitHub state:
- PR #758 remains open and draft; latest inspected head is `3193635346bf920278882be1f56bb4dda6149b07` after the management provenance fix and its regression test.
- Workflow #1911 (`40275835782`) completed with **failure** on older head `a90a8f540f4f32d06098d24c36fa097218244c1f`. Do not treat it as a run against the current head.
- Workflow #1902 (`40270654561`) completed with **failure** on `main` at `b92833bf4ff90eb64e1049862346a6934707dcc2`; Python 3.12 reported 177 failures, 3,738 passes, 12 skips, and Python 3.11 was cancelled.
- The workflows previously described as pending on `cc702c0c353690f4d9d2d3e6b7b61c819688aa78` are not evidence of current-head success. Their final status was not established in this check.
- No local checkout was available in the current execution environment, so focused tests and Ruff were not run locally. The current-head changes remain **unverified by test execution**.

Keep the PR draft/unmerged until focused local tests or CI validate the current head. No broker orders were sent, no live trading was enabled, and `.env` was not accessed or changed.
