# Apex identity — system-wide implementation audit

> **Audit baseline:** repository main at commit b57f6c8d311d9865a69e9e0a9f6198a4752176a9 (2026-10-23).  
> **Purpose:** replace optimistic documentation-only completion claims with source-backed runtime gates. Refresh this audit after every migration PR.  
> **Authority:** APEX_IDENTITY.md and APEX_Constitution.md.

## Executive status

The identity is now canonical in source and documentation in this branch. That does **not** mean the full identity is implemented. Existing code already has strong pieces (one CognitiveBrain, evidence consolidation, event-driven nudges, MTF chart/tick snapshots, competing native hypotheses, campaign memory, portfolio context and risk/execution gates), but these pieces do not yet prove a complete market-aware trader.

## Findings from actual source inspection

| Area | Existing implementation | Gap that remains | Required closure evidence |
|---|---|---|---|
| Canonical identity | Constitution plus the new machine-readable cognition.identity contract | Earlier README described fixed 10-factor scoring and zone→M1 entry, inconsistent with single-path cognition | Runtime contract exposed and logged; docs refer to the same exact statement |
| Market observation | event_driven_bootstrap._cognition_price_snapshot reads D1/H4/H1/M15/M5/M1 candles, latest tick, recent ticks, depth when available, and an open position | Feed availability, forming-vs-completed data, rate limits and degraded state need instrument-by-instrument validation | Tests for age/freshness, all registered instruments, reconnect/stale feed, and live demo telemetry |
| Structural analysis | brain/decision_core.py: structure, FVG, order blocks, liquidity, volume, inducement, Wyckoff and trend/MR/regime concepts | Coverage/wiring differs by timeframe and module; a computed module may still be stored rather than influential | Runtime trace of every source into MarketState and attributable use in Brain decisions |
| Trading-method breadth | cognition/pattern_rules.py, gold_desk/strategies.py, legacy trigger/entry_patterns.py, and advisor prompt | Native BrainReasoner.generate_hypotheses currently has a short set of explicit hypothesis branches. The legacy M1 detector is not confirmed as a live entry route; a method catalog is not the same as executable strategy competence | Shared all-symbol observation path, live route audit, structured specialist findings, scenario/backtest matrix per family |
| Native inference | cognition/brain_reasoning.py: evidence coverage, regime indicators, 16 deterministic patterns, hypotheses, counter-hypotheses, criticism | No external advisor outage still leaves a constrained/limited hypothesis generator; it cannot credibly be called exhaustive trading knowledge | Broad native hypothesis generation evaluated on adversarial market scenarios without any provider |
| Advisor integration | llm/reasoner.py opportunity-harvesting prompt; cognition keeps advisors subordinate to Brain | A single provider outage can still remove advisor-assisted breadth; verify native fallback qualifies and routes actual campaigns under deployed config | Fault-injection tests for timeout/503/malformed outputs plus demo end-to-end trace |
| Entry decision authority | cognition/loop.py → Brain → _maybe_originate → wired intent sink; docs say legacy pipeline is suppressed under COGNITION_SINGLE_PATH | Need prove effective runtime config, all direct/pending order paths and current environment match the documented single-path state | Static audit, call-site trace, runtime route assertions, pending-order reconciliation and demo tests |
| M1 price action | Five legacy checks exist: engulfing, rejection wick, pin bar, volume spike at zone and inside-bar breakout | This branch begins restoring them as contextual evidence; not an autonomous entry path; range-boundary proxy is not equivalent to institutional-zone matching | Unit tests, source provenance, multi-timeframe confluence tests and ablation/backtest |
| Execution / risk | Shared aggregator, ActionExecutor, RiskGate, broker router and protective management controls | ORG_REDESIGN_PLAN.md reports duplicated/incompletely wired risk controls; full live risk-engine path and all legacy routes need fresh trace | One authoritative execution/risk plane verified with mocked broker plus demo, every reject reason observable |
| Portfolio | Whole-cycle opportunity comparison, campaign registry and exposure context exist | Need prove new positions, reversals, scales and reallocation all use same whole-book truth and cost-aware EV | Whole-book scenario tests plus broker-truth reconciliation |
| Learning | Outcome memory, influence/calibration and pattern tracking exist | Samples, censoring, costs and strategy attribution must be validated; learning cannot promote itself into authority | Out-of-sample replay, calibration metrics and governed promotion/rollback |
| Docs and operations | Constitution, rollout runbook, audit registers and README exist | Prior docs contain conflicting statuses and outdated fixed-pipeline claims | Every contradictory claim corrected or clearly marked historical/superseded; runtime map kept current |
| Production readiness | Focused tests for recent Gold changes have passed historically | Repository-wide CI has existing lint and test-collection problems; no fresh full pass or complete demo evidence | Green full CI, fresh demo cycle, broker/execution validation, roll-forward and rollback proof |

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
- Use event-driven updates and bounded computation; "continuously" means process every material market change, not call an LLM every tick.
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
- Do not enable a data feed, provider, strategy family or execution path simply because the name exists in the repository.
- The M1 pattern module added alongside this audit is a neutral observer; it does not make the old M1 entry pipeline authoritative.
- COGNITION_ORIGINATION_MODE=live only permits a qualified Brain campaign to reach the configured sink. It is not proof that the external provider is healthy, the Brain can qualify a trade on the current market, every symbol is data-complete, or the broker path has passed a live demo.
- This audit lists known gaps and acceptance gates; it is not a statement that all rows are already closed.
