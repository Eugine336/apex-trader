# APEX TRADER — Organizational Redesign: Department Map & Implementation Plan

> Source of truth: **raw code** at the current HEAD (read file-by-file, comments/READMEs ignored).
> Goal: restructure the event-driven trading organism into 8 accountable departments where
> **the intelligence layer decides — the system is market-driven, not zone-gated** — and the only
> hardcoded vetoes are the physically necessary ones (market closed, risk cap, spread, duplicate,
> circuit breaker / broker down).

---

## 0. Executive Findings (the 6 truths that drive the plan)

1. **The system is zone-gated, not market-driven.** No structural FVG/OrderBlock zone ⇒ no trade,
   ever. The hard gate is `entry/tick_entry_detector.py:77-79` (`if not zones: return None`) plus
   `entry/entry_gate.py:222-223` (`_check_zone_valid` hard-fails on `zone is None`). Entry direction
   comes from zone bias (`entry/zone_watcher.py` `_resolve_bias`), not from a consensus thesis.

2. **Consensus is a spectator.** The canonical voting engine `brain/directional_consensus.py:decide()`
   has **zero live callers** (only learning-replay + tests). Live votes (`wm.votes`) are reduced to a
   single advisory scalar `consensus_alignment` in `decision/situation.py:370` and can nudge, but
   **cannot trigger or flip** a trade. The ranked-EV path is gated OFF (`RankerConfig.use_ranker_ev=False`).

3. **Two analysts never reach consensus.** Volatility and Correlation produce no consensus vote;
   `inducement` is computed and stored but unvoted. 6/8 named analysts feed consensus.

4. **The "ultimate" risk gate is dead.** `risk/risk_engine.py:assess()` (the full
   drawdown→daily→correlation→EV→sizing→budget chain) is **never called live**. `_on_entry_decision`
   re-implements a looser ad-hoc gate sequence and sizes through a **fresh `PositionSizer()`** that
   bypasses the engine's protections. Three independent daily-loss accumulators exist
   (`PnLTracker`, `AccountRiskManager`, `PortfolioGovernor`) and can disagree.

5. **Compliance and Portfolio are scattered and intermixed.** Permit checks (binary) are tangled with
   sizing/exposure/EV (profitability) across `_on_entry_decision`, `entry_gate.py`, `risk_gate.py`,
   `account_risk.py`, `drawdown_guard.py`, `portfolio_governor.py`. One entry gate (5c: duplicate +
   spread + EV) is **fail-OPEN**.

6. **Broker boundary is leaky.** Code outside Execution touches MT5 directly:
   `brain/broker_autodiscovery.py` (live at startup), `trigger/entry_validator.py` (legacy),
   `risk/spread_bootstrap.py` (bypasses PlatformManager). A dual entry path
   (`APEX_ED_ENTRY_VIA_EXECUTOR`, default on) still allows a direct `pm.execute_entry` fallback that
   bypasses the RiskGate + circuit breaker. `DerivTickStream` (push) is fully dormant — the system polls.

---

## 1. Target Architecture (data flow)

```
MARKET (always streaming, never stops)
   │
   ▼
① INTELLIGENCE   analysts → evidence {direction, confidence, context}   (never trade/veto)
   │
   ▼
② CONSENSUS      evidence → thesis {direction, conviction}              (ACTIVE: the trigger)
   │
   ▼
③ COMPLIANCE     binary permit  APPROVED | REJECTED                     (only necessary vetoes)
   │
   ▼
④ PORTFOLIO      sizing/exposure/correlation/allocation                 ("wise given the book?")
   │
   ▼
⑤ EXECUTION      the ONLY layer touching broker APIs                    (orders/modify/close/retry)
   │
   ▼
⑥ OPERATIONS     manage open positions + continuous thesis re-validation
   │
   ▼
⑦ LEARNING       measure outcomes, attribution, calibration             (recommends only)
   │
   ▼
⑧ GOVERNANCE     authorizes Learning's adaptation; promotion lifecycle  (contains runaway adaptation)
```

`main.py` → `SystemContext.create()` (builds ~40 subsystems) → `EventDrivenSystem`
(`event_driven_bootstrap.py`, 5,336 lines — the wiring spine). Everything below maps into this spine.

---

## 2. Complete File → Department Map

Legend — **LIVE** = on the production decision path · **WRITE-ONLY** = computes/logs, nothing
consumes it · **DEAD** = no live caller · **SUPPORT** = infra/plumbing (not a department) ·
**OFF-PATH** = backtest/RL/scripts/dashboard (offline or observability).

