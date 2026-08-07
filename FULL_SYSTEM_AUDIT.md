# APEX TRADER — Full System ED Integration Audit

**Date:** 2026-06-19
**Commit:** 8c477f25e8e1d680cb0c25e0bdc9fe31e0a0206d
**Method:** Every runtime .py file read and cross-referenced against SystemContext + event_driven_bootstrap.py
**Files audited:** 186 runtime files (excluding tests, scripts, rl/, backtest/)

---

## EXECUTIVE SUMMARY

The event-driven system places trades, manages positions at tick level, and feeds close results to risk + learning layers. But it has **one root-cause gap that silently disables 90% of the adaptive learning system** and **multiple dashboard/management blind spots**.

### By the numbers

| Status | Count | % |
|--------|------:|--:|
| CONNECTED | 72 | 39% |
| PARTIALLY CONNECTED | 48 | 26% |
| DISCONNECTED | 52 | 28% |
| DEAD CODE | 14 | 7% |
| **Total** | **186** | |

---

## ROOT CAUSE #1 — TunerAgent Has No Registered Tunables

**File:** `adaptive/tunable_adapters.py`
**Impact:** CRITICAL — Disables ALL periodic learning

The TunerAgent is on SystemContext and `on_trade_close()` fires on every close. But with **zero registered tunable adapters**, it's a no-op. In the old TradingLoop (`main_loop.py` line 1242+), 31 adapters are registered. The ED system registers none.

**Consequence — these subsystems are initialized, data is recorded, but learning NEVER runs:**
- ScoreOptimizer — `optimize()` never called
- RegimeLearner — `learn()` never called
- PairLearner — `learn()` never called
- SessionLearner — `learn()` never called
- SignalLedger — `run_grading_cycle()` never called
- VoteCalibrator — `calibrate()` never called
- ModuleGovernor — `evaluate_transitions()` never called
- GateTuner — `calibrate()` never called
- CounterfactualEngine — `maybe_recompute()` never called
- InteractionAnalyzer — `maybe_recompute()` never called
- SignalDiscoveryEngine — `maybe_recompute()` never called
- VirtualSignalManager — `evaluate()` never called
- PostCloseTracker — `tick()`/`process_pending()` never called

**Fix:** Register all tunable adapters in `EventDrivenSystem.__init__()` or `start()`, mirroring `main_loop.py:_setup_tuner_agent()`.

---

## ROOT CAUSE #2 — Dashboard Still References _trading_loop

**Files:** 6 dashboard mixins read from `_trading_loop` which is `None`
**Impact:** HIGH — Multiple dashboard panels show empty/disabled data

### DISCONNECTED (show empty in ED mode):
- `state_brain.py` — Module votes, opportunity ranker → dark
- `state_governor.py` — Portfolio governor panel → defaults
- `state_learning.py` — ALL 16 learning sub-sections → `{"enabled": false}`
- `state_ml.py` — ML insights → empty
- `state_module_governor.py` — Module governance → idle
- `state_operations.py` — Entire ops control room → blank

### PARTIALLY CONNECTED (some data, some gaps):
- `state_controls.py` — pause/resume/emergency_close → "no engine attached"
- `state_helpers.py` — Trade journal cache → stale/empty
- `state_history.py` — Depends on journal cache → empty
- `state_orchestrator.py` — Config shows disabled
- `state_performance.py` — System performance → empty
- `state_position_health.py` — Config → defaults

### Crash-safe: No file will crash — all use `getattr(None, ..., None)` patterns.

**Fix:** Each disconnected mixin needs an ED-aware path reading from SystemContext subsystems instead of `_trading_loop`.

---

## ROOT CAUSE #3 — No Continuous Portfolio Heat Monitoring

**File:** `platforms/trading_loop/risk_heat_mixin.py` (DISCONNECTED)
**Impact:** HIGH — Money at risk

The old TradingLoop ran continuous portfolio heat monitoring every cycle:
- DEFENSIVE → move positions to breakeven
- REDUCING → close weakest positions
- EMERGENCY → force-close all

The ED system only checks portfolio risk state at **entry time** (Gate 2). Once positions are open, there is **no ongoing heat monitoring**. A drawdown during open positions triggers no protective action until the next entry attempt.

