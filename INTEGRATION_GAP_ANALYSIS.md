# Apex Trader — Event-Driven Integration Gap Analysis

**Audit Date:** 2026-06-18
**Scope:** Full code read of `TradingLoop` (8,535 lines, 49+ subsystems) vs `EventDrivenSystem` (828 lines, 8 components)
**Method:** Raw code execution path tracing — no README/docs trusted

---

## Executive Summary

The event-driven system is a **parallel universe**. It reimplements entry logic (10 gates, M1 confirmation, zone detection) and management logic (14 exit checks) from scratch instead of integrating with the existing 49+ subsystems. The old `TradingLoop` is still created on startup (all subsystems initialized) but never runs its cycle — so the dashboard reads from initialized-but-empty subsystems.

**By the numbers:**
- **49+ subsystems** in TradingLoop
- **8 components** in EventDrivenSystem
- **6 subsystems** reimplemented in parallel (duplicate code paths)
- **37 subsystems** completely missing from event-driven path
- **5 subsystems** connected (WorldModel, TickStore, EventBus, CandleCloseHandler, PlatformManager)
- **1 subsystem** half-wired (Dashboard — `is_live` works but data flows are broken)

---

## 1. Old System Map — What TradingLoop Wires Up

### Core Infrastructure (5)
| Subsystem | Class | Purpose |
|-----------|-------|---------|
| Config | `AppConfig` | Central configuration |
| Platform Manager | `PlatformManager` | Broker mux (MT5 + Deriv) |
| Position Store | `PositionStore` (SQLite) | Persisted positions across restarts |
| Event Store | `EventStore` (SQLite, background writer) | Append-only event log |
| Journal Loop | `asyncio.EventLoop` | Async journal writes |

### Scan Pipeline (6)
| Subsystem | Class | Purpose |
|-----------|-------|---------|
| Scanner | `PairScanner` | 9-module multi-TF analysis + WorldModel |
| Ranker | `PairRanker` | Opportunity ranking by EV/score |
| Scheduler | `ScanScheduler` | Timer-based scan cadence |
| Opportunity Executor | `OpportunityExecutor` | Direction selection from ranked candidates |
| Density Tracker | `OpportunityDensityTracker` | Opportunity density → sizing |
| Volatility Monitor | `SystemVolatilityMonitor` | Global vol → size multiplier |

### Decision Intelligence (5)
| Subsystem | Class | Purpose |
|-----------|-------|---------|
| Decision Engine | `DecisionEngine` | Strategic ENTER/SKIP/HOLD/CLOSE per position |
| Situation Engine | `SituationEngine` | Market situation assessment for open trades |
| Risk Governor | `RiskGovernor` | Entry review with graded risk |
| Decision Journal | `DecisionJournal` | Persists every verdict (JSONL) |
| Decision Trace | `DecisionTraceRecorder` | Threads awareness through pipeline stages |

### Orchestrator (1)
| Subsystem | Class | Purpose |
|-----------|-------|---------|
| Orchestrator | `Orchestrator` | Round table → graded size multiplier [0.15, 1.0] |

### Entry & Execution (3)
| Subsystem | Class | Purpose |
|-----------|-------|---------|
| Entry Engine | `EntryEngine` | Computes SL/TP from ATR, places orders |
| Entry Validator | `EntryValidator` | Pre-entry validation (drawdown, correlation, spread) |
| Execution Monitor | `ExecutionMonitor` | Fill quality tracking (slippage, latency) |

### Trade Management (5)
| Subsystem | Class | Purpose |
|-----------|-------|---------|
| Trade Manager | `TradeManager` | Per-tick lifecycle: SL/TP/BE/trail/partial/stall |
| Managed Positions | `_LockedPositions` | Thread-safe open position registry |
| Re-Entry Manager | `ReEntryManager` | Re-entry after exits |
| Shadow Trade Manager | `TradeManager` (paper) | Paper management for rejected setups |
| Exit Checks (mixin) | `ExitChecksMixin` (857 lines) | 8 strategic exit checks |