### ① Intelligence Division (analysts → evidence)
| File | Status | Notes / boundary issues |
|---|---|---|
| `brain/structure_engine.py` | LIVE | Structure analyst (trend/BOS/CHoCH). Clean. |
| `brain/fvg_detector.py` | LIVE | Zone analyst (FVG). Clean evidence. |
| `brain/order_block.py` | LIVE | Zone analyst (OB). Clean. |
| `brain/liquidity_mapper.py` | LIVE | Liquidity analyst. Clean. |
| `brain/inducement_detector.py` | LIVE (stored) | Computed + stored on WM but **no vote extractor** consumes it. |
| `brain/volume_analyzer.py` | LIVE | Volume analyst. Clean. |
| `brain/wyckoff_engine.py` | LIVE | Profile-gated evidence. |
| `brain/currency_strength.py` | LIVE | Feeds consensus + high-authority opposition. |
| `brain/momentum_divergence.py` | LIVE | Momentum analyst (via `vote_from_momentum`). |
| `brain/session_vwap.py` | LIVE | Session/VWAP evidence (`vote_from_vwap`). |
| `brain/concept_modules.py` | LIVE | Live regime label source (trend/MR/vol regime). |
| `brain/world_model.py` | LIVE | Per-symbol frozen evidence snapshot. Single source of truth. |
| `brain/decision_core.py` | LIVE | `build_consensus` (the *actual* live consensus assembly) + `compute_bias`. |
| `brain/smoothing.py`, `brain/market_data_utils.py`, `brain/atr_percentile.py` | LIVE/SUPPORT | Shared math helpers. |
| `brain/volume_profile.py` | DEAD | POC penalty — tests only. |
| `brain/volatility_stop.py` | LIVE (entry only) | Used for entry SL sizing, **not** as a consensus vote. → Volatility analyst missing from consensus. |
| `brain/regime_detector.py` | DEAD (live path) | Only legacy `MTFOrchestrator` + tests. |
| `brain/setup_quality.py` | DEAD | OQ/EQ funcs — tests only; live OQ/EQ comes from ranker. |
| `brain/mtf_orchestrator.py` | DEAD/legacy | Superseded by `scanner/candle_close_handler.py`. |
| `brain/orchestrator.py` | LIVE | **Dept ④/⑤** — folds evidence into a size multiplier + physics vetoes. |
| `brain/opportunity_ranker.py` | LIVE (gated) | Produces `wm.candidates`; selection use gated by `use_ranker_ev=False`. |
| `brain/opportunity_density.py` | LIVE | **Dept ④** sizing input. |
| `brain/broker_autodiscovery.py` | LIVE | **Dept ⑤ violation** — imports `MetaTrader5` directly (`:150-192,540-550`). |
| `brain/session_engine.py` | LIVE | **Dept ③** — SessionEngine/NewsGuard (compliance gate), correctly separated. |
| `brain/correlation_engine.py` | LIVE | **Dept ③/④** — exposure, not a consensus vote. → Correlation analyst missing from consensus. |
| `brain/drawdown_guard.py` | LIVE | **Dept ③/④** — FROZEN permit + risk_pct sizing. |
| `brain/decision_trace.py` | SUPPORT | Decision tracing for dashboard. |

### ② Consensus Division (evidence → thesis)
| File | Status | Notes |
|---|---|---|
| `brain/directional_consensus.py` | **MIXED** | `Vote`/`VoteResult`/`vote_from_*` extractors LIVE; **`decide()` (weighted vote + high-authority veto) DEAD on live** — only learning-replay/tests. |
| `brain/decision_core.py:build_consensus` | LIVE | Builds the vote panel + candidates, stores on WM — but **does not decide direction**. |
| `brain/opportunity_ranker.py` | LIVE (advisory) | Ranked candidates feed dashboard + management OQ/EQ; selection gated off. |
| `decision/situation.py:_assess_consensus` | LIVE | Collapses full panel → scalar `consensus_alignment` (`:370`), keeps `consensus_components` alongside. |