**Fix:** Add a periodic heat check in the TickEvalLoop or PositionEvaluator that reads `PortfolioRiskStateMachine` and generates CLOSE/MODIFY_SL intents for positions when heat escalates.

---

## ROOT CAUSE #4 — Shadow Resolution Is Dead

**File:** `platforms/trading_loop/shadow_live_mixin.py` (DISCONNECTED)
**Impact:** MEDIUM — Counterfactual data broken

Shadow contracts (rejected trade setups tracked as paper trades) are created by `_record_shadow_rejection()` in the bootstrap but **never resolved**. The old TradingLoop had a live-feed resolution loop that advanced shadows. The ED system creates shadows but never checks if they would have won or lost.

**Fix:** Add shadow resolution to the TickEvalLoop — on each tick, advance open shadow contracts and resolve them when SL/TP is hit.

---

## ROOT CAUSE #5 — DecisionEngine/Orchestrator Management Path Unused

**Files:** `decision/engine.py`, `decision/situation.py`, `brain/orchestrator.py`
**Impact:** MEDIUM — No strategic thesis management for open positions

These subsystems have TWO interfaces:
1. `decide_entry()` / `assess_entry()` / `evaluate_proposal()` → **CONNECTED** (used for entries)
2. `decide_management()` / `assess_open_trade()` / `evaluate_open_position()` → **NOT CALLED**

The ED system makes entry decisions strategically but manages open positions purely mechanically (price vs SL/TP). No thesis validation, no conviction tracking over time, no strategic HOLD/CLOSE/TIGHTEN decisions.

**Fix:** Wire `SituationEngine.assess_open_trade()` + `DecisionEngine.decide_management()` into the PositionEvaluator cycle for each open position.

---

## FULL DIRECTORY-BY-DIRECTORY AUDIT

### core/ (2 files)
| File | Status | Notes |
|------|--------|-------|
| system_context.py | CONNECTED | Central subsystem container, 690 lines, 40+ subsystems |
| __init__.py | CONNECTED | Package init |

### entry/ (6 files)
| File | Status | Notes |
|------|--------|-------|
| entry_orchestrator.py | CONNECTED | Wired through bootstrap, not EventBus directly |
| zone_watcher.py | CONNECTED | Called by EntryOrchestrator |
| tick_entry_detector.py | CONNECTED | Called by EntryOrchestrator |
| m1_confirmation.py | CONNECTED | Called by EntryOrchestrator |
| entry_gate.py | CONNECTED | Called by EntryOrchestrator |
| models.py | CONNECTED | Data models |

### execution/ (7 files)
| File | Status | Notes |
|------|--------|-------|
| action_executor.py | CONNECTED | Broker execution pipeline |
| intent_aggregator.py | CONNECTED | Conflict resolution |
| position_worker.py | CONNECTED | 14 management checks |
| management_state.py | CONNECTED | SQLite-backed state |
| position_snapshot.py | CONNECTED | Data model |
| intents.py | CONNECTED | Data model |
| risk_gate.py | CONNECTED | Pre-execution validation |

### tick/ (5 files)
| File | Status | Notes |
|------|--------|-------|
| event_bus.py | CONNECTED | Core pub/sub infrastructure |
| tick_router.py | CONNECTED | Central tick hub |
| tick_store.py | CONNECTED | Ring buffer with coalescing |
| candle_close_detector.py | CONNECTED | Derives close events from ticks |
| models.py | CONNECTED | Data models |

### scanner/ (5 files)
| File | Status | Notes |
|------|--------|-------|
| candle_close_handler.py | CONNECTED | Analysis plane — candle close → brain modules → WorldModel |
| pair_scanner.py | PARTIALLY | Owns WorldModelStore, used by CandleCloseHandler. Consensus pipeline not driven by ED |
| pair_ranker.py | PARTIALLY | On SystemContext but ranking not used for ED zone-based entries |
| scan_scheduler.py | DISCONNECTED | Timer-based, replaced by candle-close events |
| rr_helper.py | DISCONNECTED | Pure utility |