### Risk Layer (7)
| Subsystem | Class | Purpose |
|-----------|-------|---------|
| Risk Engine | `RiskEngine` | Position sizing, per-trade risk |
| Drawdown Guard | `DrawdownGuard` | NORMAL→RECOVERY→FROZEN state machine |
| Correlation Engine | `CorrelationEngine` | Cluster exposure limits |
| Portfolio Risk SM | `PortfolioRiskStateMachine` | NORMAL→DEFENSIVE→REDUCING→EMERGENCY |
| Risk Reporter | `RiskReporter` | Risk state for dashboard |
| Account Risk | `AccountRiskManager` | Per-broker/account silos |
| Portfolio Governor | `PortfolioGovernor` | Max positions, daily loss halt, concentration |

### Adaptive Learning (12)
| Subsystem | Class | Purpose |
|-----------|-------|---------|
| ML Adapter | `AdaptiveOptimizer` | Umbrella: ScoreOptimizer, RegimeLearner, PairLearner, SessionLearner |
| Outcome Feedback | `OutcomeFeedback` | Links realised R to modules |
| Signal Ledger | `SignalLedger` | Records every module's directional read per cycle |
| Emitter Feedback | `EmitterFeedbackService` | Per-module graded accuracy |
| Vote Calibrator | `VoteCalibrator` | Accuracy → vote weight (L6) |
| Module Governor | `ModuleGovernor` | ACTIVE→SHADOW→DISABLED lifecycle (L3) |
| Post-Close Tracker | `PostCloseTracker` | MFE/MAE forward checks |
| Gate Tuner | `GateTuner` | Shadow outcomes → gate threshold tuning |
| Counterfactual Engine | `CounterfactualEngine` | Leave-one-out module attribution (L4) |
| Interaction Analyzer | `InteractionAnalyzer` | Leave-K-out pair interaction discovery (L5b) |
| Shadow Store | `ShadowStore` | Persistence for rejected setup outcomes |
| Tuner Agent | `TunerAgent` | Central coordinator for 31 tunables |

### Evolution Engines (7)
| Subsystem | Class | Purpose |
|-----------|-------|---------|
| Capital Allocator | `CapitalAllocator` | Capital across fingerprints (L5.5a) |
| Execution Profiles | `ExecutionProfileManager` | Per-trade parameter vectors (L5.5b) |
| Regime Detector | `RegimeDetector` | Per-pair TRENDING/RANGING/VOLATILE/QUIET (L7) |
| Risk Manager | `RiskManager` | Adaptive circuit-breaker (L8) |
| Behavior Discovery | `BehaviorDiscoveryEngine` | Emergent behavior clustering (L6) |
| Parameter Evolver | `ParameterEvolver` | Mutation + counterfactual replay (L5a) |
| Signal Discovery | `SignalDiscoveryEngine` + `VirtualModuleRegistry` + `VirtualSignalManager` | Emergent signal mining → synthetic voting modules (L5c) |

### Planning (3)
| Subsystem | Class | Purpose |
|-----------|-------|---------|
| Trade Planner | `TradePlanner` | Coordinator: advisors → execution plan |
| Outcome Logger | `OutcomeLogger` | Plan → outcome linkage |
| Calibrator | `Calibrator` | Auto-calibrate planner thresholds |

### Ops / Production Hardening (6)
| Subsystem | Class | Purpose |
|-----------|-------|---------|
| Shutdown Manager | `ShutdownManager` | Graceful flush of all stores |
| Startup Recovery | `StartupRecovery` | Crash marker detection + recovery |
| Health Check | `HealthCheck` | Aggregate /api/health |
| Process Watchdog | `ProcessWatchdog` | Heartbeat + stall detection |
| Tick Profiler | `TickProfiler` | Per-component latency |
| Daily Maintenance | `DailyMaintenance` | Log rotation, DB cleanup, event prune |

### Session & Safety (2)
| Subsystem | Class | Purpose |
|-----------|-------|---------|
| Session Engine | `SessionEngine` | London/NY/Asia session detection |
| News Guard | `NewsGuard` | High-impact news proximity guard |

---

## 2. Event-Driven System Map — What EventDrivenSystem Wires Up

