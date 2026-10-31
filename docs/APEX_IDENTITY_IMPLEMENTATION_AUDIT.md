# Apex identity — system-wide implementation audit

> **Audit baseline:** repository main at commit 7a74844f708486e33c5125a9dace3ed04db604c2 (2026-10-24; re-verified 2026-10-25).  
> **Purpose:** replace optimistic documentation-only completion claims with source-backed runtime gates. Refresh this audit after every migration PR.  
> **Authority:** APEX_IDENTITY.md and APEX_Constitution.md.

## Executive status

The identity is canonical in source and documentation in this branch. The shared cognition snapshot now includes universal completed-bar OHLC observations and contextual M1 patterns when data is available. The latest completed focused workflow on the previous head passed on Python 3.11 and 3.12 (264 tests per version, compilation and scoped Ruff lint; 3 warnings). New route-level static guards and configuration changes have landed since that run and are awaiting a fresh focused run. A repository-wide Ruff run is green; the full suite now completes collection but still has numerous failures in legacy-contract, environment/dependency, and test-fixture areas. That does **not** mean the full identity is implemented. Existing code already has strong pieces (one CognitiveBrain, evidence consolidation, event-driven nudges, MTF chart/tick snapshots, competing native hypotheses, campaign memory, portfolio context and risk/execution gates), but these pieces do not yet prove a complete market-aware trader.

## Current work item — native verdict survives advisor failure (2026-10-29)

The current implementation change makes advisor configuration/health remain
infrastructure metadata while the already-completed native reasoning supplies
the market verdict. When no advisor is configured, a provider is unavailable,
or a healthy advisor returns no opinion this cycle, native qualification still
runs. If native qualification fails, the Brain returns `CONTINUE_OBSERVING`
with separate `reasoning_mode`, `external_advisor_status`, and native diagnostics
rather than replacing that verdict with `REASONER_UNAVAILABLE`. A qualified
native campaign remains eligible through the existing qualification and risk path.
Regression assertions cover native-before-availability call order, a qualified
native campaign when a healthy advisor returns no opinion, native-only operation
without a configured advisor, provider outage, and constitutional conformance. **These
changes have not yet been executed in a test environment; status is implemented
in the branch, integration/test status unverified until the focused suites and
identity audit run.** This change does not close the separate native hypothesis
breadth, runtime route, all-instrument evidence, or demo-execution gaps below.

## Current work item — System 2 market context and epistemic integrity (2026-10-29)

Implementation added on branch `redesign/system2-market-intelligence-context`:
- `cognition/market_context.py` summarizes only supplied evidence for instrument contract/economic specifications, session calendars, execution constraints, synchronized cross-market relationships, historical validation metadata, unfamiliar measurements and missing information.
- `BrainReasoner.analyze_evidence()` includes this context in `EvidenceAnalysis.market_context`, which is serialized for the native reasoning record and advisor context.
- Correlation/lead-lag evidence is explicitly labeled observed association; causation remains unestablished.
- Historical validation metadata requires out-of-sample evaluation, walk-forward evaluation, untouched holdout, leakage checks, selection-bias checks and overfitting checks before being labeled `validated_controls_reported`. Even then, the module does not claim predictive ability is demonstrated; independent audit remains required.
- Unfamiliar measurement keys are preserved as unclassified observations rather than being silently discarded or treated as validated patterns.
- Missing instrument/session/execution/historical context is explicitly listed. This code does not infer missing contract specifications, sessions or predictive skill.

**Validation status: unverified.** Focused tests were added in `tests/test_market_context_intelligence.py` but have not been executed in this environment. This is an initial System 2 implementation slice, not proof that broker adapters supply verified instrument specifications, session calendars, synchronized cross-asset data, or leakage-resistant historical outcomes. The new module is not yet wired to a proven source of authoritative contract metadata. No live orders were sent; `.env`, runtime database state and broker settings were not changed. Do not mark System 2 complete until source wiring, focused tests, identity audit, and route-level impact review are completed.

## Findings from actual source inspection