### decision/ (6 files)
| File | Status | Notes |
|------|--------|-------|
| engine.py | PARTIALLY | `decide_entry()` CONNECTED, `decide_management()` NOT CALLED |
| situation.py | PARTIALLY | `assess_entry()` CONNECTED, `assess_open_trade()` NOT CALLED |
| governor.py | PARTIALLY | `review_entry()` CONNECTED, `review()` (management) NOT CALLED |
| journal.py | PARTIALLY | `log_entry()` CONNECTED, `log()` (management) NOT CALLED |
| actions.py | CONNECTED | Data models |
| context.py | CONNECTED | Data models |

### brain/ (35 files)
| File | Status | Notes |
|------|--------|-------|
| world_model.py | CONNECTED | Core analysis data structure |
| orchestrator.py | CONNECTED | Entry sizing (management evaluation NOT called) |
| correlation_engine.py | CONNECTED | Entry risk gating |
| drawdown_guard.py | CONNECTED | Entry gate + close feedback |
| execution_monitor.py | CONNECTED | Fill quality tracking |
| outcome_feedback.py | CONNECTED | Entry + close feedback |
| session_engine.py | CONNECTED | Session + news guard |
| trade_journal.py | CONNECTED | Trade logging |
| regime_detector.py | CONNECTED | Regime classification |
| opportunity_density.py | PARTIALLY | On SystemContext, not directly invoked |
| fvg_detector.py | PARTIALLY | Used by CandleCloseHandler, not on SystemContext |
| order_block.py | PARTIALLY | Used by CandleCloseHandler |
| structure_engine.py | PARTIALLY | Used by CandleCloseHandler |
| liquidity_mapper.py | PARTIALLY | Used by CandleCloseHandler |
| volume_analyzer.py | PARTIALLY | Used by CandleCloseHandler |
| wyckoff_engine.py | PARTIALLY | Used by CandleCloseHandler |
| inducement_detector.py | PARTIALLY | Used by CandleCloseHandler |
| instrument_profile.py | PARTIALLY | Used by CandleCloseHandler |
| currency_strength.py | PARTIALLY | Used by CorrelationEngine, not ED directly |
| directional_consensus.py | DISCONNECTED | Entire vote/consensus pipeline not driven by ED |
| opportunity_ranker.py | DISCONNECTED | Scan pipeline only |
| decision_trace.py | DISCONNECTED | TradingLoop-only tracing |
| setup_quality.py | DISCONNECTED | Scan pipeline only |
| momentum_divergence.py | DISCONNECTED | Vote pipeline only |
| session_vwap.py | DISCONNECTED | Vote pipeline only |
| atr_percentile.py | DISCONNECTED | Scan pipeline utility |
| swap_model.py | DISCONNECTED | Financing cost model |
| volatility_stop.py | DISCONNECTED | Used by EntryEngine internally |
| volume_profile.py | DISCONNECTED | Analysis utility |
| market_data_utils.py | DISCONNECTED | Data utility |
| symbol_mapper.py | DISCONNECTED | Setup utility |
| smoothing.py | PARTIALLY | Used by RiskEngine indirectly |
| broker_autodiscovery.py | DISCONNECTED | Startup-only |
| backtest_engine.py | DISCONNECTED | Standalone tool |
| mtf_orchestrator.py | DISCONNECTED | Scan pipeline only |

### risk/ (9 files)
| File | Status | Notes |
|------|--------|-------|
| risk_engine.py | PARTIALLY | `record_trade_result()` called, `assess()` NOT called for entries |
| position_sizer.py | CONNECTED | Direct import in bootstrap |
| portfolio_risk_state.py | PARTIALLY | Referenced but reduction/emergency helpers NOT used |
| account_risk.py | PARTIALLY | Close-time wired, per-tick heat NOT tracked |
| daily_tracker.py | PARTIALLY | Indirect via RiskEngine |
| risk_reporter.py | PARTIALLY | On SystemContext, `generate_report()` never called |
| spread_monitor.py | DISCONNECTED | Lives inside RiskEngine, not used by ED |
| spread_bootstrap.py | DISCONNECTED | Startup utility |
| risk_accumulation.py | DISCONNECTED | Not used by ED entry path |

### governor/ (2 files)
| File | Status | Notes |
|------|--------|-------|
| portfolio_governor.py | PARTIALLY | Close-time P&L update wired, `check()` entry gate NOT called |
| models.py | CONNECTED | Data models |