### ③ Compliance Division (binary permit) — *currently scattered*
| File | Status | Notes |
|---|---|---|
| `event_driven_bootstrap.py:_on_entry_decision` | LIVE | Hosts Gates 0–5c inline (the de-facto compliance sequence). |
| `entry/entry_gate.py` | LIVE | 10 gates; **mixes** Compliance (market/session/spread/news/drawdown) with Consensus (`score_minimum`) + Portfolio (`risk_reward_ok`) + zone gate. |
| `execution/risk_gate.py` | LIVE | Last-mile execution validation (emergency DD≥20%, SL-direction, rate-limit). Correctly Dept ⑤-adjacent. |
| `risk/account_risk.py` | LIVE | Daily-loss-cap halt (permit) + per-account heat (portfolio). Mixed. |
| `governor/portfolio_governor.py` | LIVE | Daily-loss halt + max-positions (permit) + concentration (portfolio). Fail-closed. Own daily tally. |
| `brain/drawdown_guard.py` | LIVE | FROZEN permit + risk sizing. Mixed. |
| `risk/spread_monitor.py` | LIVE | Spread permit (gate 5c, **fail-OPEN**). |
| `brain/session_engine.py` (NewsGuard/SessionEngine) | LIVE | Market-open / news permit. |
| `risk/daily_tracker.py` (PnLTracker) | DEAD for gating | Only used inside dead `assess()`. Third daily-loss accumulator. |

### ④ Portfolio Division (sizing/exposure/allocation) — *currently fragmented, no cohesive layer*
| File | Status | Notes |
|---|---|---|
| `risk/position_sizer.py` | LIVE | Lot/stake math; fresh instance at `_on_entry_decision` (bypasses engine). |
| `risk/risk_engine.py` | **DEAD core** | `assess()` (full protective + sizing chain) never called live; object survives only as a parts-bag + `record_trade_result`. |
| `risk/portfolio_risk_state.py` | LIVE | NORMAL→DEFENSIVE→REDUCING→EMERGENCY ladder + flatten. |
| `risk/risk_accumulation.py` | LIVE | Graded-risk proximity math. |
| `risk/account_risk.py` | LIVE | Per-account heat/exposure silos. |
| `brain/correlation_engine.py` | LIVE | Count-proxy correlation; real returns-matrix lives in dead `adaptive/risk_manager.py`. |
| `adaptive/risk_manager.py` | DEAD | Full correlation matrix + `can_open_position` — not wired live. |
| `adaptive/capital_allocator.py` | LIVE | `get_sizing_multiplier` (one opaque factor, not a distinct allocation layer). |
| `brain/opportunity_density.py` | LIVE | Density → sizing input. |
| `risk/risk_reporter.py`, `risk/spread_bootstrap.py` | SUPPORT | Dashboard / startup spread seed (`spread_bootstrap` bypasses PlatformManager). |
| `scanner/rr_helper.py` | DEAD | Side-agnostic R:R — no live callers. |

### ⑤ Execution Division (sole broker gateway)
| File | Status | Notes |
|---|---|---|
| `execution/action_executor.py` | LIVE | Serialized, risk-gated bridge; dual circuit breakers (open vs manage). |
| `execution/intent_aggregator.py` | LIVE | Dedup/conflict-resolve intents per cycle. |
| `execution/intents.py` | LIVE | `Intent` model + factories. |
| `platforms/platform_manager.py` | LIVE | Routing + broker gateway (single `_broker_write_lock`). |
| `platforms/base_connector.py` | LIVE | ABC + result dataclasses. |
| `platforms/circuit_breaker.py` | LIVE | Used only by ActionExecutor (manage breaker shared across MT5+Deriv). |
| `platforms/order_idempotency.py` | LIVE | Deterministic idem keys (per-connector dedup). |
| `platforms/mt5/mt5_connector.py` | LIVE | Raw MT5 (allowed). |
| `platforms/deriv/deriv_connector.py` | LIVE | WS connector; reconnect serialized via `_connect_lock` (both paths). |
| `platforms/deriv/deriv_tick_stream.py` | DEAD/DORMANT | Push feed never constructed; system polls. |
| `persistence/deriv_position_store.py` | LIVE | Deriv contract metadata store. |
| `platforms/candle_cache.py` | LIVE | OHLCV TTL cache. |
| `tmp_deriv_test.py`, `tmp_deriv_test_harness.py` | OFF-PATH | Root-level harnesses calling `place_order` directly — delete. |