**Freshness correction (PR #753; focused validation passed):** `Evidence.is_fresh()` rejects evidence whose source timestamp is later than the comparison time, including evidence with an otherwise non-expiring horizon. This closes a negative-age fail-open; it does not establish that each broker adapter normalizes timestamps correctly. The focused cognition workflow passed on Python 3.11 and 3.12 with compilation and scoped Ruff checks, and the regression test passed in the full Python 3.11 run. The full-suite run still reports 180 failed / 3,717 passed / 12 skipped on Python 3.11; the Python 3.12 matrix job was cancelled after that failure. The preceding main baseline reported 180 failed / 3,716 passed / 12 skipped. This is a narrowly scoped fail-closed correction, not a green full suite or runtime completion.

| Area | Existing implementation | Gap that remains | Required closure evidence |
|---|---|---|---|
| Canonical identity | Constitution plus the new machine-readable cognition.identity contract | Earlier README described fixed 10-factor scoring and zone→M1 entry, inconsistent with single-path cognition | Runtime contract exposed and logged; docs refer to the same exact statement |
| Market observation | event_driven_bootstrap._cognition_price_snapshot reads D1/H4/H1/M15/M5/M1 candles, latest tick, recent ticks, depth when available, and an open position. cognition.market_lenses.analyze_market_frames now emits completed-bar structure, range/sweep reclaim, momentum, volatility, broker volume when supplied, and MTF-alignment evidence for the shared Brain; original candle timestamps are preserved where the source supplies them and unknown timestamps reduce confidence | Verify every broker/data adapter supplies correctly normalized source times, source freshness, forming/completed distinction, cache age, rate limits and degraded state for every instrument | Tests for stale/unknown timestamp behavior and source-age horizons; all registered instruments; reconnect/stale feed; runtime/demo telemetry |
| Structural analysis | brain/decision_core.py: structure, FVG, order blocks, liquidity, volume, inducement, Wyckoff and trend/MR/regime concepts | Coverage/wiring differs by timeframe and module; a computed module may still be stored rather than influential | Runtime trace of every source into MarketState and attributable use in Brain decisions |
| Trading-method breadth | cognition/pattern_rules.py, gold_desk/strategies.py, legacy trigger/entry_patterns.py, and advisor prompt | Native BrainReasoner.generate_hypotheses currently has a short set of explicit hypothesis branches. The legacy M1 detector is not confirmed as a live entry route; a method catalog is not the same as executable strategy competence | Shared all-symbol observation path, live route audit, structured specialist findings, scenario/backtest matrix per family |
| Native inference | cognition/brain_reasoning.py: evidence coverage, regime indicators, 16 deterministic patterns, hypotheses, counter-hypotheses, criticism | No external advisor outage still leaves a constrained/limited hypothesis generator; it cannot credibly be called exhaustive trading knowledge | Broad native hypothesis generation evaluated on adversarial market scenarios without any provider |
| Advisor integration | Native reasoning runs before provider availability handling in cognition/brain.py; provider status and native verdict are serialized separately; native fallback can originate a campaign when qualification passes | This branch separates provider outage from the native observe verdict and updates outage regression tests, but tests have not run. Native hypothesis breadth remains constrained; deployed runtime routing and end-to-end qualification remain unverified | Run native reasoning/provider-resilience/constitutional suites and identity audit; fault-inject timeout/503/malformed outputs; verify a qualified native campaign reaches shared admission/risk/execution in demo mode |
| Entry decision authority | cognition/loop.py → Brain → _maybe_originate → wired intent sink; docs say legacy pipeline is suppressed under COGNITION_SINGLE_PATH | Need prove effective runtime config, all direct/pending order paths and current environment match the documented single-path state | Static audit, call-site trace, runtime route assertions, pending-order reconciliation and demo tests |
| M1 price action | cognition.price_action_patterns adds engulfing, pin-bar/rejection wick, inside-bar breakout, range reclaim and broker-volume-at-boundary observations to the shared snapshot for supported instruments. They remain neutral, provisional observations—not order authority | Must test broker bar conventions, source-time freshness and multi-timeframe context; the local range-boundary proxy is not equivalent to institutionally defined zones | Unit tests, adapter provenance and freshness tests, multi-timeframe confluence, ablation/backtest |
| Execution / risk | Brain-originated entries use the shared aggregator/RiskGate/executor and fail-closed net-cost check. Management-only scale-in and reverse-open intents are suppressed pending first-class active opportunities and whole-book review; the legacy atomic reversal manager is disabled in runtime wiring. A legacy news-order staging route is being blocked independently. | ORG_REDESIGN_PLAN.md reports duplicated/incompletely wired risk controls; the full set of direct/pending order call sites, scale/reverse/re-entry/recovery paths and complete live risk-engine path still need fresh call-graph verification. The optional ChatGPT→Binance relay is a separate external-signal entry route; its LIVE mode is now blocked until it can submit a CognitiveBrain-authored opportunity through shared admission. | Focused route guards, runtime bypass tests, complete route audit, green full CI, broker-truth reconciliation and a demo cycle with observable reject reasons |
| Portfolio | Whole-cycle opportunity comparison, campaign registry and exposure context exist | Need prove new positions, reversals, scales and reallocation all use same whole-book truth and cost-aware EV | Whole-book scenario tests plus broker-truth reconciliation |
| Learning | Outcome memory, influence/calibration and pattern tracking exist | Samples, censoring, costs and strategy attribution must be validated; learning cannot promote itself into authority | Out-of-sample replay, calibration metrics and governed promotion/rollback |
| Docs and operations | Constitution, rollout runbook, audit registers and README exist | Prior docs contain conflicting statuses and outdated fixed-pipeline claims | Every contradictory claim corrected or clearly marked historical/superseded; runtime map kept current |
| Secrets / runtime configuration | Per the user's explicit instruction, `.env` was restored from `main` on this branch and verified byte-for-byte identical (same Git blob SHA); it remains tracked. `.env.example` is a separate sanitized template, `.gitignore` contains ignore rules, and a security test checks the template only | The tracked restored file may contain active credentials and can be exposed to repository readers. The user instructed that `.env` must not be edited again; no further `.env` edits are in scope. Any credential that was exposed should be rotated in its provider console independently of file edits | Respect the no-edit instruction, keep the sanitized template free of credential values, and review the broader repo's secret-management policy separately without mutating `.env` |
| Production readiness | Focused cognition workflow on PR #753 head `1e8b8814c09290900c028fc9c408a87ce73b4692` passed on Python 3.11 and 3.12, with compilation and scoped Ruff. Full CI Ruff passed and collection succeeded; this head reports 180 failed / 3,717 passed / 12 skipped on Python 3.11, while Python 3.12 was cancelled after the failure. The preceding main baseline reported 180 failed / 3,716 passed / 12 skipped. | Failures span legacy/retired API assumptions, dependency/environment expectations and subsystem defects; no full-suite green result or end-to-end broker demo has been proved | Triage/fix failures or isolate stale tests with evidence; rerun full CI on current main; finish live-route/risk-path audit; demonstrate broker demo and rollback proof |

## Core architecture target

```text
Broker facts / quote / ticks / completed + forming OHLC / session / event data
              ↓
Specialist observers and concept lenses (non-authoritative)
              ↓
Evidence registry with provenance, freshness, uncertainty and data gaps
              ↓
Single CognitiveBrain
  ├─ build current market state
  ├─ choose relevant analytical lenses dynamically
  ├─ generate several conditional market hypotheses
  ├─ challenge them against contradictory evidence
  ├─ discover and track opportunity candidates across horizons
  ├─ evaluate net expectancy, activation, invalidation and opportunity cost
  └─ decide: act / manage / observe / request better evidence
              ↓
Shared portfolio + compliance/risk + broker execution controls
              ↓
Outcome attribution → memory/calibration → validated adaptation
```

## Migration phases and definition of done

### Phase 0 — identity and truth-in-documentation
- Machine-readable identity contract with tests.
- README, constitution preamble and runbook point to the exact statement.
- Each status uses **implemented / wired / tested / shadow-only / legacy / blocked / unverified**.

### Phase 1 — unified observation plane
- Every instrument exposes the same core market facts and source freshness.
- M1 patterns are evidence, not entry authority, and can contribute for every instrument where M1 data is supported.
- Forming candle observations are explicitly provisional.
- Stale or unavailable evidence creates a degraded-input state rather than a silent empty market.

### Phase 2 — strategy knowledge and dynamic method selection
- Catalog analytical families, concepts, prerequisites, invalidation concepts, known failure modes and evidence requirements.
- Relevance selection is conditional on current market state and what data is available; catalog entries never encode a universal entry recipe.
- Support combined and opposing lenses, with source-level reasons shown to the Brain.
- Mark tools needing unavailable data (e.g., centralized order flow/DOM) as unavailable rather than simulated.

### Phase 3 — native hypothesis breadth
- Replace the small fixed hypothesis branch set with a structured hypothesis workbench that can form continuation, pullback, reversal, range rotation, failed breakout, volatility expansion, mean reversion and novel/unknown-pattern hypotheses from evidence.
- Always include counter-hypotheses and state what evidence would distinguish them.
- Native-only path must use the same explicit activation, expectancy, invalidation, data-quality and portfolio requirements as advisor-assisted path.
- Do not equate any single pattern or strategy name with a ready-to-enter opportunity.

### Phase 4 — all-horizon opportunity set and continuous reassessment
- Track several conditional opportunities simultaneously across micro / short / medium / longer horizons.
- Use event-driven updates and bounded computation; the default material-change wake floor is one second per symbol. "Continuously" means process material market changes as they arrive, not call an LLM every tick; periodic sweeps and feed/provider limits remain explicit.
- Reassess during open campaigns from thesis, current book, exit evidence, execution conditions and alternatives.
- Test missed-opportunity / duplicate-entry / overtrading failure modes.

### Phase 5 — execution/risk consolidation
- Trace every exposure-increasing path, scale-in, reversal, pending order and recovery path.
- One shared execution authority, one authoritative risk path, no silent fail-open market decision.
- Execution feasibility and risk remain hard controls that can reject a Brain decision.

### Phase 6 — learning/governance
- Attribute outcomes to evidence sources and method combinations only when provenance is sound.
- Sample floors, multiple-testing safeguards, leakage prevention, walk-forward and untouched holdout.
- Promotion and rollback governed by explicit thresholds; no unvalidated self-modification.

### Phase 7 — docs, full regression and demo gate
- Remove/update superseded claims in README, architecture maps, module READMEs, runbooks and all root Markdown files.
- Run full pytest, lint, compile, static route audits, replay/backtests, fault injection and broker demo.
- Startup banner exposes identity version, reasoning mode, market-feed freshness, authoritative gate/origination mode and live/shadow status.
- Do not call the redesign complete or promote unattended live exposure until every required gate has evidence.

## Constraints and honesty rules

**Pending-order route guard (PR #754; focused validation passed):** the scheduled NewsPlanner has a direct callback to `connector.place_pending_order()`, which bypasses the Brain's first-class opportunity and whole-book admission. Its scheduled update entrypoint and direct staging helper are now inert, so neither a configuration flag nor a symbol allowlist can revive the legacy route. News context remains observational; staged orders remain blocked until the common Brain path admits them. The focused workflow passed on Python 3.11 and 3.12 and lint passed. The full suite remains red with 180 unchanged failures; the latest tested branch had 3,718 passing tests and 12 skips. Other direct broker routes remain under audit.

- No source, test, backtest or log can prove guaranteed profitability or capture of every profitable movement.
- `.env` was restored exactly from `main` at the user's instruction and must not be edited again. It remains tracked; any active credential previously exposed should be rotated separately without changing that file.
- Do not enable a data feed, provider, strategy family or execution path simply because the name exists in the repository.
- The M1 pattern module added alongside this audit is a neutral observer; it does not make the old M1 entry pipeline authoritative.
- COGNITION_ORIGINATION_MODE=live only permits a qualified Brain campaign to reach the configured sink. It is not proof that the external provider is healthy, the Brain can qualify a trade on the current market, every symbol is data-complete, or the broker path has passed a live demo.
- This audit lists known gaps and acceptance gates; it is not a statement that all rows are already closed.

## 2026-10-26 redesign workbench update

**Branch:** `redesign/native-intelligence-workbench`  
**Review:** [PR #756](https://github.com/Eugine336/apex-trader/pull/756)  
**Status at this update:** partial implementation; not a production-readiness sign-off.

### Landed on the workbench branch

- Expanded `cognition.strategy_knowledge.STRATEGY_LENSES` from 17 to 38 analytical/operational lenses, spanning structure, momentum, regime/volatility, macro/news context, relative value, auction/profile, execution, portfolio survival, and learning validation.
- Changed concept matching to token/phrase boundaries and excluded source-module paths from being treated as market evidence.
- Required a concept match to occur in a domain consumed by that lens before labelling it relevant. A relevance result is attention allocation only, not direction, a qualified setup, or order authority.
- Kept declared data requirements separate from known missing context; the registry does not claim that required data is present.
- Hardened the MT5 stale-tick warning throttle so partially constructed connectors still reject stale ticks rather than raising an unrelated attribute error.
- Added regression coverage for registry breadth, provenance/sub-string false positives, missing evidence, repeated-source inflation, and lens limitations.
- Corrected the regime-learner test fixture to isolate its actual persistence root; setting only `SAVE_PATH` was insufficient because the learner resolves its directory through `runtime_paths.data_dir()`.

### Validation snapshot

- Latest focused workflow for the strategy-knowledge / market-data workbench passed.
- Repository lint passed.
- Full CI is still red. On the previous head `ec2fe85d192e9e4e8a42eff0f74471fd05b5b2dd`, Python 3.11 reported 178 failures, 3,735 passes, and 12 skips; the Python 3.12 matrix job was cancelled after the Python 3.11 failure. This is not a green full-suite result, and the baseline contains extensive unrelated legacy failures.
- The regime-learner persistence fixture correction was made after that run and requires a fresh CI run before its effect can be confirmed.

### Explicitly not complete

This PR does **not** close the full identity backlog. Still required: fresh all-route audit of every exposure-increasing API; complete proof that every entry passes through native Brain, first-class opportunity, whole-book admission and common risk/execution; broader adversarial tests of native hypotheses and advisor-outage operation; per-instrument feed/freshness and broker-truth verification; cost-aware walk-forward and untouched holdout evaluation; and controlled demo-account telemetry. No strategy family is proven profitable by its presence in this registry. Do not enable live exposure on the strength of this workbench change.

### Operational boundary

No `.env` file was accessed or changed as part of this work. No broker orders were sent and no live broker configuration was changed. Demo-account testing remains a separate validation stage after code validation.

## 2026-10-26 native hypothesis follow-up

The follow-up branch adds conditional native hypotheses from independently observed completed-timeframe context:
- A trend-context hypothesis can be formed when multiple completed timeframes agree, even if the named-pattern detector is quiet. Decelerating momentum is explicitly surfaced as a pullback/transition alternative.
- A neutral range hypothesis can be formed from repeated timeframe range states without requiring a single `range_rotation` pattern tag.
- The fallback is now described as unresolved evidence rather than asserting that all unrecognized conditions are "consolidation/noise".

These hypotheses remain interpretations, not entry signals. They do not create a first-class opportunity, bypass whole-book admission, or imply a profitable edge. Tests exercise both trend/pullback and range-state cases without named patterns.

**Still unresolved:** direct broker API call-graph proof for all instruments, full runtime management-thesis wiring, broader adversarial scenario tests, and cost-aware walk-forward/holdout evidence. The full repository CI baseline remains red; focused workflow results are reported separately. Demo-account testing has not been performed.



## 2026-10-26 exposure-route audit guard follow-up

A standard-library AST regression test was added in `tests/test_broker_mutation_route_audit.py`. It checks that runtime orchestration and other production modules do not call exposure-increasing broker APIs directly, outside the shared executor/gateway boundary. Broker adapters and simulated backtest execution remain explicit implementation exceptions. This is a static guardrail; it is not equivalent to a runtime call trace or broker demo.

### Current full-suite failure classification

The latest completed `main` CI run (#1902) reports **177 failed, 3,738 passed, 12 skipped** on Python 3.12; the Python 3.11 test job was cancelled. Lint passes and test collection succeeds. Failures include:
- stale test contracts that expect a campaign to open when advisor quorum metadata is absent or unconfirmed; the Brain now correctly records unconfirmed coverage and continues observing;
- test/API compatibility mismatches (for example, dashboard tests pass a `client=` argument not accepted by the installed Starlette `TestClient`);
- legacy tests asserting removed bootstrap methods or prior pipeline behavior;
- genuine subsystem/fixture failures that still require individual triage.

These categories must not be collapsed into “legacy failures” without checking each one. Do not relax fail-closed decision gates just to make old positive-path tests pass; positive-path fixtures should provide explicit, valid council coverage. Do not mark the redesign complete until the route audit, failure triage, focused tests, full CI, walk-forward/holdout evaluation, and controlled demo validation have evidence.

Operational boundary remains unchanged: `.env` is off-limits; no broker orders or live configuration changes are permitted during this work.
## 2026-10-26 recovery pass — post-run findings

This section supersedes older test counts where they differ; it does not mark the redesign complete.

### Latest available full-suite evidence

The latest completed route-audit branch workflow at the time of this snapshot completed lint and collection, but Python 3.12 failed with **175 failed, 3,742 passed, 12 skipped** and Python 3.11 was cancelled. That run predates the fixes listed below. New full CI run [#1942](https://github.com/Eugine336/apex-trader/actions/runs/40280936900) and Gold-focused run [#325](https://github.com/Eugine336/apex-trader/actions/runs/40280936955) are now running against head `cc702c0c353690f4d9d2d3e6b7b61c819688aa78`; their results are pending, so the suite is not yet verified green.

### Root-cause fixes committed after that run

- Cognitive conformance fixtures now include a fresh, explicit execution-quality quote with estimated_total_cost_r, so their EV assertions reach the EV gate rather than failing earlier because execution cost is unverified. Production fail-closed behavior is unchanged.
- RiskConfig.allow_intentional_hedge now defaults to false. Same-instrument opposing exposure requires explicit opt-in.
- The zero-stop-distance regression now expects a non-tradeable zero-lot result (skip_invalid_stop), rather than expecting the minimum lot for an invalid stop.
- The conviction-sizing regression now derives its expected multiplier from the continuous conviction curve instead of asserting an obsolete fixed 0.5 factor.
- Removed process-global fake dependency injection from tests/test_risk_sizing_fixes.py; the old setup could leave mocked torch, numpy, pandas, and other dependencies in sys.modules, contaminating unrelated tests in the same pytest process.

### Remaining verified gaps

- A full-suite rerun was unavailable at the time of the earlier snapshot. CI has since been triggered on the latest branch head; see the current workflow links in the 2026-10-26 validation snapshot. Until those runs complete, the latest changes are not test-passing evidence.
- The repository tree still contains tracked runtime SQLite databases and SQLite -wal/-shm files. They may contain persistent trading, campaign, and learning state. Do not delete them blindly; preserve/export the data before changing tracking policy.
- The static entry-route audit is not runtime proof of every entry, pending-order, reversal, scale-in, exit, and stop-management path. Recording fake-broker call traces and a controlled demo cycle remain necessary.
- The full-suite silent-exception audit reports hundreds of existing handlers. Do not mass-insert noisy logs or weaken the audit to manufacture a pass; classify intentional fail-safe fallbacks and genuinely silent failures, then remediate by subsystem.
- Learning promotion remains unverified against cost-aware walk-forward splits and an untouched holdout. No learning or optimizer threshold should be lowered to make tests pass.
- Broker demo reconciliation and live-position management have not been validated end-to-end.

### Protected operating boundaries

The .env file remains out of scope: do not read, display, copy, recreate, rename, or modify it. No broker orders have been sent and live trading has not been enabled by this recovery pass.
### Additional failure-triage changes in this pass

- All dashboard auth tests now use the Starlette-version-compatible client-address helper; three remaining tests had bypassed the helper and still passed the unsupported client keyword directly.
- The Deriv token-refresh test builder now initializes the retry/failure streak that production connector initialization owns.
- The per-user process environment builder now honors its explicit repo_root argument and omits APEX_REPO_DIR when no root was supplied, rather than leaking the parent checkout path into an isolated process.
- The post-close MFE/MAE observation test now matches the declared read-only, evidence-collection default; this tracker does not itself authorize or mutate a trade.
- The Composio adapter test now asserts the current v3 tools/execute request contract rather than the retired v2 endpoint.

These are code/test-contract corrections inferred from the failing run and source contracts. Because the quota prevents a rerun, each remains pending execution verification.


## 2026-10-26 canonical identity propagation pass

The canonical source remains `cognition/identity.py`; this pass propagates its exact identity statement/version across the lifecycle instead of duplicating alternate wording:

- **Observation:** `MarketState.consolidation()` exposes `identity_version` and the exact canonical identity alongside freshness, coverage, conflict and uncertainty.
- **Analysis and decision:** `DecisionPackage.to_dict()` and `CognitiveBrain.get_status()` expose the same canonical identity/version.
- **Campaign and execution hand-off:** `CampaignSpecification.to_dict()`, `OriginationIntent`, and the immutable execution `Intent` carry identity plus decision/campaign/opportunity provenance. The Brain origination sink propagates those fields into the shared `Intent.open` path.
- **Execution boundary:** `ActionExecutor` rejects a new OPEN intent if its identity version or statement differs from the canonical contract, before any broker dispatch. This does not block risk-reducing management actions on existing positions.
- **Learning:** entry/outcome journal records include the identity version and exact statement, keeping the decision contract attached to later outcome attribution.
- **Regression coverage:** added assertions for observation snapshots, serialized decisions/campaigns, learning records, translator provenance, execution-intent propagation, and rejection of a mismatched OPEN identity. Removed import-time global torch/RL mocks from the bankruptcy and MTF-observation tests to prevent test-order contamination of the RL suite.

**Validation status:** these latest edits have been committed to the route-audit branch, but no test run has executed against the current head. The last available workflow is from an earlier head and failed; Actions quota is exhausted. Do not treat the new assertions or the full suite as verified until tests are run in a local environment or CI becomes available. No broker orders were sent; `.env` remains untouched.


## Verification refresh — 2026-10-26, PR #758 recovery

The live PR head was rechecked before this refresh. The branch includes the identity propagation and management-intent changes described above. The management translator now prefers the Brain decision's `preferred_opportunity_id` (and explicit decision campaign metadata when present) over older position metadata, with a regression test for that precedence. This is a source-level correction; it is not a runtime validation result.

- **Canonical identity source:** implemented in `cognition/identity.py`; duplicate canonical constants were not introduced.
- **Observation/decision/campaign serialization:** implemented in source; focused test execution at the current head remains unverified.
- **Entry intent identity gate:** implemented and covered by tests in source; broker-dispatch behavior remains unverified in this environment.
- **Management identity/provenance:** implemented in the translator and intent factories; translator provenance precedence now has a regression test. The full live-position-to-executor path remains only partially runtime-verified.
- **Broker-route audit:** AST guard implemented; it is not a complete runtime call-graph proof.
- **Learning lineage:** identity fields are present in entry/outcome journal code; end-to-end lineage and OOS/holdout behavior remain unverified.
- **RL test isolation:** global dependency mocks were removed from the two named tests in source; cross-suite isolation is unverified without executing the suite.
- **Validation:** workflow #1911 (run ID 40275835782) is completed with failure on older head `a90a8f540f4f32d06098d24c36fa097218244c1f`. Main workflow #1902 is also failed. No successful workflow is established for the latest head. The current tool environment does not provide a checked-out repository for local test execution, so no local test result is claimed.

No broker orders were sent and live trading was not enabled. `.env` was not accessed or modified.


## Latest CI evidence update — 2026-10-26

The latest completed full workflow on the prior PR head reports 140 failures, 3,785 passes, and 12 skips on Python 3.12; lint passed and Python 3.11 was cancelled. The focused Gold desk workflow passed on both Python 3.11 and 3.12. These are not a full-suite pass. A dashboard TestClient compatibility fix was committed afterward; it is not yet validated by a subsequent run. The PR must remain unmerged while the full suite and critical execution/management tests remain red or unverified.


## Current validation refresh — 2026-10-26

The canonical identity remains sourced from `cognition/identity.py`; no alternate identity constant was added by the recovery work. Identity propagation source coverage is present across observations, decisions, campaigns, entry intents, management intents, and learning records, but full runtime call-path validation is incomplete.

The current recovery head is `313d04fbcd835eb82e19e8db44bd6e0660453c8b`. Its full CI run is still in progress: https://github.com/Eugine336/apex-trader/actions/runs/40306586682. The most recent completed full suite on a prior head failed with 50 failures and 3,888 passes. Gold-focused tests passed on several earlier recovery heads; that is not a full identity/execution proof.

Remaining identity-critical verification:
- prove every broker-mutating path is reachable only after canonical Brain opportunity admission, portfolio/risk gates, and identity validation;
- prove live position management preserves the same identity and campaign/opportunity provenance;
- verify fail-closed behavior for stale/missing feeds and degraded persistence;
- validate outcome lineage and cost-aware walk-forward learning with an untouched holdout.

PR #758 remains draft/unmerged. No live orders have been sent, and `.env` was not accessed or modified.


## System 2 implementation refresh — 2026-10-29

PR #790 remains draft and unmerged. The branch now:
- marks MT5 instrument specifications verified only after a successful live `symbol_info` response;
- exposes Deriv multiplier limits only when broker-discovered, tracks the source observation epoch, and marks cached limits unverified after 15 minutes;
- keeps Deriv multiplier constraints separate from full tick-value/contract-size economics;
- requires explicit synchronization evidence rather than treating timestamp presence as synchronized cross-timeframe data;
- rejects malformed historical rows, sample IDs reused across train/validation/test, and earlier outcome horizons that overlap the next split's decision window.

Still incomplete / unverified:
- authoritative instrument-specific session calendars, timezone/DST and holiday sources are not wired into cognition;
- synchronized cross-asset observations are not produced by a verified live data source;
- real historical outcomes are not yet supplied as point-in-time validation rows, and transaction-cost-aware walk-forward performance is not independently established;
- broker constraints are represented as evidence but have not been proven enforced consistently across the shared qualification and execution path;
- focused tests and `scripts/identity_audit.py` have not been executed on this branch. A local clone attempt could not resolve GitHub in the current execution environment; no test result is claimed.

System 2 is **in progress, not merge-ready**. Missing data remains unknown; no prediction or profitability claim is made. No broker orders were sent. `.env`, runtime databases and broker settings were not accessed or modified during this work.

- The generic `SessionEngine` status is now surfaced into cognition as an explicitly unverified observation. It does not satisfy the requirement for an authoritative instrument-specific calendar, timezone and holiday source.


## System 2 continuation — additional live evidence paths

- Added measured cross-asset return associations for instruments registered by the existing correlation engine. It fetches closed H1 broker candles, requires timezone-aware timestamps, joins exact shared UTC candle times, excludes the forming primary bar, rejects duplicate/naive timestamps and requires at least 20 paired returns. Returns spanning missing H1 bars are excluded. The result is association-only and never asserts causation. Unsupported legs remain absent.
- Added a bounded per-symbol in-process ledger of actual close outcomes and surfaced it as descriptive historical evidence. It is deliberately marked unvalidated because the current close record does not carry per-feature point-in-time timestamps; it cannot be used as proof of a predictive edge.
- Hardened round-trip cost estimation to return unknown/zero when broker spread is missing, invalid or zero. The mandatory net-EV gate then rejects the opportunity instead of treating missing spread as a free market.
- Added tests for synchronized cross-asset evidence, naive/misaligned timestamps, descriptive outcome evidence, and fail-closed cost estimation. These tests remain unexecuted in this environment.

- Commission is now treated as unknown when the configured round-trip amount is zero and no positive realized-deal estimate exists, unless `COGNITION_COMMISSION_MODEL_VERIFIED=true` explicitly attests the zero-commission model. This setting was added to `CognitionConfig` and `.env.example`; the real `.env` was not read or changed. The net-EV gate fails closed when commission is unknown.

- Cross-asset calculations are cached in-process for 60 seconds per symbol to avoid repeatedly fetching related H1 bars on every cognition cycle. Cached evidence retains its source timestamp and normal freshness horizon.

### Follow-up — broker-reported weekly trading windows (2026-10-29)

- `MT5Connector.get_symbol_spec()` now queries `symbol_info_session_trade` when the installed MT5 API exposes it and returns per-symbol weekday/session start and end offsets in **broker-server time**.
- `EventDrivenSystem._cognition_price_snapshot()` surfaces these as separate `SESSION` evidence with provider provenance.
- This does **not** close the full calendar gate: broker-server timezone/DST rules and a complete holiday calendar are not reliably exposed or verified. These windows must not be interpreted as UTC, and the session evidence remains calendar-unverified.
- Added `tests/test_mt5_symbol_sessions.py` for schedule extraction and unavailable-API behavior. These tests have not been executed here; no pass is claimed.

### Follow-up — synchronized timeframe evidence and shared execution constraints (2026-10-29)

- `cognition.market_lenses` now rejects timezone-naive datetime-like timestamps and emits cross-timeframe alignment as synchronized only when at least two intraday frames have source timestamps, candle opens aligned to their declared epoch grids, and completed bars eligible at one shared snapshot cutoff. D1 is excluded from the synchronization assertion because broker daily boundaries may depend on server timezone/DST. Missing provenance keeps the relationship unverified.
- `ActionExecutor` now applies a last-mile broker specification gate to every canonical OPEN: live source/specification provenance, full-trading mode, min/max/step volume, point/stops-level availability, valid directional geometry, and broker minimum stop/target distance are required before the broker call. Unknown or incomplete specs reject new exposure; risk-reducing management actions are not blocked by this OPEN-only check.
- MT5 symbol specs now expose `trade_stops_level` and `trade_freeze_level`. `_money_per_price()` also requires explicitly verified broker tick-value/tick-size data and rejects non-finite or non-positive values.
- The recording broker used by tests now supplies clearly synthetic, explicitly marked test constraints. Regression cases cover naive timestamps, synchronized timeframe observations, unverified specifications, volume-step violations, stop-distance violations and monetary conversion provenance.
- **Operational consequence:** the shared executor will fail closed for connector/spec combinations that do not expose a complete verified MT5-style constraint set (including the current Deriv multiplier-only specification). Those routes need a connector-specific full constraint validator before new exposure can be enabled.
- **Validation still pending:** these tests and `scripts/identity_audit.py` have not been run in a local runtime. The implementation is not declared test-clean. Historical trade-linked feature snapshots with decision-time provenance, complete timezone/DST/holiday calendars, realistic financing costs, and independent walk-forward/untouched-holdout results remain unclosed.

## System 2 continuation — decision-time snapshot and trade linkage (2026-10-30)

PR #792 merged to main at 74e0b845390cebe8385b2812003da8344b59e0cc; its implementation is **not accepted**:

- Both advisor-assisted and native-only Brain campaign paths now capture a point-in-time feature snapshot from the fresh evidence already in the `MarketState`. Each feature carries its evidence ID and original source timestamp; stale/future evidence is excluded, values are normalized for JSON serialization, and snapshots are explicitly marked unvalidated.
- Campaign translation carries the same snapshot into `OriginationIntent`, then the shared `Intent`. The Brain origination sink rejects a missing, incomplete, or decision-ID-mismatched snapshot rather than creating a trade that cannot be attributed.
- The shared flush loop now has a successful-OPEN callback. The bootstrap associates the snapshot with the exact broker ticket and emits a durable `DECISION_TRADE_ENTRY_SNAPSHOT` event. Close attribution attempts exact-ticket recovery from the event ledger after restart and emits `DECISION_TRADE_OUTCOME` with the linked decision/campaign/opportunity IDs, snapshot ID, realized P&L, commission, swap, fee and exit reason.
- Added `tests/test_decision_feature_snapshot.py` for fresh evidence capture, stale/future evidence exclusion, and snapshot propagation through campaign translation.

**Not yet verified:** tests have not been executed in a supported runtime; event-store queue persistence/recovery, provider-specific ticket fields, and full end-to-end entry-to-close attribution still require focused integration tests. This branch must not be described as System 2 complete until those tests and `scripts/identity_audit.py` run successfully and all remaining calendar, connector-constraint, financing-cost and independent validation gates are closed or explicitly accepted as blockers. No live broker orders were sent.



## System 2 durability follow-up — 2026-10-30

- Follow-up implementation PR #793 (draft, not accepted): redesign/system2-durable-provenance, based on 74e0b845390cebe8385b2812003da8344b59e0cc.
- Entry-snapshot and outcome events use synchronous SQLite commits through EventStore.emit(..., durable=True). Failure raises to the producer and marks the store degraded; the event is not acknowledged as persisted merely because it was queued.
- Restart recovery filters by exact parent_id ticket at the SQL layer; it no longer scans a capped set of same-symbol rows and filters tickets in application code.
- Intent construction deep-copies nested decision snapshot content to prevent mutation through the original caller-owned object. Intrinsic immutability is still not claimed.
- DecisionPackage now records a precise creation epoch; snapshot capture uses that epoch as its cutoff and fails closed when the timestamp is missing or invalid. Regression cases cover later-arriving evidence and missing decision chronology.
- Added tests for synchronous commit visibility and exact-ticket query behavior. Not executed. No CI runs or status checks were reported for the base merge commit. The full integration path, supported-runtime test suite, identity audit, exact broker ticket semantics, authoritative calendars, complete connector constraints, verified financing estimates and untouched-holdout validation remain unresolved.



## System 3 native reasoning implementation refresh — 2026-10-30

**Implemented and merged to `main` in PR #796 at commit `94a01c793d7418357f81adf7c8989f965c2c3e5b` on 2026-10-30.**

- Native analysis in `cognition/brain_reasoning.py` now preserves source evidence IDs, source modules, original timestamps, domains, source confidence/uncertainty, observed measurements and pattern-to-evidence lineage.
- The serialized epistemic record distinguishes observed facts, inferred patterns/contradictions, assumptions and unknowns. Missing domains and unknown horizons remain explicit.
- Native hypotheses now have stable IDs, family/type, supporting and contradicting evidence IDs, unresolved unknowns, assumptions, distinguishing evidence, source-declared horizon and decomposed reasoning-strength metadata. The confidence field is explicitly labelled uncalibrated; it is not a probability.
- The leading-hypothesis selector now compares no-trade and non-directional explanations against directional hypotheses rather than automatically preferring any directional candidate.
- Generated counter-hypotheses are challenge-only records and cannot become the selected primary direction. Coverage is based on informative measurements in the six core market domains; empty high-confidence telemetry and non-market metadata do not count as usable market coverage.
- The native classifier preserves conflicts across multiple displacement sources, order-flow sources, same-timeframe trend observations, momentum states, volatility states, statistical z-scores and M1 patterns. It does not select a directional reading based on input order.
- Native evidence freshness is evaluated at the same decision-time cutoff supplied to consolidation. The same cutoff now reaches every native fallback route, verified execution-cost check and market-derived opportunity geometry.
- Full native hypothesis records, the selected hypothesis ID and the observed-fact/inference/assumption/unknown record are retained on both native open and observe decisions.
- Counter-hypotheses are generated for up to the three strongest directional theses and linked to each primary hypothesis ID; when there is no directional thesis, the strongest market-state explanation is challenged. Self-criticism penalizes missing attributable support and contradictory independent source readings.
- Every evidence-relevant lens in the strategy-lens registry now becomes an explicitly non-directional candidate hypothesis with its own source domains, required context and limitations. Volatility-regime and unfamiliar-measurement hypotheses are also preserved without inventing a side. Pattern-source links disclose domain-level candidate-source granularity; unknown measurements retain exact source IDs.
- New deterministic tests cover evidence lineage, epistemic separation, conflicting readings, no-trade comparison, source horizon metadata, stale/future data and decision-time consistency.

**Validation:** focused Gold desk tests and lint passed. Full Python 3.11 and 3.12 suites each reported 3,963 passed and one failure: the silent-exception audit's 180 unclassified handlers. The pre-merge `main` baseline reported 3,948 passed and the same single 180-handler audit failure on both runtimes. No audit rules were weakened or suppressed. The post-merge main workflow remains queued. System 3 implementation is complete; the inherited repository-wide audit is still an outstanding global quality gate, not a new System 3 regression.



## System 4 — opportunity discovery and payoff qualification (2026-10-30)

**Implementation merged:** PR #797 merged to `main` at `b141881641746f9f64e94cbe246d59684bb3c3f5` on 2026-10-31. System 4 remains **NOT ACCEPTED**.

- Audited the existing market-derived geometry resolver and found stale-but-fresh older breakout activation, cross-timeframe momentum confirmation, missing-symbol inheritance, unverified sweep reclaim and arrival-order structural conflict weaknesses.
- Geometry now uses exact symbol identity, declared timeframe, `data_basis=completed_ohlc`, original `source_bar_epoch` age at the decision cutoff, latest structural activation, timeframe-aligned momentum/range response, explicit sweep reclaim and fail-closed same-time structural conflict handling.
- Each directional candidate in the opportunity set receives its own Brain-derived geometry and source evidence IDs; geometry is no longer attached only to the preferred candidate.
- System 3 native confidence is explicitly uncalibrated. The old EV path passed effective confidence into a function whose contract treats the value as win probability. Directional action now requires a separate, fresh, direction-specific and exact-symbol out-of-sample calibration record with verified source, walk-forward/untouched holdout, leakage/selection/overfit controls, method/ID, sample count, Brier score and bounded reliability gap.
- Missing calibration leaves canonical `DecisionPackage.expected_value` at zero, retains any confidence-derived EV only as explicitly labelled diagnostic text, and blocks directional exposure in both advisor-assisted and native-only origination. Execution-cost evidence must match the exact symbol and carry an attributable non-fallback source. Historical calibration records are excluded from market-domain breadth. This deliberately exposes the missing end-to-end calibration producer as an unresolved acceptance dependency rather than treating reasoning confidence as probability.
- Exposure sizing is capped by effective confidence; quality scaling cannot raise it above confidence.

New tests cover temporal/timeframe activation, conflicting structures, exact symbol matching, reversal reclaim, no-reclaim rejection, unfamiliar/low-liquidity states, calibrated-probability provenance and quality, and calibration-domain isolation.

**Validation:** lint passed; focused Gold desk tests passed on Python 3.11 and 3.12. Full Python 3.11 and 3.12 suites each reported 3,990 passed and one failure in `test_no_silent_exception_handlers`: 180 unclassified handlers. The pre-merge `main` run reported the same 180-handler failure, so it is inherited. The full identity audit did not execute after the failing test step. System 4 remains blocked on a selection-aware durable candidate/outcome ledger, independently validated calibration and a structurally enforced producer-attestation boundary. No live orders, runtime database changes or destructive cleanup were performed.

### Calibration data-integrity finding
The current durable campaign memory is not a valid probability-training corpus: it stores completed campaigns only, which are selected by the existing entry gates and omit rejected/deferred candidates. The decision-provenance event pair is valuable exact-ticket lineage for opened trades, but it explicitly records predictive validity as unvalidated and false; it does not resolve the full candidate universe or supply independent holdout labels. The existing instrument CalibrationEngine learns ATR/spread/zone/structure statistics, not direction-specific win probabilities. Consequently, the calibration producer cannot be completed honestly by simply wiring one of these existing stores into the Brain. The missing dependency is a selection-aware candidate/outcome ledger with fixed-horizon labels and chronological leakage-safe validation. Until that exists, the directional calibration gate remains fail-closed. In addition, the Brain currently validates declared calibration flags but has no structurally enforced producer/attestation boundary; `source_verified=true` is not independent proof. A dedicated producer must own issuance and bind the artifact to its dataset, split protocol, symbol, direction and validation results before production trust can be claimed.



## System 1 + System 4 concurrent implementation update — 2026-10-31

This update supersedes the 2026-10-30 statement above that the candidate ledger and producer did not yet exist. The implementation is on draft PR #798, branch `redesign/system1-system4-unified-intelligence`; it is not merged or accepted.

### System 1 changes
- Cognitive contracts normalize their identity version to the canonical source; MarketState, DecisionPackage and CampaignSpecification also normalize the identity statement.
- Repository audit checks the single definition of identity statement/version/non-negotiable principles, identity fields on all cognitive contracts, Brain-only construction of DecisionPackage/CampaignSpecification, pure-analyst import restrictions and the existing ActionExecutor/platform broker-mutation boundary.
- Ingestion time and verified source time are distinct. Evidence with only `timestamp_epoch` is not marked source-time verified. Decision snapshots do not backfill source time from ingestion time.
- The execution-bound decision snapshot requires every included fresh evidence row to carry an explicitly verified source timestamp. Incomplete snapshots remain incomplete and cannot be silently repaired with later data.

### System 4 changes
- The durable ledger captures opportunities and native hypotheses from stand-down/observe decisions, and records final candidate dispositions.
- Fixed-horizon outcome labels preserve exact reference and outcome quote provenance and are recomputed by the producer from verified prices and decision-time risk geometry.
- The producer validates exact candidate, decision, symbol, side, candidate-family and horizon lineage; source-time ordering; fresh non-provisional market evidence; quote/geometry evidence IDs; and final disposition.
- Calibration is scoped by a controlled, explicit candidate family in addition to symbol, side and horizon. Unclassified directional candidates remain observable and block calibration rather than being silently discarded.
- Brain consumes only an exact producer-registry artifact for the same family/symbol/side/horizon, and uses the conservative lower of development and untouched-holdout expected R less verified cost. A favourable-direction base rate is not treated as a target-before-stop win probability.
- The advisor JSON contract now requests the controlled candidate-family field. Native hypotheses already carry an explicit family type; the family is not inferred from thesis prose.

### Current validation boundary
- Tests have been added/updated but have not been run in this session. The latest PR head has no GitHub Actions workflow runs or commit status checks reported, and a local checkout/test runtime was unavailable.
- The code is not proven syntactically valid or runtime-correct yet. Do not mark either system accepted, do not merge, and do not authorize live trading based on this implementation record.
- The family-conditional estimator is a historical expected-return estimator with walk-forward and untouched-holdout checks. It does not establish profitability or current-regime predictive edge without further independent statistical review.
- No broker orders, `.env` edits, runtime database mutation/deletion or credential changes were performed.