### management/ (6 files)
| File | Status | Notes |
|------|--------|-------|
| opportunity_executor.py | CONNECTED | On SystemContext |
| exit_cause.py | CONNECTED | Enum taxonomy |
| re_entry.py | PARTIALLY | On SystemContext, NEVER CALLED |
| trade_manager.py | DISCONNECTED | Superseded by PositionWorker |
| trailing_stop.py | DISCONNECTED | Superseded by PositionWorker |
| partial_close.py | DISCONNECTED | Superseded by PositionWorker |

### adaptive/ (29 files)
| File | Status | Notes |
|------|--------|-------|
| tuner_agent.py | PARTIALLY | `on_trade_close()` fires, NO TUNABLES REGISTERED → no-op |
| tunable_adapters.py | PARTIALLY | EXISTS but never imported/registered by bootstrap |
| optimizer.py | PARTIALLY | `register_new_trade()` called, `run_optimization()` NEVER called |
| signal_ledger.py | PARTIALLY | Entry+close recording works, `run_grading_cycle()` NEVER called |
| counterfactual.py | PARTIALLY | open+close recording works, `maybe_recompute()` NEVER called |
| module_governor.py | PARTIALLY | On SystemContext, `evaluate_transitions()` NEVER called |
| vote_calibrator.py | PARTIALLY | On SystemContext, `calibrate()` NEVER called |
| gate_tuner.py | PARTIALLY | On SystemContext, `calibrate()` NEVER called |
| post_close_tracker.py | PARTIALLY | `record_close()` called, `tick()/process_pending()` NEVER called |
| execution_profiles.py | PARTIALLY | `record_outcome()` called, `select_profile()` NEVER called |
| capital_allocator.py | CONNECTED | `record_outcome()` called |
| behavior_discovery.py | CONNECTED | `record_trade()` called |
| outcome_feedback.py | CONNECTED | Used on SystemContext via brain/ |
| emitter_feedback.py | CONNECTED | Read-only service |
| interaction_discovery.py | PARTIALLY | On SystemContext, periodic compute NEVER driven |
| signal_discovery.py | PARTIALLY | On SystemContext, periodic compute NEVER driven |
| virtual_modules.py | CONNECTED | Scanner consumes it |
| virtual_promotion.py | PARTIALLY | `evaluate()` NEVER driven |
| regime_detector.py | PARTIALLY | `get_regime()` called, `update()` may not be fed data |
| score_optimizer.py | PARTIALLY | Weight loading works, `optimize()` NEVER called |
| pair_learner.py | PARTIALLY | PostCloseTracker wired, `learn()` NEVER called |
| regime_learner.py | PARTIALLY | `learn()` NEVER called |
| session_learner.py | PARTIALLY | `learn()` NEVER called |
| trade_analyzer.py | PARTIALLY | `analyze_all()` NEVER called |
| ev_estimator.py | DISCONNECTED | Utility, no lifecycle needed |
| win_rate_provider.py | DISCONNECTED | Utility, no lifecycle needed |
| param_evolution.py | DISCONNECTED | Only in TradingLoop |
| risk_manager.py | DISCONNECTED | Only in TradingLoop |
| tunable.py | CONNECTED | Protocol infrastructure |

### ml/ (8 files)
| File | Status | Notes |
|------|--------|-------|
| ALL 7 shims | DEAD CODE | Backward-compat re-exports from adaptive/ |
| __init__.py | DEAD CODE | Package init for dead shims |

### planning/ (4 files)
| File | Status | Notes |
|------|--------|-------|
| trade_planner.py | CONNECTED | Auto-calibration feedback loop works |
| outcome_logger.py | CONNECTED | Entry + close logging works |
| calibrator.py | CONNECTED | Auto-calibration works |
| models.py | CONNECTED | Data models |

