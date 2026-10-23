# Apex identity — system-wide implementation audit

> **Audit baseline:** repository main at commit b57f6c8d311d9865a69e9e0a9f6198a4752176a9 (2026-10-23).  
> **Purpose:** replace optimistic documentation-only completion claims with source-backed runtime gates. Refresh this audit after every migration PR.  
> **Authority:** APEX_IDENTITY.md and APEX_Constitution.md.

## Executive status

The identity is canonical in source and documentation in this branch. The shared cognition snapshot now includes universal completed-bar OHLC observations and contextual M1 patterns when data is available. The focused workflow has passed on Python 3.11 and 3.12 (113 tests per version, compilation and scoped lint). A repository-wide Ruff run is green; the full suite now completes collection but still has numerous failures in legacy-contract, environment/dependency, and test-fixture areas. That does **not** mean the full identity is implemented. Existing code already has strong pieces (one CognitiveBrain, evidence consolidation, event-driven nudges, MTF chart/tick snapshots, competing native hypotheses, campaign memory, portfolio context and risk/execution gates), but these pieces do not yet prove a complete market-aware trader.

## Findings from actual source inspection

| Area | Existing implementation | Gap that remains | Required closure evidence |
|---|---|---|---|
| Canonical identity | Constitution plus the new machine-readable cognition.identity contract | Earlier README described fixed 10-factor scoring and zone→M1 entry, inconsistent with single-path cognition | Runtime contract exposed and logged; docs refer to the same exact statement |
| Market observation | event_driven_bootstrap._cognition_price_snapshot reads D1/H4/H1/M15/M5/M1 candles, latest tick, recent ticks, depth when available, and an open position. cognition.market_lenses.analyze_market_frames now emits completed-bar structure, range/sweep reclaim, momentum, volatility, broker volume when supplied, and MTF-alignment evidence for the shared Brain; original candle timestamps are preserved where the source supplies them and unknown timestamps reduce confidence | Verify every broker/data adapter supplies correctly normalized source times, source freshness, forming/completed distinction, cache age, rate limits and degraded state for every instrument | Tests for stale/unknown timestamp behavior and source-age horizons; all registered instruments; reconnect/stale feed; runtime/demo telemetry |
| Structural analysis | brain/decision_core.py: structure, FVG, order blocks, liquidity, volume, inducement, Wyckoff and trend/MR/regime concepts | Coverage/wiring differs by timeframe and module; a computed module may still be stored rather than influential | Runtime trace of every source into MarketState and attributable use in Brain decisions |
| Trading-method breadth | cognition/pattern_rules.py, gold_desk/strategies.py, legacy trigger/entry_patterns.py, and advisor prompt | Native BrainReasoner.generate_hypotheses currently has a short set of explicit hypothesis branches. The legacy M1 detector is not confirmed as a live entry route; a method catalog is not the same as executable strategy competence | Shared all-symbol observation path, live route audit, structured specialist findings, scenario/backtest matrix per family |
| Native inference | cognition/brain_reasoning.py: evidence coverage, regime indicators, 16 deterministic patterns, hypotheses, counter-hypotheses, criticism | No external advisor outage still leaves a constrained/limited hypothesis generator; it cannot credibly be called exhaustive trading knowledge | Broad native hypothesis generation evaluated on adversarial market scenarios without any provider |
| Advisor integration | llm/reasoner.py opportunity-harvesting prompt; cognition keeps advisors subordinate to Brain | A single provider outage can still remove advisor-assisted breadth; verify native fallback qualifies and routes actual campaigns under deployed config | Fault-injection tests for timeout/503/malformed outputs plus demo end-to-end trace |
| Entry decision authority | cognition/loop.py → Brain → _maybe_originate → wired intent sink; docs say legacy pipeline is suppressed under COGNITION_SINGLE_PATH | Need prove effective runtime config, all direct/pending order paths and current environment match the documented single-path state | Static audit, call-site trace, runtime route assertions, pending-order reconciliation and demo tests |
| M1 price action | cognition.price_action_patterns adds engulfing, pin-bar/rejection wick, inside-bar breakout, range reclaim and broker-volume-at-boundary observations to the shared snapshot for supported instruments. They remain neutral, provisional observations—not order authority | Must test broker bar conventions, source-time freshness and multi-timeframe context; the local range-boundary proxy is not equivalent to institutionally defined zones | Unit tests, adapter provenance and freshness tests, multi-timeframe confluence, ablation/backtest |
| Execution / risk | Shared aggregator, ActionExecutor, RiskGate, broker router and protective management controls | ORG_REDESIGN_PLAN.md reports duplicated/incompletely wired risk controls; full live risk-engine path and all legacy routes need fresh trace | One authoritative execution/risk plane verified with mocked broker plus demo, every reject reason observable |
| Portfolio | Whole-cycle opportunity comparison, campaign registry and exposure context exist | Need prove new positions, reversals, scales and reallocation all use same whole-book truth and cost-aware EV | Whole-book scenario tests plus broker-truth reconciliation |
| Learning | Outcome memory, influence/calibration and pattern tracking exist | Samples, censoring, costs and strategy attribution must be validated; learning cannot promote itself into authority | Out-of-sample replay, calibration metrics and governed promotion/rollback |
| Docs and operations | Constitution, rollout runbook, audit registers and README exist | Prior docs contain conflicting statuses and outdated fixed-pipeline claims | Every contradictory claim corrected or clearly marked historical/superseded; runtime map kept current |
| Secrets / runtime configuration | The tracked `.env` was removed from this branch and replaced with a secret-free `.env.example`; runtime files are now ignored and a security regression test covers template hygiene | Git history still contains past versions of the file. Rotate exposed broker/provider/API credentials; back up local `.env` before updating any checkout where Git still tracks it | Confirm the repository head has no `.env`, rotate affected credentials, audit recent history for active keys, and keep every runtime `.env` local/ignored |
| Production readiness | Latest completed focused workflow passed on Python 3.11 and 3.12 (113 tests each); repository-wide Ruff lint passed. The last completed full-suite run before the latest security/configuration commits collected successfully but reported 182 failed / 3,696 passed / 12 skipped | Full-suite failures span stale/retired API assertions, dependency compatibility, environment expectations, and fixture/runtime mismatches; no full-suite green run or end-to-end broker demo on the latest head | Fresh full-suite result, broker/execution validation, live-route audit, roll-forward and rollback proof |

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

- No source, test, backtest or log can prove guaranteed profitability or capture of every profitable movement.
- Deleting `.env` from the current tree does not purge Git history; rotate credentials previously committed in that file.
- Do not enable a data feed, provider, strategy family or execution path simply because the name exists in the repository.
- The M1 pattern module added alongside this audit is a neutral observer; it does not make the old M1 entry pipeline authoritative.
- COGNITION_ORIGINATION_MODE=live only permits a qualified Brain campaign to reach the configured sink. It is not proof that the external provider is healthy, the Brain can qualify a trade on the current market, every symbol is data-complete, or the broker path has passed a live demo.
- This audit lists known gaps and acceptance gates; it is not a statement that all rows are already closed.