| Component | Source | Purpose |
|-----------|--------|---------|
| EventBus | `tick/event_bus.py` | Thread-safe pub/sub |
| TickStore | `tick/tick_store.py` | Ring buffer, 15Hz coalescing |
| TickRouter | `tick/tick_router.py` | Tick fan-out + callbacks |
| CandleCloseDetector | `tick/candle_close_detector.py` | M1-D1 boundary detection |
| CandleCloseHandler | `scanner/candle_close_handler.py` | Candle close → brain modules → WorldModel |
| WorldModelStore | `brain/world_model.py` | Atomic multi-TF analysis container |
| EntryOrchestrator | `entry/entry_orchestrator.py` | Zone watch → tick entry → M1 confirm → gates |
| PositionEvaluator | `event_driven_bootstrap.py` | Position checks → intents |
| IntentAggregator | `execution/intent_aggregator.py` | Conflict resolution + dedup |
| ActionExecutor | `execution/action_executor.py` | Serialized broker execution + retry |
| ManagementStateStore | `execution/management_state.py` | In-memory breakeven/trailing state |
| MT5TickPoller | `event_driven_bootstrap.py` | MT5 price polling → ticks |
| DerivTickAdapter | `event_driven_bootstrap.py` | Deriv price polling → ticks |

**Total: 13 components.** Of these, 5 are new infrastructure (EventBus, TickStore, TickRouter, CandleCloseDetector, CandleCloseHandler), 4 are parallel reimplementations (EntryOrchestrator, PositionWorker, EntryGate, M1CandleConfirmer), and 4 are new execution pipeline (IntentAggregator, ActionExecutor, ManagementStateStore, PositionEvaluator).

---

## 3. Gap Table

### Legend
- **CONNECTED** — Event-driven system uses the same shared component
- **PARALLEL** — Event-driven system reimplements it (duplicate code, maintenance risk)
- **MISSING** — Event-driven system has no equivalent
- **HALF-WIRED** — Referenced but connection is broken or incomplete