### persistence/ (11 files)
| File | Status | Notes |
|------|--------|-------|
| event_store.py | CONNECTED | Core persistence backbone |
| domain_events.py | CONNECTED | Event type registry |
| event_sink.py | CONNECTED | loguru → EventStore bridge |
| broker_history.py | CONNECTED | Emits BROKER_HISTORY_IMPORT events |
| backfill.py | CONNECTED | Historical event backfill |
| position_store.py | PARTIALLY | No TRADE_OPEN/CLOSE events emitted |
| shadow_store.py | PARTIALLY | No events on mutations |
| shadow_resolver.py | PARTIALLY | No live event trail |
| deriv_position_store.py | DISCONNECTED | Pure SQLite, no events |
| atomic_write.py | DISCONNECTED | Pure utility |

### platforms/ (14 files)
| File | Status | Notes |
|------|--------|-------|
| platform_manager.py | CONNECTED | Shared by both systems |
| mt5_connector.py | CONNECTED | Broker API wrapper |
| deriv_connector.py | PARTIALLY | Uses DerivPositionStore, no domain events |
| main_loop.py | DEAD CODE | Old 8,535-line TradingLoop, NOT imported in production |
| trading_loop/exit_checks_mixin.py | DEAD CODE | Old management, superseded by PositionWorker |
| trading_loop/risk_heat_mixin.py | DEAD CODE | Old heat monitoring (GAP: not replicated) |
| trading_loop/recovery_mixin.py | DEAD CODE | Old recovery |
| trading_loop/shadow_live_mixin.py | DEAD CODE | Old shadow resolution (GAP: not replicated) |
| trading_loop/positions.py | DEAD CODE | ManagedPosition, used by tests only |
| candle_cache.py | DISCONNECTED | In-memory cache |
| circuit_breaker.py | DISCONNECTED | State machine utility |
| health_watchdog.py | DISCONNECTED | Freshness tracker |
| startup_check.py | DISCONNECTED | Pre-flight only |
| order_idempotency.py | DISCONNECTED | Pure utility |
| maintenance.py | DISCONNECTED | Daily cleanup utility |

### ops/ (4 files)
| File | Status | Notes |
|------|--------|-------|
| logging_config.py | CONNECTED | Structured logging |
| lifecycle.py | PARTIALLY | ShutdownManager/StartupRecovery used, no domain events |
| watchdog.py | PARTIALLY | Heartbeat works, no domain events |
| tick_profiler.py | DISCONNECTED | In-memory metrics only |

### dashboard/ (24 files)
| File | Status | Notes |
|------|--------|-------|
| api.py | CONNECTED | Safe — delegates to mixins |
| state.py | CONNECTED | ED-aware via set_event_driven_system() |
| state_status.py | CONNECTED | Full ED fallback path |
| state_trades.py | CONNECTED | Full ED fallback path |
| state_risk.py | CONNECTED | Full ED fallback path |
| state_health.py | CONNECTED | Full ED fallback path |
| state_scanner.py | CONNECTED | Full ED fallback path |
| state_decisions.py | CONNECTED | Data-driven (JSONL) |
| state_decision_trace.py | CONNECTED | Data-driven (EventStore) |
| state_events.py | CONNECTED | Data-driven (EventStore) |
| state_planner.py | CONNECTED | Data-driven (JSONL/JSON) |
| state_shadow.py | CONNECTED | Data-driven (SQLite) |
| state_controls.py | PARTIALLY | pause/resume/emergency → "no engine" |
| state_helpers.py | PARTIALLY | Journal cache empty in ED |
| state_history.py | PARTIALLY | Depends on journal cache |
| state_orchestrator.py | PARTIALLY | Config shows disabled |
| state_performance.py | PARTIALLY | System perf → empty |
| state_position_health.py | PARTIALLY | Config → defaults |
| state_brain.py | DISCONNECTED | Module votes → dark |
| state_governor.py | DISCONNECTED | Governor panel → defaults |
| state_learning.py | DISCONNECTED | ALL 16 sections → idle |
| state_ml.py | DISCONNECTED | ML insights → empty |
| state_module_governor.py | DISCONNECTED | Module gov → idle |
| state_operations.py | DISCONNECTED | Entire ops room → blank |

### Other files
| File | Status | Notes |
|------|--------|-------|
| main.py | CONNECTED | Clean ED-only startup |
| config.py | CONNECTED | Shared configuration |
| conftest.py | CONNECTED | Test infrastructure |
| platform_context.py | CONNECTED | Shared platform context |

---