### ⑥ Operations Division (manage open positions)
| File | Status | Notes |
|---|---|---|
| `event_driven_bootstrap.py:PositionEvaluator` (271–980) | LIVE | The live manager: mechanical (`PositionWorker`) + strategic (`_run_decision_engine_management`). |
| `execution/position_worker.py` | LIVE | Stateless snapshot→intents (SL/TP1/BE/structure-trail/invalidation/conviction-collapse). |
| `execution/position_snapshot.py` | LIVE | Immutable snapshot. |
| `execution/management_state.py` | LIVE | SQLite mgmt state (BE/trail/partial flags). |
| `execution/management_scheduler.py` | LIVE | Due-symbol selection. |
| `management/trailing_stop.py` | LIVE | `StructureTrailingStop` (used by PositionWorker). |
| `decision/engine.py:decide_management` | LIVE | Strategic thesis re-validation → CLOSE/TIGHTEN/BE/SCALE_IN. |
| `management/trade_manager.py` | DEAD/legacy | Full mgmt FSM (TP2-adjust/TP3/structure-exit) — superseded by PositionWorker; logic hand-copied. |
| `management/partial_close.py` | DEAD | PositionWorker computes BE/partial inline. |
| `management/re_entry.py` | LIVE (lossy inputs) | Fed a synthetic stub; only clears cooldown, never re-enters. |
| `management/exit_cause.py` | DEAD | `ExitCause` enum never imported; live closes use free-text strings. |
| `management/opportunity_executor.py` | DEAD | Constructed but `execute()` never called (used only as truthiness gate). |

### ⑦ Learning Division (measure + recommend)
| File | Status | Notes |
|---|---|---|
| `adaptive/optimizer.py` (+`trade_analyzer`,`pair_learner`,`session_learner`,`regime_learner`,`score_optimizer`) | LIVE | Consumed at entry (`is_losing_pattern`, `get_trade_adjustments`/AVOID veto + size). **Mutates behavior directly.** |
| `adaptive/vote_calibrator.py` | LIVE | Re-weights live votes (`decision_core.py:364`). **Mutates directly.** |
| `adaptive/counterfactual.py` | LIVE | Marginal-R attribution feeding calibrator/governor. |
| `adaptive/emitter_feedback.py`, `adaptive/signal_ledger.py` | LIVE | Signal grading consumed by calibrator/governor. |
| `adaptive/win_rate_provider.py` | LIVE | Per-pair win prob → ranker. |
| `adaptive/ev_estimator.py` | LIVE | EV gate (also in dead `assess()`). |
| `adaptive/capital_allocator.py`, `adaptive/execution_profiles.py`, `adaptive/regime_detector.py`, `adaptive/zone_edge_tracker.py` | LIVE | Feed sizing/EV/profile. |
| `adaptive/post_close_tracker.py` | LIVE | Post-close MFE/MAE → pair_learner. |
| `brain/outcome_feedback.py` | WRITE-ONLY | `module_accuracy` → dashboard only. |
| `adaptive/interaction_discovery.py` | WRITE-ONLY | Toxic/protective pairs → dashboard; **not** consumed by ModuleGovernor. |
| `adaptive/behavior_discovery.py` | WRITE-ONLY | Cluster labels → dashboard only. |
| `adaptive/signal_discovery.py` | WRITE-ONLY | Feeds only the (dead) virtual-promotion loop. |
| `adaptive/param_evolution.py` | DEAD | Never instantiated. |
| `brain/trade_journal.py` | LIVE | Persistent closed-trade store backing the learner pipeline. |

### ⑧ Governance Division (authorize + contain)
| File | Status | Notes |
|---|---|---|
| `adaptive/module_governor.py` | LIVE | The **only** real authorization gate — suppresses SHADOW/DISABLED modules from the vote panel (`decision_core.py:352`). |
| `adaptive/tuner_agent.py` (+`tunable.py`,`tunable_adapters.py`) | LIVE | Containment: central scheduling, sole-authority enforcement, validate/rollback/audit of tuning. |
| `adaptive/virtual_modules.py`, `adaptive/virtual_promotion.py` | WRITE-ONLY | Promotion lifecycle runs but `compute_votes` has no live consumer + registry defaults disabled → promotes into a void. |