| # | Capability | Old System Component | Status | Detail |
|---|-----------|---------------------|--------|--------|
| **CORE** | | | | |
| 1 | Config | `AppConfig` | **CONNECTED** | EventDrivenSystem receives `config` |
| 2 | Platform Manager | `PlatformManager` | **CONNECTED** | Shared via constructor |
| 3 | Position persistence | `PositionStore` (SQLite) | **MISSING** | ED uses in-memory `ManagementStateStore` — crash = all state lost |
| 4 | Event logging | `EventStore` | **MISSING** | ED never emits domain events (TRADE_OPEN, TRADE_CLOSE, etc.) |
| 5 | WorldModel | `WorldModelStore` | **CONNECTED** | Built in Phase 1 |
| **SCAN** | | | | |
| 6 | Multi-TF analysis | `PairScanner` (9 modules) | **CONNECTED** | CandleCloseHandler runs brain modules → WorldModel |
| 7 | Pair ranking | `PairRanker` | **MISSING** | ED entry path uses zone proximity, not ranked opportunity EV |
| 8 | Scan scheduling | `ScanScheduler` | **REPLACED** | Candle-close events replace timer — correct by design |
| 9 | Opportunity selection | `OpportunityExecutor` | **MISSING** | No graded direction selection from ranked candidates |
| 10 | Density tracking | `OpportunityDensityTracker` | **MISSING** | No scan frequency → sizing feedback |
| 11 | Volatility sizing | `SystemVolatilityMonitor` | **MISSING** | No global vol → size multiplier |
| **DECISION** | | | | |
| 12 | Strategic decisions | `DecisionEngine` | **MISSING** | No ENTER/SKIP conviction scoring, no thesis integrity, no reversal logic |
| 13 | Situation assessment | `SituationEngine` | **MISSING** | No per-trade market situation (regime, momentum, structure) |
| 14 | Risk review | `RiskGovernor` | **MISSING** | ED has simple currency-count check, not graded risk review |
| 15 | Decision journal | `DecisionJournal` | **MISSING** | No persisted decision audit trail |
| 16 | Decision trace | `DecisionTraceRecorder` | **MISSING** | No cross-stage awareness threading |
| **ORCHESTRATOR** | | | | |
| 17 | Round table sizing | `Orchestrator` | **MISSING** | No multi-dimensional evidence → graded size multiplier |
| **ENTRY** | | | | |
| 18 | Entry engine (ATR SL/TP) | `EntryEngine` | **PARALLEL** | ED uses `EntryOrchestrator` + `ZoneWatcher` — different architecture, same purpose |
| 19 | Entry validation | `EntryValidator` | **PARALLEL** | ED uses `EntryGate` — reimplements 10 gates with callback pattern |
| 20 | M1 confirmation | `EntryEngine._detect_m1_choch` | **PARALLEL** | ED uses `M1CandleConfirmer` — reimplements same CHoCH/BOS + momentum logic |
| 21 | Execution monitor | `ExecutionMonitor` | **MISSING** | No fill quality tracking (slippage, latency, requotes) |
| **MANAGEMENT** | | | | |
| 22 | Trade lifecycle | `TradeManager` (13 checks) | **PARALLEL** | ED uses `PositionWorker` — reimplements all 14 checks as pure functions |
| 23 | Position registry | `_LockedPositions` | **PARALLEL** | ED uses `ManagementStateStore` (different data model) |
| 24 | Strategic exits | `ExitChecksMixin` (8 checks) | **PARALLEL** | Reimplemented in `PositionWorker` as additional checks |
| 25 | Re-entry logic | `ReEntryManager` | **MISSING** | No re-entry after exits |
| 26 | Shadow paper trading | Shadow TM + `ShadowStore` | **MISSING** | No counterfactual paper trading of rejected setups |
| **RISK** | | | | |
| 27 | Position sizing | `RiskEngine` | **HALF-WIRED** | ED creates `PositionSizer` per entry call but does NOT use `RiskEngine` for drawdown mode, daily caps |
| 28 | Drawdown state machine | `DrawdownGuard` | **MISSING** | No NORMAL→RECOVERY→FROZEN progression |
| 29 | Correlation limits | `CorrelationEngine` | **HALF-WIRED** | ED has simple currency-count check, not CorrelationEngine's cluster analysis |
| 30 | Portfolio heat SM | `PortfolioRiskStateMachine` | **MISSING** | No NORMAL→DEFENSIVE→REDUCING→EMERGENCY |
| 31 | Risk reporting | `RiskReporter` | **MISSING** | No risk state for dashboard |
| 32 | Account risk silos | `AccountRiskManager` | **MISSING** | No per-broker/account independent risk |
| 33 | Portfolio governor | `PortfolioGovernor` | **MISSING** | No max positions halt, no daily loss halt, no concentration limits |
| **ADAPTIVE** | | | | |
| 34 | Score optimization | `ScoreOptimizer` | **MISSING** | No adaptive scoring weight updates |
| 35 | Regime learning | `RegimeLearner` | **MISSING** | No per-regime entry threshold learning |
| 36 | Pair learning | `PairLearner` | **MISSING** | No per-pair win rate → sizing |
| 37 | Session learning | `SessionLearner` | **MISSING** | No per-session profile optimization |
| 38 | Outcome feedback | `OutcomeFeedback` | **MISSING** | No per-module accuracy from realised R |
| 39 | Signal ledger | `SignalLedger` | **MISSING** | No per-cycle module directional grading |
| 40 | Emitter feedback | `EmitterFeedbackService` | **MISSING** | No accuracy service for calibration |
| 41 | Vote calibrator | `VoteCalibrator` | **MISSING** | No accuracy → vote weight (L6) |
| 42 | Module governor | `ModuleGovernor` | **MISSING** | No ACTIVE→SHADOW→DISABLED lifecycle (L3) |
| 43 | Post-close tracker | `PostCloseTracker` | **MISSING** | No forward MFE/MAE checks |
| 44 | Gate tuner | `GateTuner` | **MISSING** | No shadow → gate threshold auto-tune |
| 45 | Counterfactual | `CounterfactualEngine` | **MISSING** | No leave-one-out module attribution (L4) |
| 46 | Interaction analysis | `InteractionAnalyzer` | **MISSING** | No module pair synergy/toxicity (L5b) |
| 47 | Tuner agent | `TunerAgent` (31 tunables) | **MISSING** | No centralized auto-tuning |
| **EVOLUTION** | | | | |
| 48 | Capital allocation | `CapitalAllocator` | **MISSING** | No fingerprint-based capital (L5.5a) |
| 49 | Execution profiles | `ExecutionProfileManager` | **MISSING** | No per-trade parameter vectors (L5.5b) |
| 50 | Regime detection | `RegimeDetector` | **MISSING** | No per-pair regime classifier (L7) |
| 51 | Adaptive risk | `RiskManager` (L8) | **MISSING** | No adaptive circuit-breaker |
| 52 | Behavior discovery | `BehaviorDiscoveryEngine` | **MISSING** | No emergent behavior clustering (L6) |
| 53 | Parameter evolution | `ParameterEvolver` | **MISSING** | No mutation + replay (L5a) |
| 54 | Signal discovery | `SignalDiscoveryEngine` + `VirtualModuleRegistry` | **MISSING** | No emergent signal mining (L5c) |
| **PLANNING** | | | | |
| 55 | Trade planner | `TradePlanner` | **MISSING** | No advisor coordination |
| 56 | Outcome logger | `OutcomeLogger` | **MISSING** | No plan → outcome linkage |
| 57 | Calibrator | `Calibrator` | **MISSING** | No planner threshold calibration |
| **OPS** | | | | |
| 58 | Graceful shutdown | `ShutdownManager` | **MISSING** | ED stops threads but doesn't flush adaptive stores |
| 59 | Startup recovery | `StartupRecovery` | **HALF-WIRED** | ED has `_recover_open_positions()` but doesn't restore drawdown/risk/governor state |
| 60 | Health check | `HealthCheck` | **MISSING** | No /api/health aggregate |
| 61 | Process watchdog | `ProcessWatchdog` | **MISSING** | No heartbeat / stall detection |
| 62 | Tick profiler | `TickProfiler` | **MISSING** | No per-component latency profiling |
| 63 | Daily maintenance | `DailyMaintenance` | **MISSING** | No log rotation, DB cleanup, event prune |
| **SESSION** | | | | |
| 64 | Session detection | `SessionEngine` | **HALF-WIRED** | ED passes `is_session_active` callback but the callback is a stub returning True |
| 65 | News guard | `NewsGuard` | **HALF-WIRED** | ED passes `is_news_clear` callback but the callback is a stub returning True |
| **DASHBOARD** | | | | |
| 66 | Dashboard data | `LiveState` (21 mixins) | **HALF-WIRED** | `is_live` works via ED system. But 16 mixins read from `_trading_loop` which is initialized but never exercised — returns empty/stale data |