## TOP 10 HIGHEST-PRIORITY FIXES

### 1. Register Tunable Adapters with TunerAgent
- **Impact:** CRITICAL — Re-enables ALL adaptive learning (31 tunables)
- **Effort:** Medium — Mirror `main_loop.py:_setup_tuner_agent()` in bootstrap
- **Files:** event_driven_bootstrap.py

### 2. Add Continuous Portfolio Heat Monitoring
- **Impact:** CRITICAL — Money at risk. Currently no protective action on open positions
- **Effort:** Medium — Add heat check to TickEvalLoop
- **Files:** event_driven_bootstrap.py

### 3. Wire DecisionEngine.decide_management() for Open Positions
- **Impact:** HIGH — Restores strategic thesis management
- **Effort:** Medium — Build SituationAssessment per position each eval cycle
- **Files:** event_driven_bootstrap.py

### 4. Fix 6 Disconnected Dashboard Mixins
- **Impact:** HIGH — Dashboard panels go from empty to functional
- **Effort:** Medium — Add ED-aware paths reading from SystemContext
- **Files:** dashboard/state_brain.py, state_governor.py, state_learning.py, state_ml.py, state_module_governor.py, state_operations.py

### 5. Wire PortfolioGovernor.check() for Entries
- **Impact:** HIGH — Currently only updates P&L on close, never gates entries
- **Effort:** Small — Add check() call before entry execution
- **Files:** event_driven_bootstrap.py

### 6. Wire RiskEngine.assess() for Entries
- **Impact:** HIGH — Full conviction/heat/drawdown assessment bypassed
- **Effort:** Medium — Replace execution-layer RiskGate with RiskEngine.assess()
- **Files:** event_driven_bootstrap.py

### 7. Wire Shadow Resolution for Rejected Setups
- **Impact:** MEDIUM — Counterfactual data for gate tuning
- **Effort:** Medium — Add shadow advancement to TickEvalLoop
- **Files:** event_driven_bootstrap.py

### 8. Wire ReEntryManager After Closes
- **Impact:** MEDIUM — Re-entry opportunities after valid exits
- **Effort:** Small — Add call in _on_trade_closed()
- **Files:** event_driven_bootstrap.py

### 9. Wire PostCloseTracker.tick() for Forward Checks
- **Impact:** MEDIUM — MFE/MAE forward price checks scheduled but never executed
- **Effort:** Small — Add periodic tick() call
- **Files:** event_driven_bootstrap.py

### 10. Wire ExecutionProfiles.select_profile() at Entry
- **Impact:** LOW — Dynamic per-trade parameter selection
- **Effort:** Small — Add call before sizing
- **Files:** event_driven_bootstrap.py

---

## DEAD CODE TO REMOVE (Future Cleanup)

| File/Dir | Lines | Why |
|----------|------:|-----|
| platforms/main_loop.py | 8,535 | Old TradingLoop, not imported in production |
| platforms/trading_loop/ (5 files) | ~2,500 | Old mixins, superseded |
| ml/ (8 files) | ~200 | Backward-compat shims |
| management/trade_manager.py | 809 | Superseded by PositionWorker |
| management/trailing_stop.py | ~150 | Superseded by PositionWorker |
| management/partial_close.py | ~100 | Superseded by PositionWorker |
| trigger/entry_validator.py | ~300 | Superseded by entry/entry_gate.py |
| **Total dead code** | **~12,600** | |

---

## CONCLUSION

The event-driven system has a **working execution spine** (tick → candle close → analysis → entry → management → close → risk feedback). The critical gaps are:

1. **Learning is dormant** — TunerAgent runs but has no adapters, so none of the 31 tunables ever adjust. Fix #1 alone would re-enable the entire adaptive layer.
2. **Heat monitoring is entry-only** — No protective action on open positions during drawdowns. Fix #2 is a safety issue.
3. **Management is mechanical only** — No strategic thesis tracking. Fix #3 restores the intelligence layer.
4. **Dashboard is half-blind** — 6 panels show nothing. Fix #4 is cosmetic but important for operator visibility.

Fixes #1-3 are the highest impact. #1 is the single most impactful change — one registration block re-enables an entire 31-component learning system that's currently collecting data but never processing it.