### Cross-cutting glue
| File | Status | Dept | Notes |
|---|---|---|---|
| `decision/engine.py` | LIVE | ②→③/④ | `decide_entry` gates **and** sizes (leak); ignores `regime`/`horizon`/`oq`/`eq`/`ev` (not passed in `EntryContext`); config DE weights never wired. |
| `decision/situation.py` | LIVE | ①/② | Vectors populated; collapses panel→scalar alignment. |
| `decision/context.py`, `decision/actions.py`, `decision/journal.py` | LIVE | — | `Action.SCALE_IN`/`PARTIAL_CLOSE` enum branches never produced by DecisionEngine. |
| `decision/governor.py` | LIVE | ④ | RiskGovernor post-DE review. |
| `planning/trade_planner.py` | LIVE (degraded) | ④/⑤ | Full plan computed then discarded; only SKIP/WAIT honored. |
| `planning/calibrator.py`, `planning/outcome_logger.py`, `planning/models.py` | LIVE | ⑦ | Planner outcome learning. |
| `scanner/candle_close_handler.py` | LIVE | ①/② | Sole live analysis path (candle close → modules → WM → consensus → `world_model_update`). |
| `tick/*` (`tick_router`,`candle_close_detector`,`event_bus`,`tick_store`,`models`) | LIVE | SUPPORT | Event backbone. |
| `persistence/event_store.py`,`domain_events.py`,`event_sink.py`,`event_replayer.py`,`recovery.py`,`position_store.py`,`shadow_store.py`,`atomic_write.py` | LIVE | SUPPORT | Persistence backbone. |
| `persistence/shadow_resolver.py`,`broker_history.py`,`backfill.py` | DEAD | — | Tests-only. |
| `ops/lifecycle.py`,`ops/watchdog.py`,`ops/logging_config.py` | LIVE | SUPPORT | Shutdown/heartbeat/structured logging. |
| `ops/tick_profiler.py` | DEAD | — | Tests-only. |
| `trigger/entry_engine.py` | LIVE (helper only) | — | `calculate_entry` DEAD (backtest only); live uses only `calculate_stop_loss`/`calculate_targets`. |
| `trigger/entry_patterns.py`,`trigger/entry_validator.py` | DEAD | — | `entry_validator` also a **Dept ⑤ violation** (direct MT5). |
| `rl/*` | OFF-PATH | — | Offline training; live bridge dormant (untrained checkpoint). |
| `backtest/*`, `brain/backtest_engine.py`, `brain/swap_model.py` | OFF-PATH | — | Offline by design. |
| `dashboard/*` | SUPPORT | — | Observability. |
| `ml/*` | DEAD | — | Back-compat shim re-exporting `adaptive.*`; tests-only. |
| `scripts/*`, `fetch_*.py`, `discover_icmarkets.py`, `train_colab.py`, `run_training.py` | OFF-PATH | — | CLI/offline tools (some touch MT5 directly). |

---

## 3. Boundary Violations (must-fix register)

| # | Violation | Location | Correct dept | Severity |
|---|---|---|---|---|
| V1 | Zone REQUIRED before any trade considered (zone gatekeeper) | `entry/tick_entry_detector.py:77-79`, `entry/entry_gate.py:222-223` | ② must trigger, Zone is ① evidence | **Critical (core redesign)** |
| V2 | Consensus engine `decide()` never consulted; entry direction from zone bias | `directional_consensus.py:107` (dead), `decision_core.py:114-119` | ② | **Critical** |
| V3 | Intelligence reduced to scalar before decision | `decision_core.py:132`, `situation.py:370`, `orchestrator.py:612` | ①/② keep structure | High |
| V4 | `RiskEngine.assess()` (full protective chain) dead; live sizing bypasses it | `risk_engine.py:125-441` vs `_on_entry_decision:4488` | ③/④ | High |
| V5 | Compliance mixes binary-permit with sizing + EV/profitability | `entry_gate.py`, `risk_engine.py:249`, gate 5c | ③ must be pure permit | High |
| V6 | One fail-OPEN entry gate (duplicate+spread+EV) | `_on_entry_decision:3850-3939` | ③ fail-closed | High |
| V7 | Three independent daily-loss accumulators | `PnLTracker`, `AccountRiskManager`, `PortfolioGovernor` | ③ single source | Medium |
| V8 | Direct MT5 outside Execution (live at startup) | `brain/broker_autodiscovery.py:150-192,540-550` | ⑤ | High |
| V9 | Direct MT5 in legacy validator | `trigger/entry_validator.py:180-181` | ⑤ | Medium (dead but latent) |
| V10 | `spread_bootstrap` calls connectors directly, bypasses PlatformManager | `risk/spread_bootstrap.py:47-93` | ⑤ | Medium |
| V11 | Dual entry path — direct `pm.execute_entry` fallback bypasses RiskGate+breaker | `_on_entry_decision:4585-4624` | ⑤ | High |
| V12 | Manage circuit breaker shared across MT5+Deriv | `action_executor.py:171-191` | ⑤ per-platform | Medium |
| V13 | `Action.SCALE_IN`/`PARTIAL_CLOSE` verdicts journaled then dropped (no handler branch) | `decision/engine.py` / `_run_decision_engine_management` | ⑥ | Medium |
| V14 | `ExitCause` enum unused; free-text exit strings feed learners | `management/exit_cause.py` vs `_on_trade_closed:5035` | ⑥→⑦ | Medium |
| V15 | Learning mutates live behavior directly (no Governance authorization) | VoteCalibrator/optimizer/capital_allocator/exec_profiles | ⑦ recommends, ⑧ authorizes | High |
| V16 | Virtual promotion promotes into a void (`compute_votes` unconsumed, registry disabled) | `virtual_promotion.py` + `virtual_modules.py` | ⑧ | Medium |
| V17 | InteractionAnalyzer toxic pairs computed but ignored | `interaction_discovery.py` → no consumer | ⑦→⑧/④ | Low |
| V18 | DecisionEngine config weights + intelligence inputs (regime/horizon/oq/eq/ev) never wired into `EntryContext` | `system_context.py:268-280`, `_on_entry_decision:4011-4042` | ②/④ | Medium |