### Score

| Status | Count |
|--------|-------|
| CONNECTED | 5 |
| REPLACED (correctly) | 1 |
| PARALLEL (duplicate code) | 6 |
| HALF-WIRED | 6 |
| MISSING | 48 |
| **Total** | **66** |

---

## 4. The Root Cause

In `main.py` line 141, even in event-driven mode:

```python
trading_loop = TradingLoop(config)       # ← ALL 49+ subsystems initialized
platform_manager = trading_loop.platforms
...
ed_system = EventDrivenSystem(config, platform_manager)  # ← only 13 components
```

The `EventDrivenSystem` receives a bare `PlatformManager` and `AppConfig`. It has **no access** to the 49+ subsystems that `TradingLoop` just created. Those subsystems sit initialized but dormant — never fed data, never exercised.

Meanwhile, `LiveState.attach(trading_loop, platform_manager, connection_status)` gives the dashboard access to `trading_loop` — so dashboard mixins can technically read from `trading_loop.risk_engine`, `trading_loop.scanner`, etc. But since the trading loop's cycle never runs, those subsystems hold their initial (empty) state.

---

## 5. Integration Plan (Ordered by Impact)

### Priority 1: MONEY AT RISK — Fix Within 1 Session

| # | Fix | What | Files |
|---|-----|------|-------|
| **P1.1** | Wire trade-close feedback | When ActionExecutor closes a position, fire the same 23-step feedback chain that `_record_closed_trade` fires: drawdown, risk_engine, governor, journal, ML, counterfactual, capital allocator, etc. | `event_driven_bootstrap.py` |
| **P1.2** | Wire portfolio risk state machine | ED system must check `PortfolioRiskStateMachine` on every eval cycle — DEFENSIVE blocks entries, REDUCING closes weakest, EMERGENCY force-closes. Currently no portfolio heat monitoring at all. | `event_driven_bootstrap.py` |
| **P1.3** | Wire DrawdownGuard | Every position close must feed `drawdown.register_trade_result()`. Every entry must check `drawdown.mode`. Currently entries have no drawdown awareness. | `event_driven_bootstrap.py` |
| **P1.4** | Wire PortfolioGovernor | Max positions, daily loss halt, currency/sector concentration — currently only a simple currency count check exists. | `event_driven_bootstrap.py` |
| **P1.5** | Wire AccountRiskManager | Per-account daily loss caps, per-account heat blocking. Currently all accounts treated as one pool. | `event_driven_bootstrap.py` |
| **P1.6** | Persist position state | Write `ManagementState` to SQLite (mirror `PositionStore` pattern). Crash recovery must restore breakeven/trailing/partial state. | `execution/management_state.py` |

**How:** Pass `TradingLoop` subsystem references into `EventDrivenSystem.__init__()` — or better, extract them into a shared `SystemContext` that both paths can read. The subsystems (RiskEngine, DrawdownGuard, etc.) are already instantiated; ED just needs handles to them.

### Priority 2: SYSTEM DOESN'T LEARN — Fix Within 1 Session

| # | Fix | What | Files |
|---|-----|------|-------|
| **P2.1** | Wire TunerAgent to ED close events | On trade close: route through `_tuner_agent.on_trade_close()` which cascades to all 31 tunables | `event_driven_bootstrap.py` |
| **P2.2** | Wire Signal Ledger to scan events | On each CandleCloseHandler run, record module directional reads via `_signal_ledger.record()` | `scanner/candle_close_handler.py` or `event_driven_bootstrap.py` |
| **P2.3** | Wire Outcome Feedback | On trade close, call `_outcome_feedback.complete()` to link realised R back to driving modules | `event_driven_bootstrap.py` |

**How:** Accept `TunerAgent`, `SignalLedger`, and `OutcomeFeedback` as dependencies in `EventDrivenSystem.__init__()`. Fire them on the appropriate events.

### Priority 3: DASHBOARD IS BLIND — Fix Within 1 Session

| # | Fix | What | Files |
|---|-----|------|-------|
| **P3.1** | Add ED-aware paths to dashboard mixins | Each Category A mixin (16 of them) needs an `elif self._event_driven_system is not None:` branch that reads from ED's WorldModelStore, TickStore, EntryOrchestrator stats, etc. | `dashboard/state_*.py` (16 files) |
| **P3.2** | Emit domain events from ED | When ED places an order or closes a trade, emit `ORDER_SENT`, `ORDER_FILLED`, `TRADE_OPEN`, `TRADE_CLOSE` events to EventStore. Dashboard's Category B mixins (events, decision_trace, shadow) will then show ED activity. | `event_driven_bootstrap.py` |
| **P3.3** | Wire TradeJournal | On trade close, write to `TradeJournal` so the history panel and performance panel have data. | `event_driven_bootstrap.py` |

### Priority 4: ENTRY QUALITY — Fix Within 1-2 Sessions

| # | Fix | What | Files |
|---|-----|------|-------|
| **P4.1** | Wire DecisionEngine for entries | Before placing an order, run `DecisionEngine.decide_entry()` for strategic conviction scoring. Currently ED fires on any zone touch + M1 confirm with no strategic gate. | `event_driven_bootstrap.py` |
| **P4.2** | Wire Orchestrator for sizing | Replace the simple `PositionSizer` call with the Orchestrator round table — multi-dimensional evidence → graded size multiplier. Currently every entry gets flat sizing. | `event_driven_bootstrap.py` |
| **P4.3** | Wire EntryEngine for ATR SL/TP | The ED entry path uses zone-based SL/TP from WorldModel. EntryEngine's ATR-based SL/TP calculation produces better risk-adjusted levels. Wire it in parallel and take the tighter of the two. | `event_driven_bootstrap.py` or `entry/entry_orchestrator.py` |
| **P4.4** | Wire session/news callbacks | Replace stub `is_session_active` and `is_news_clear` callbacks with actual `SessionEngine` and `NewsGuard` calls. | `event_driven_bootstrap.py` |

### Priority 5: MANAGEMENT QUALITY — Fix Within 1 Session

| # | Fix | What | Files |
|---|-----|------|-------|
| **P5.1** | Wire DecisionEngine for open positions | On each eval cycle, run `DecisionEngine.decide_management()` per position for strategic HOLD/CLOSE/TIGHTEN/SCALE_IN verdicts that override tick-level checks. | `event_driven_bootstrap.py` |
| **P5.2** | Wire Execution Profiles | Select per-trade execution parameter vectors based on context (horizon × regime × consensus). | `event_driven_bootstrap.py` |