---

## 4. Per-Department Gap Analysis (exists / wrong-boundary / missing)

**① Intelligence** — *Exists:* 6 analysts producing clean evidence. *Wrong boundary:* `broker_autodiscovery` direct MT5 (V8). *Missing:* Volatility analyst vote, Correlation analyst vote, Inducement vote extractor; a single uniform evidence contract `{direction, confidence, context, metadata}` enforced across all analysts.

**② Consensus** — *Exists:* vote extractors, `build_consensus` panel, ranker. *Wrong boundary:* output is advisory-only. *Missing:* an **active** consensus that emits a thesis (direction+conviction) capable of **triggering** an entry without a zone; unification of `build_consensus` (live, no decision) vs `decide()`/ranker (decision, dead/gated); structured (non-scalar) thesis carried forward.

**③ Compliance** — *Exists:* every necessary permit (FROZEN, daily-loss, market-open, spread, duplicate, max-positions, circuit-breaker). *Wrong boundary:* permits intermixed with sizing + EV; one fail-open gate; 3 daily-loss tallies. *Missing:* a single pure `permit() → APPROVED|REJECTED` layer with the **only** vetoes being market-closed, risk-cap/daily-loss, spread, duplicate, circuit-breaker/broker-down — no profitability opinion.

**④ Portfolio** — *Exists:* sizer, density, account heat, correlation (count-proxy), state ladder. *Wrong boundary:* sizing scattered across an 8-factor opaque `combined_mult`; `assess()` dead. *Missing:* a cohesive `Portfolio.evaluate(book, candidate) → {approved_size, exposure_verdict}`; first-class capital-weighted currency/asset/**broker** exposure budgets; revive the real returns-correlation matrix.

**⑤ Execution** — *Exists:* clean executor + aggregator + idempotency + serialized broker lock + Deriv reconnect lock. *Wrong boundary:* direct-broker callers (V8–V10), dual entry path (V11), shared manage breaker (V12). *Missing:* enforcement that Execution is the *sole* gateway (private/guarded mutators); per-(platform×op) breakers; decide push vs poll for `DerivTickStream`; centralized in-flight ledger.

**⑥ Operations** — *Exists:* mechanical + strategic management, structure trailing, BE, TP1 partial, scale-in, thesis re-validation (DE every 10s + mechanical proxies). *Wrong boundary:* logic duplicated in dead TradeManager. *Missing:* scale-OUT wiring (V13), `ExitCause` adoption (V14), real re-entry inputs + actual re-entry, TP2 dynamic adjust / non-structure trail fallback.

**⑦ Learning** — *Exists:* closed-trade pipeline wired (`set_trade_history_provider`→TradeJournal), calibrator, attribution. *Wrong boundary:* mutates behavior directly (V15). *Missing:* a clean "recommend-only" contract; consumers for `interaction_discovery`/`behavior_discovery`/`outcome_feedback` (currently write-only); remove/wire `param_evolution`.

**⑧ Governance** — *Exists:* ModuleGovernor (panel suppression) + TunerAgent (containment). *Wrong boundary:* most Learning bypasses it. *Missing:* a real authorization layer that gates **all** Learning recommendations before they change behavior; unified promotion lifecycle (real + virtual modules); close or delete the virtual-promotion void (V16); consume toxic-pair recommendations (V17).

---

## 5. Implementation Plan — One Session Per Department

> **Data-flow order** is ①→⑧. **Implementation order differs** because of dependencies: the broker
> gateway must be clean first; the Compliance/Portfolio safety floor must be solid *before* the zone
> gate is removed (Consensus going active raises entry frequency — Compliance becomes the only
> throttle); evidence producers must emit a structured contract before Consensus can consume it.
> Each phase is **one coding session, end-to-end for that department**, with its own tests, and
> leaves the system green and shippable.

### Phase 0 — Execution Division (foundation: sole broker gateway)
**Why first:** every later phase routes orders through Execution; close the leaks before building on top.
- Fix V8/V9/V10: move MT5 symbol-discovery + market-hours + spread sampling behind a connector/PlatformManager capability; remove direct `MetaTrader5`/connector calls from `brain/broker_autodiscovery.py`, `trigger/entry_validator.py`, `risk/spread_bootstrap.py`.
- Fix V11: remove the direct `pm.execute_entry` fallback; **all** OPENs go through `ActionExecutor` (RiskGate + breaker).
- Fix V12: split the manage circuit breaker per `(platform × op-class)`.
- Decide `DerivTickStream`: wire push end-to-end **or** delete it; delete `tmp_deriv_test*.py`.
- Make PlatformManager mutators executor-only (guard/private).
- **Files:** `platforms/*`, `execution/action_executor.py`, `brain/broker_autodiscovery.py`, `risk/spread_bootstrap.py`, `trigger/entry_validator.py` (delete), bootstrap entry dispatch.

### Phase 1 — Compliance Division (the safety floor that replaces zone throttling)
**Why second:** removing the zone gate later means Compliance is the *only* thing preventing overtrading; it must be a single, hardened, pure-permit layer first.
- Build one `ComplianceDivision.permit(candidate, book, account) → APPROVED|REJECTED(reason)`.
- Move into it (binary only): market-open/broker-available, circuit-breaker/connection, spread acceptable, duplicate position, DrawdownGuard FROZEN, daily-loss halt, max-positions, PortfolioRiskSM DEFENSIVE+.
- **Remove profitability from Compliance** (EV veto leaves — it belongs to ④/⑦).
- Fix V6 (fail-closed) and V7 (consolidate the 3 daily-loss accumulators to one).
- Leave hooks where ④ Portfolio will own sizing/exposure.
- **Files:** new `compliance/` module; refactor `_on_entry_decision` gates, `entry/entry_gate.py`, `risk/account_risk.py`, `governor/portfolio_governor.py`, `brain/drawdown_guard.py`, `risk/spread_monitor.py`, `brain/session_engine.py`.

### Phase 2 — Portfolio Division (capital allocation & exposure as a distinct layer)
**Why third:** entry-distribution changes when zones go; sizing/exposure must be book-aware and cohesive, not an opaque multiplier.
- Build `PortfolioDivision.evaluate(book, candidate) → {approved_size, exposure_verdict, rationale}`.
- Consolidate sizing (replace the fresh `PositionSizer()` bypass; fold the real protective chain from dead `assess()`); capital-weighted currency/asset/**broker** exposure budgets; revive the real returns-correlation matrix (`adaptive/risk_manager.py`) replacing count-proxies.
- Make capital allocation first-class (not a `cap_mult` factor).
- Fix V4 (sizing no longer bypasses protections); remove dead `assess()` once parity reached.
- **Files:** new `portfolio/` module; `risk/position_sizer.py`, `risk/risk_engine.py`, `brain/correlation_engine.py`, `adaptive/risk_manager.py`, `adaptive/capital_allocator.py`, bootstrap sizing path.

### Phase 3 — Intelligence Division (uniform structured evidence + complete analyst roster)
**Why fourth:** Consensus (next) needs every analyst emitting the same structured contract, including the two missing ones.
- Enforce one evidence contract `{direction, confidence, context, metadata}` across all analysts.
- Add the **Volatility analyst** vote and **Correlation analyst** vote into `build_consensus`; add the **inducement** vote extractor.
- Fix V3 at the source — preserve per-module structure (no premature scalar collapse); keep `compute_bias` as one input among many, not the direction oracle.
- **Files:** `brain/*` analysts, `brain/directional_consensus.py` (extractors), `brain/decision_core.py:build_consensus`, `scanner/candle_close_handler.py`.

### Phase 4 — Consensus Division (the central shift: ACTIVE, market-driven trigger)
**Why fifth:** this is the user's core requirement; safe to flip now that Execution + Compliance + Portfolio + structured Intelligence are ready.
- Make Consensus emit a **thesis** (direction + conviction + structured support/oppose) every analysis cycle.
- Add a consensus-driven trigger path so a sufficiently-convicted thesis **initiates an entry independent of any zone**; **demote Zone to one analyst** (remove the hard gate V1/V2: `tick_entry_detector.py:77-79`, `entry_gate.py:222-223`).
- Unify `build_consensus` vs `decide()`/ranker into one authoritative engine; SL/TP from ATR (`trigger/entry_engine.calculate_stop_loss/targets`) when no zone; conviction from consensus, not `zone.conviction`.
- Wire V18 inputs (regime/horizon/oq/eq/ev + DE config weights) into `EntryContext`/DecisionEngine; conviction threshold is the trigger — **no hardcoded frequency governor** (Learning tightens conviction organically via outcomes).
- Keep the necessary vetoes only (delegated to ③ Compliance).
- **Files:** `brain/directional_consensus.py`, `brain/decision_core.py`, `decision/situation.py`, `decision/engine.py`, `decision/context.py`, `entry/*` (orchestrator/tick_detector/gate), `event_driven_bootstrap.py:_on_entry_decision`, `core/system_context.py`.

### Phase 5 — Operations Division (manage what the new trigger opens)
- Wire scale-OUT (V13: add `PARTIAL_CLOSE` branch mirroring SCALE_IN).
- Adopt `ExitCause` enum end-to-end (V14) so learners get categorical exit features.
- Real re-entry inputs + actually re-enter (not just clear cooldown); broaden trigger beyond the breakeven heuristic.
- Port TP2 dynamic-adjust / add non-structure (ATR) trailing fallback; then delete dead `TradeManager`/`PartialCloseCalculator` to end the duplication.
- **Files:** `event_driven_bootstrap.py:PositionEvaluator/_run_decision_engine_management/_on_trade_closed`, `execution/position_worker.py`, `management/exit_cause.py`, `management/re_entry.py`, delete `management/trade_manager.py`+`partial_close.py`.

### Phase 6 — Learning Division (measure + recommend, never mutate)
- Convert every Learning output to a **recommendation** object routed through Governance (Phase 7) rather than applied directly (V15): VoteCalibrator weights, optimizer veto/size, capital allocator, execution profiles.
- Give `interaction_discovery`/`behavior_discovery`/`outcome_feedback` real consumers or label analytics-only; wire or delete `param_evolution`.
- **Files:** `adaptive/*` (learners + feedback), `brain/outcome_feedback.py`.

### Phase 7 — Governance Division (authorize + contain)
- Build a single authorization layer: Learning recommends → Governance authorizes → behavior changes.
- Unify the promotion lifecycle (real modules via ModuleGovernor + virtual via VirtualSignalManager) into one Shadow→Validation→Limited→Full machine; close the virtual void (V16: consume `compute_votes`, enable registry) or remove it.
- Consume InteractionAnalyzer toxic/protective pairs (V17) to contain correlated/overfit module sets; keep TunerAgent as the containment/rollback substrate.
- **Files:** `adaptive/module_governor.py`, `adaptive/virtual_modules.py`, `adaptive/virtual_promotion.py`, `adaptive/tuner_agent.py`, `adaptive/interaction_discovery.py`, `core/system_context.py`.

### Final — Cleanup pass (after departments stand)
- Delete confirmed-dead, parity-replaced code: `ml/*` shim, `persistence/{shadow_resolver,broker_history,backfill}.py`, `ops/tick_profiler.py`, `scanner/rr_helper.py`, `brain/{volume_profile,setup_quality,mtf_orchestrator,regime_detector}.py` (verify zero live imports at that HEAD), `trigger/entry_patterns.py`, dead `management/*`.

---

## 6. Dependency Graph (why this order)

```
Phase 0 Execution ─────────────┐ (clean gateway underpins all order flow)
Phase 1 Compliance ───────┐    │ (safety floor must precede removing the zone throttle)
Phase 2 Portfolio ──────┐ │    │ (book-aware sizing before frequency rises)
Phase 3 Intelligence ─┐ │ │    │ (structured evidence before consensus consumes it)
Phase 4 Consensus  ◄──┴─┴─┴────┘ (ACTIVE trigger — safe once 0–3 are in place)
Phase 5 Operations ◄── manages what 4 opens
Phase 6 Learning   ◄── measures what 5 closes
Phase 7 Governance ◄── authorizes what 6 recommends
```

**Bottom line:** the architecture (data flow) is ①→⑧; the build order is 0,1,2,3,4,5,6,7. The single
most important change — making the **intelligence/consensus the trigger instead of the zone** — lands
in Phase 4, only after the broker gateway (0), the safety floor (1), book-aware sizing (2), and a
complete structured analyst roster (3) are in place, so the system can be trusted to decide while the
only hardcoded vetoes remain the physically necessary ones.