### Priority 6: OPS SAFETY — Fix Within 1 Session

| # | Fix | What | Files |
|---|-----|------|-------|
| **P6.1** | Wire ShutdownManager | On stop, flush all adaptive SQLite stores, clear crash marker. | `event_driven_bootstrap.py` |
| **P6.2** | Wire full startup recovery | Restore drawdown mode, risk engine state, governor daily P&L, per-account silos — not just open positions. | `event_driven_bootstrap.py` |
| **P6.3** | Wire Daily Maintenance | Log rotation, DB cleanup, event prune at day boundary. | `event_driven_bootstrap.py` |
| **P6.4** | Wire ProcessWatchdog | Heartbeat + stall detection in the event loop. | `event_driven_bootstrap.py` |

### Priority 7: ELIMINATE DUPLICATES — Fix Within 2 Sessions

| # | Fix | What | Files |
|---|-----|------|-------|
| **P7.1** | PositionWorker → use TradeManager | Instead of reimplementing 14 checks, wrap `TradeManager.update()` in a stateless adapter that returns intents. Both code paths must share the same business rules. | `execution/position_worker.py`, `management/trade_manager.py` |
| **P7.2** | EntryGate → use EntryValidator | Instead of reimplemented gates, call `EntryValidator.validate()` and convert results to `GateResult`. | `entry/entry_gate.py` |
| **P7.3** | M1CandleConfirmer → use EntryEngine methods | Extract `_detect_m1_choch` and `_detect_momentum_confirmation` from `EntryEngine` into shared functions both paths call. | `trigger/entry_engine.py`, `entry/m1_confirmation.py` |

---

## 6. Recommended Implementation Order

**Session A (P1):** Wire all risk subsystems into ED. This is the most dangerous gap — real money with no portfolio risk monitoring, no drawdown guard, no daily loss halt.

**Session B (P2 + P3.2 + P3.3):** Wire learning + domain events + journal. System starts learning from its trades and dashboard history panel works.

**Session C (P3.1):** Dashboard mixins get ED-aware data paths. Dashboard becomes fully functional.

**Session D (P4):** Entry quality — DecisionEngine, Orchestrator, ATR SL/TP, real session/news callbacks.

**Session E (P5 + P6):** Management quality + ops safety. DecisionEngine for open trades, shutdown/recovery, maintenance.

**Session F (P7):** Eliminate duplicate code paths. Single source of truth for all business rules.

---

## 7. The Key Architectural Decision

The cleanest integration path is NOT to rewrite `EventDrivenSystem`. It's to **pass the existing subsystems in as dependencies**:

```python
# Current (broken):
ed_system = EventDrivenSystem(config, platform_manager)

# Target (integrated):
ed_system = EventDrivenSystem(
    config=config,
    platform_manager=platform_manager,
    risk_engine=trading_loop.risk_engine,
    drawdown_guard=trading_loop.drawdown,
    portfolio_governor=trading_loop._governor,
    portfolio_risk_sm=trading_loop._portfolio_risk_sm,
    account_risk=trading_loop._account_risk,
    correlation_engine=trading_loop.correlation,
    decision_engine=trading_loop._decision_engine,
    situation_engine=trading_loop._situation_engine,
    orchestrator=trading_loop._orchestrator,
    trade_journal=trading_loop.journal,
    ml_adapter=trading_loop.ml,
    signal_ledger=trading_loop._signal_ledger,
    outcome_feedback=trading_loop._outcome_feedback,
    tuner_agent=trading_loop._tuner_agent,
    entry_engine=trading_loop.entry_engine,
    session_engine=trading_loop.session_engine,
    news_guard=trading_loop.news_guard,
    position_store=trading_loop.position_store,
    shadow_store=trading_loop._shadow_store,
    # ... etc
)
```

Or better — extract a `SystemContext` dataclass from `TradingLoop.__init__` that holds all shared subsystems, and pass that single object to both `TradingLoop` and `EventDrivenSystem`. This eliminates the dependency on `TradingLoop` being instantiated just to create subsystems that ED needs.
