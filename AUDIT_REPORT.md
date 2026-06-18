# APEX TRADER — COMPREHENSIVE CODE AUDIT

**Audit Date:** 2026-06-18
**Auditor:** Bits Code (automated raw-code analysis)
**Scope:** Every source file in the repository. README/docs/comments NOT trusted — findings from code execution paths ONLY.
**Verdict:** Apex is a real, deeply-engineered, institutional-philosophy algorithmic trading system. It is not a toy, not a wrapper, not a demo. It is also not production-safe in several critical dimensions documented below.

---

## PHASE 1 — FILE INVENTORY

**Total source files (excluding node_modules, .ddagent):** ~250+
**Languages:** Python (backend, ~95%), JavaScript/JSX (dashboard frontend, ~5%)
**Lines of code:** ~45,000+ Python, ~5,000+ JSX

### Directory Map

| Directory | Files | Purpose |
|---|---|---|
| `brain/` | 33 | Market-reading modules: structure, FVG, order blocks, liquidity, Wyckoff, regime, session, currency strength, volume, momentum, correlation, orchestrator |
| `adaptive/` | 29 | Self-learning layer: score optimizer, regime learner, pair learner, session learner, counterfactual attribution, interaction discovery, signal discovery, virtual modules, vote calibrator, module governor, capital allocator, execution profiles, behavior discovery, tuner agent |
| `platforms/` | 20 | Broker connectors (MT5, Deriv WebSocket), trading loop, recovery, risk heat, exit checks, shadow live, circuit breaker, candle cache, platform manager |
| `scanner/` | 5 | Pair scanner (1,835 lines), ranker, R:R helper, scan scheduler |
| `trigger/` | 4 | Entry engine (1,613 lines), M1 pattern detector, entry validator |
| `management/` | 7 | Trade manager (809 lines), trailing stop, partial close, re-entry, exit cause taxonomy, opportunity executor |
| `decision/` | 7 | Decision engine (1,166 lines), situation engine, governor, journal, context, actions |
| `risk/` | 10 | Risk engine (780 lines), position sizer, portfolio risk state machine, account risk, daily tracker, spread monitor, risk accumulation, risk reporter |
| `persistence/` | 10 | Event store, position store, shadow store, atomic write, broker history, domain events |
| `rl/` | 14 | Reinforcement learning: Gym environments, PPO trainer, actor-critic network, authority progression, shadow trading, bridge to scanner |
| `governor/` | 3 | Portfolio governor: position caps, currency/sector concentration |
| `planning/` | 5 | Trade planner, calibrator, outcome logger |
| `ml/` | 8 | Backward-compatibility shims → `adaptive/` |
| `ops/` | 4 | Lifecycle (shutdown/recovery), structured logging, watchdog |
| `dashboard/` | 35+ | FastAPI REST/WebSocket backend + React/Vite frontend |
| `backtest/` | 10 | Simulated broker, historical data, strategy base class, results |
| `config/` | 3 | Broker JSON configs (Deriv, ICMarkets, MetaQuotes) |
| `scripts/` | 12 | Utilities: symbol discovery, data fetch, backup/restore, smoke test, RL validation |
| `bootstrap/` | 2 | Datadog APM initialization |
| Root files | ~15 | main.py, config.py (~2,850 lines), conftest.py, Dockerfile, docker-compose.yml, pyproject.toml, etc. |

---

## PHASE 2 — WHO IS APEX?

### Identity

**Apex is an autonomous multi-platform algorithmic trading system** that trades forex (28 pairs), commodities (4), indices (10), crypto (8), and Deriv synthetic instruments (15) — 65 instruments total across two broker platforms simultaneously.

It connects to:
1. **MetaTrader 5** (Windows IPC via the MetaTrader5 Python package) — for forex, commodities, indices, crypto CFDs
2. **Deriv** (WebSocket + OAuth2/OTP REST API) — for synthetic volatility indices, boom/crash, step index, range break

### What It Trades

Every instrument in the `INSTRUMENT_REGISTRY` (config.py lines 54–171). The system scans all enabled pairs continuously and trades whatever passes its multi-gate pipeline. It does NOT pick a single strategy per instrument — it runs the same unified brain across everything, with per-category parameter profiles.

### How It Trades — The Strategy

Apex implements **ICT-style institutional order flow analysis** combined with **multi-timeframe confluence scoring** and **machine-learning adaptive optimization**. The core logic:

1. **Structure Engine** — Identifies swing highs/lows, labels them (HH/HL/LH/LL), determines market structure trend (BULLISH/BEARISH/RANGING), detects BOS (Break of Structure) and CHoCH (Change of Character) on closed bars only
2. **Fair Value Gaps (FVG)** — Detects price imbalances; tracks OPEN/PARTIALLY/FILLED/MITIGATED status
3. **Order Blocks (OB)** — Identifies the last opposing candle before a strong impulse; tracks FRESH/TESTED/MITIGATED/BROKEN status; detects breaker blocks
4. **Liquidity Mapping** — Finds equal highs/lows, swing-based stop clusters; predicts which side gets swept next; detects live sweeps and classifies reactions
5. **Wyckoff Engine** — Classifies accumulation/distribution phases; detects springs (LONG) and upthrusts (SHORT)
6. **Currency Strength** — RSI-based cross-pair momentum ranking of 8 major currencies
7. **Volume Analysis** — Spike detection, divergence, climax candles, Point of Control estimation
8. **Momentum Divergence** — RSI/MACD divergence detection on M5 and H1
9. **Session VWAP** — Intraday VWAP penalty for wrong-side entries

These 9 modules each cast **weighted signed votes** (LONG/SHORT/NEUTRAL with confidence) into a **directional consensus** system. The consensus produces ranked **opportunity clusters** (SCALP/SWING) with Expected Value in R-units.

### The Full Execution Path (Entry Point → First Trade)

```
main.py
  └→ AppConfig() loads all 30+ config dataclasses
  └→ Broker auto-discovery (symbol mapping)
  └→ Phase 1-8 module loading
  └→ TradingLoop(config)
      └→ PlatformManager.connect_all() (MT5 + Deriv)
      └→ spread_bootstrap() — patches live spreads into registry
      └→ StartupCheck.run_all() — imports, config, registry, DB, disk
      └→ _perform_startup_recovery() — restore persisted positions, reconcile
      └→ MAIN LOOP: while running:
          └→ _run_supervised_cycle()
              └→ run_once()
                  ├→ SCAN PHASE:
                  │   └→ PlatformManager.fetch_all_market_data() [parallel, 10 threads]
                  │   └→ PairScanner.scan_all() → PairScanResult per instrument
                  │       └→ Per pair: StructureEngine + FVG + OB + Liquidity + Wyckoff
                  │                   + CurrencyStrength + Volume + Momentum + VWAP
                  │       └→ DirectionalConsensus.decide_opportunities() → ranked clusters
                  │       └→ OpportunityQuality + EntryQuality scoring
                  │       └→ Status: READY / WATCHLIST / WAITING / MARKET_CLOSED
                  │
                  ├→ RANK PHASE:
                  │   └→ PairRanker.rank() → sorted by composite priority
                  │   └→ OpportunityExecutor.select() → best candidate(s)
                  │
                  ├→ TRIGGER PHASE (for each READY candidate):
                  │   └→ DecisionEngine.decide_entry() → ENTER/SKIP with conviction
                  │   └→ TradePlanner.plan() → TradePlan with SL/TP/sizing/reasoning
                  │   └→ RiskGovernor.review_entry() → approve/veto/dim
                  │   └→ EntryEngine.calculate_entry() → exact entry/SL/TP1/TP2/lots
                  │   └→ EntryValidator.validate() → final safety gate
                  │   └→ Orchestrator.evaluate_proposal() → graded size multiplier
                  │   └→ RiskEngine.assess() → drawdown/daily/correlation/sizing
                  │   └→ PlatformManager.place_order() → broker execution
                  │   └→ ShadowStore.create_contract() for rejected setups
                  │
                  ├→ MANAGE PHASE (for each open position):
                  │   └→ SituationEngine.assess_open_trade() → continuous dimensions
                  │   └→ DecisionEngine.decide_management() → HOLD/CLOSE/TIGHTEN/etc
                  │   └→ RiskGovernor.review() → validate/override
                  │   └→ Orchestrator.evaluate_open_position() → health score + action
                  │   └→ TradeManager.update() → SL/TP/breakeven/trailing/partial/exit
                  │   └→ Exit checks: invalidation, conviction collapse, HTF candle,
                  │     dynamic SL, absolute profit, session close, spread, opportunity cost
                  │
                  ├→ RISK PHASE:
                  │   └→ Portfolio heat check → state machine (NORMAL→DEFENSIVE→REDUCING→EMERGENCY)
                  │   └→ Margin guardian → flatten if margin < threshold
                  │   └→ DrawdownGuard state update
                  │
                  └→ LEARN PHASE (on trade close):
                      └→ TradeJournal.record()
                      └→ OutcomeFeedback.complete()
                      └→ SignalLedger.grade_pending()
                      └→ TunerAgent.on_trade_close() → runs due learners
                      └→ AdaptiveOptimizer.run_optimization() (if not via agent)
                      └→ CounterfactualEngine.compute_attributions()
```

### The Decision Logic: Signal → Execution

The system does NOT use simple indicator crossovers. It uses:

1. **9-module weighted voting** with configurable per-module weights (default all 1.0)
2. **Opportunity clustering** — groups votes into SCALP/SWING clusters by timeframe
3. **Expected Value** — EV = p_win × R:R − (1 − p_win), with adaptive win-rate provider
4. **Two-stage quality gate** — Opportunity Quality (direction-free conditions) + Entry Quality (direction-aware location)
5. **7-confluence scoring** — structure, FVG, OB, liquidity, sweep, currency strength, session (0–123 scale, threshold 85 for READY)
6. **Decision Engine** — continuous weighted scoring of ENTER vs SKIP, with conviction-based sizing
7. **Orchestrator Round Table** — every evidence dimension folds into a bounded [0.15, 1.0] size multiplier. Weak dimensions SIZE DOWN; they never kill. Only physics vetoes (margin, market closed, duplicate, below min lot).
8. **Governor** — independent safety layer with veto authority
9. **Risk Engine** — drawdown guard, daily/weekly caps, correlation, position sizing, spread check

**Key design philosophy (from code):** "Not 'should I trade this?' (yes/no) → but 'how big should I trade this?'"

---

## PHASE 3 — EXECUTION PATH TRACING

### Component Dependency Map

```
                    ┌──────────────┐
                    │   main.py    │
                    │ (entry point)│
                    └──────┬───────┘
                           │
                    ┌──────▼───────┐
                    │ TradingLoop  │ (platforms/main_loop.py — 2000+ lines)
                    │  .run_once() │
                    └──────┬───────┘
                           │
          ┌────────────────┼────────────────┐
          ▼                ▼                ▼
   ┌─────────────┐  ┌──────────┐   ┌──────────────┐
   │ PairScanner │  │  Manage  │   │  Risk/Heat   │
   │  (scan)     │  │  (open   │   │  (portfolio) │
   │             │  │   trades)│   │              │
   └──────┬──────┘  └────┬─────┘   └──────┬───────┘
          │               │                │
   ┌──────▼──────┐  ┌────▼─────┐   ┌──────▼───────┐
   │  9 Brain    │  │ Decision │   │ State Machine│
   │  Modules    │  │ Engine   │   │ NORMAL→EMERG │
   │  (voting)   │  │ Situation│   │              │
   └──────┬──────┘  └────┬─────┘   └──────────────┘
          │               │
   ┌──────▼──────┐  ┌────▼─────┐
   │ Consensus   │  │ Trade    │
   │ Ranking     │  │ Manager  │
   │ Opportunity │  │ SL/TP/BE │
   └──────┬──────┘  └──────────┘
          │
   ┌──────▼──────┐
   │ Entry Engine│ → EntryValidator → RiskEngine → Broker
   │ Planner     │
   │ Governor    │
   │ Orchestrator│
   └─────────────┘

   LEARNING (background):
   TradeJournal ──→ AdaptiveOptimizer ──→ ScoreOptimizer
                                     ──→ RegimeLearner
                                     ──→ PairLearner
                                     ──→ SessionLearner
   CounterfactualEngine ──→ InteractionDiscovery
                       ──→ SignalDiscovery ──→ VirtualModules
   SignalLedger ──→ EmitterFeedback ──→ VoteCalibrator
                                    ──→ ModuleGovernor
   TunerAgent (coordinates all ^)
```

### What Happens When Things Fail

| Failure | What Happens | Dangerous? |
|---|---|---|
| **MT5 disconnects** | PlatformManager.reconnect_platform() with exponential backoff (5→60s). Trading continues on Deriv. Recovery reconciliation on reconnect. | ✅ Handled |
| **Deriv WS drops** | Exponential backoff reconnect (up to 10 attempts). In-memory position state LOST on crash — no persistence for Deriv positions. | 🔴 DANGEROUS |
| **Deriv token expires** | Logs CRITICAL. No auto-refresh mechanism. System halts until manual token rotation. | 🔴 DANGEROUS |
| **Price is stale (>120s)** | MT5Connector raises RuntimeError. Scanner skips that pair for the cycle. | ✅ Handled |
| **Price is NaN/Inf** | EntryValidator.check_price_finiteness() catches NaN/Inf. Does NOT catch zero or negative. | ⚠️ Partial |
| **SL/TP on wrong side** | _validate_stop_target_sidedness() rejects. Also EntryEngine has sanity fixes that force-reset wrong-side TPs. | ✅ Handled |
| **Balance is zero** | Python falsy check (`if balance`) treats 0.0 as False → silently drops the update. Position sizing uses stale balance. | 🔴 DANGEROUS |
| **Single cycle crash** | _run_supervised_cycle() catches, increments failure counter. After N consecutive failures → halt. | ✅ Handled |
| **Spread spikes** | SpreadMonitor blocks entries above 3× average. Exit checks tighten SL on open positions. | ✅ Handled |
| **Daily loss limit hit** | RiskEngine freezes trading. DrawdownGuard escalates to FROZEN. | ✅ Handled |
| **Portfolio heat > 2%** | State machine: NORMAL→DEFENSIVE (BE moves) →REDUCING (close weakest) →EMERGENCY (force close). | ✅ Handled |
| **Margin < 100%** | Margin guardian flattens ALL positions. One-way — no recovery without restart. | ⚠️ Aggressive |
| **Connection drops mid-trade** | Idempotency key system prevents duplicate orders. Reconciliation on reconnect. Position store persists MT5 trades. | ✅ Handled (MT5), ⚠️ Partial (Deriv) |
| **SQLite corruption** | PositionStore uses WAL mode. atomic_write uses fsync+replace. DailyMaintenance backup uses shutil.copy2 WITHOUT lock. | ⚠️ Backup-time risk |
| **All brain modules error** | Each module failure is caught independently. Scanner still produces a result with whatever modules succeeded. Score may be misleadingly low/high. | ⚠️ Degraded |

---

## PHASE 4 — CRITICAL AUDIT FINDINGS

### 🟢 CONFIRMED WORKING

1. **Multi-gate entry pipeline** — 7+ independent safety checks (scanner, decision engine, governor, planner, risk engine, entry validator, orchestrator) must all pass. Defence in depth is real.

2. **Position reconciliation** — Never removes a position without positive broker platform confirmation. Handles orphans, externally-closed trades, and crash recovery correctly.

3. **Thread-safe position management** — `_LockedPositions` with RLock. Proper cross-thread access from trading loop and dashboard.

4. **Atomic file writes** — tempfile + fsync + os.replace. JSON config changes are crash-safe.

5. **Append-only event store** — Background writer thread, bounded queue, WAL mode. Non-blocking writes.

6. **Idempotent order execution** — SHA-256 based keys embedded in order comments. Dedup checked against open positions, pending orders, and deal history.

7. **Multi-tier emergency response** — NORMAL→DEFENSIVE→REDUCING→EMERGENCY with per-hour/per-cycle caps on force-closes. Graduated, not all-or-nothing.

8. **Adaptive learning with safety bounds** — Score optimizer changes clamped to ±25% envelope and ±3 points/cycle. OOS validation gate. Rollback on failure.

9. **Shadow trading for rejected setups** — Rejected trades are paper-traded to measure gate effectiveness. Never touches real money.

10. **Circuit breaker pattern** — Consecutive failures trigger circuit open, exponential backoff, half-open test.

11. **Counterfactual attribution** — Leave-one-out module analysis measures each module's actual marginal contribution.

12. **Module governor lifecycle** — ACTIVE→SHADOW→DISABLED with Bayesian accuracy + marginal R dual signal. Struggling modules are shadowed (weight=0) before being disabled.

13. **Startup self-test** — Checks imports, config, registry, database, disk space before trading.

14. **Weekend protection** — Blocks new FX/metals positions on Friday near market close.

15. **Session-aware scanning** — 5s active / 60s quiet / 300s dead zone. Adapts scan frequency to market state.

---

### 🟡 DANGEROUS EDGE CASES

1. **🔴 CRITICAL — Hardcoded GitHub PAT in `access-token.py`**
   Token `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` is committed in plaintext. Even if revoked, it's in git history. **Immediate action: revoke token, remove file, scrub history.**

2. **🔴 CRITICAL — Deriv positions are in-memory only**
   `DerivConnector._positions` dict stores all position metadata (SL, TP, stake, multiplier). A crash loses this data. `modify_order()` fails silently because it can't look up the position. Unlike MT5 (where position_store.py persists to SQLite), Deriv has NO persistence layer for open positions.

3. **🔴 CRITICAL — Deriv token expiry has no auto-refresh**
   Access tokens are short-lived (~3600s). When they expire, the system logs CRITICAL but has no mechanism to refresh. Production halts until manual intervention.

4. **🔴 CRITICAL — Zero balance treated as falsy**
   `if balance` in Python treats 0.0 as False. Multiple places in `account_risk.py`, `risk_engine.py`, and `platform_manager.py` silently skip updates for a zero balance instead of erroring. A margin call that zeros the balance would leave the system blind.

5. **🔴 CRITICAL — PnLTracker is not persisted**
   `daily_tracker.py` stores daily/weekly P&L in memory only. A mid-day restart resets all daily loss tracking to zero, potentially allowing trading beyond the daily loss limit. The DrawdownGuard state IS persisted via RiskEngine, but the raw P&L data that feeds into daily limit checks is lost.

6. **🔴 CRITICAL — RiskEngine balance desync**
   `risk_engine.py` maintains its own `self.balance` that is modified by `record_trade_result()`. If the broker balance diverges (slippage, commissions, manual trades, broker adjustments), the engine's balance view diverges permanently. All sizing and P&L percentage calculations use this potentially-wrong balance.

7. **⚠️ HIGH — EntryValidator fails open on market hours**
   `check_market_open()` returns True (pass) when MT5 is unavailable, symbol_info returns None, or any exception occurs. A disconnected MT5 means the validator never catches that the market is closed.

8. **⚠️ HIGH — Stall exit threshold is not instrument-aware**
   TradeManager `_check_stall()` uses a fixed 5-pip threshold. For Gold (XAUUSD), 5 pips = $0.05. Every Gold trade would be stall-exited after the time limit regardless of actual P&L. For BTCUSD, 5 pips = $5 (negligible).

9. **⚠️ HIGH — Position sizer has no input validation on risk_pct**
   `risk_pct` is passed as a decimal fraction (0.02). No validation it's not accidentally a percentage (2.0). Passing 2.0 would risk 200% of the account. The 5% hard cap catches some cases but 5% per trade is still dangerous.

10. **⚠️ HIGH — CircuitBreaker is not thread-safe**
    No locks on state mutations. If the trading loop and health-check thread both call `record_failure()`/`record_success()` concurrently, the failure counter and state can become inconsistent.

11. **⚠️ HIGH — Candle cache has no eviction policy**
    `CandleCache` stores DataFrames with TTL but no max-size limit. With many symbols × timeframes × counts, memory grows without bound. No eviction.

12. **⚠️ HIGH — Idempotency key boundary crossing**
    5-minute time bucket. An intent submitted at 4:59 and retried at 5:01 gets a DIFFERENT key, defeating deduplication. Could cause duplicate orders on a retry across the bucket boundary.

13. **⚠️ HIGH — Symbol mapper creates new instances on every call**
    `resolve_to_internal()` creates a new `SymbolMapper` for EACH broker config on EVERY call, and for each mapper calls `to_broker()` on every registry symbol. O(brokers × symbols²) per call.

14. **⚠️ MEDIUM — TradeJournal partial-trade consolidation uses positional indexes**
    `_consolidate_partial_rows` accesses rows by magic number indices (0-9) rather than named columns. If SELECT order changes, consolidation silently corrupts data.

15. **⚠️ MEDIUM — FVG and OrderBlock status can oscillate**
    MITIGATED status can be overwritten by PARTIALLY on a later candle because there's no break after setting MITIGATED. Also, both use O(n²) `iterrows()` loops.

16. **⚠️ MEDIUM — No DST adjustment for session times**
    SessionEngine hardcodes UTC hours. London/NY session boundaries shift by 1 hour during DST changes. No adjustment.

17. **⚠️ MEDIUM — DatabaseBackup uses shutil.copy2 without lock**
    If SQLite is being written during backup, the copy can be corrupt. Should use SQLite's backup API or WAL checkpoint.

18. **⚠️ MEDIUM — Margin safety bypassed for Deriv**
    `_margin_guardian_check()` returns 0.0 for Deriv. Margin-based safety is entirely absent for Deriv positions.

19. **⚠️ MEDIUM — EntryEngine returns 0.01 lot on zero risk**
    When risk_pips ≤ 0, `calculate_position_size()` returns 0.01 (hardcoded minimum lot) instead of rejecting the trade. A zero-risk scenario should fail, not default to minimum lot.

20. **⚠️ MEDIUM — Outcome feedback JSONL grows unbounded**
    `_read_records()` reads the ENTIRE file into memory on every call. No rotation, no pruning. Will degrade over months of operation.

---

### 🔴 MISSING ENTIRELY

1. **No Deriv position persistence** — MT5 positions are SQLite-backed and survive crashes. Deriv positions are in-memory dictionaries. A crash means open Deriv positions become unmanaged orphans that the system doesn't know about until reconciliation from the broker (which may not have the internal SL/TP/regime/score metadata needed for proper management).

2. **No token refresh mechanism** — Deriv access tokens expire. The system has no OAuth2 refresh token flow, no token rotation, no alerting before expiry. The `access-token.py` file suggests this was done manually.

3. **No rate limiting on dashboard API** — The FastAPI dashboard has API key auth but no rate limiting. A brute-force attack or malicious client could exhaust resources.

4. **No encryption at rest** — SQLite databases, JSONL journals, broker configs with credentials hints are all plaintext on disk. No disk encryption beyond what the host provides.

5. **No automated alerting/notification** — When the system enters EMERGENCY state, hits daily loss cap, or freezes — it logs to stderr and event store. No email, SMS, Telegram, or webhook notification. The operator must be watching logs.

6. **No graceful degradation for news guard** — `NewsGuard._fetch_events()` makes an HTTP call to `nfs.faireconomy.media`. If the feed is permanently down (not just transient), the system blocks ALL trading indefinitely (fail-closed). There's no circuit breaker or TTL on the news block.

7. **No health check endpoint for container orchestration** — The `HealthCheck` class exists in `ops/lifecycle.py` but is not wired to a `/health` or `/ready` endpoint that Kubernetes/Docker health probes could use.

8. **No position-level P&L persistence for Deriv** — When a Deriv trade closes, the realized P&L relies on the in-memory `_positions` dict. If the position was lost due to a crash, the close P&L defaults to 0.0.

9. **No thread-safe spread recording** — `SpreadMonitor` uses plain deques without locks. Concurrent scan threads could interleave append/read operations.

10. **No maximum drawdown kill switch at the account level** — The FROZEN mode halts new trading but does NOT close existing positions. A flash crash during FROZEN could wipe out remaining equity while the system watches.

---

## PHASE 5 — ARCHITECTURE SUMMARY

### Data / Signal / Money Flow

```
MARKET DATA (MT5 IPC / Deriv WebSocket)
  │
  ▼
PlatformManager.fetch_all_market_data()  ──→  CandleCache (in-memory, TTL)
  │
  ▼
PairScanner.scan_all()
  ├── StructureEngine (swings, BOS, CHoCH)
  ├── FVGDetector (price gaps)
  ├── OrderBlockDetector (supply/demand zones)
  ├── LiquidityMapper (stop clusters, sweeps)
  ├── WyckoffEngine (accumulation/distribution)
  ├── CurrencyStrengthMeter (cross-pair RSI)
  ├── VolumeAnalyzer (spikes, divergence)
  ├── MomentumDivergence (RSI/MACD)
  └── SessionVWAP (intraday anchor)
  │
  ▼
DirectionalConsensus.decide_opportunities()  ──→  9 weighted votes → clusters
  │
  ▼
PairRanker.rank()  ──→  sorted by composite priority
  │
  ▼
DecisionEngine.decide_entry()  ──→  ENTER/SKIP with conviction
  │
  ▼
TradePlanner.plan()  ──→  structured trade plan
  │
  ▼
RiskGovernor.review_entry()  ──→  approve / dim / veto
  │
  ▼
EntryEngine.calculate_entry()  ──→  exact entry/SL/TP/lots
  │
  ▼
EntryValidator.validate()  ──→  final safety gate
  │
  ▼
Orchestrator.evaluate_proposal()  ──→  graded size multiplier [0.15, 1.0]
  │
  ▼
RiskEngine.assess()  ──→  drawdown/daily/correlation/sizing
  │
  ▼
PlatformManager.place_order()  ──→  MT5 order_send() / Deriv WS buy
  │
  ▼                                          ▼
MONEY IN THE MARKET                    ShadowStore (rejected setups)
  │                                          │
  ▼                                          ▼
TradeManager.update() per cycle         ShadowResolver (paper trade)
  │
  ▼
Management: SL/TP/BE/trail/partial/exit
  │
  ▼
TRADE CLOSES  ──→  TradeJournal.record()
                   OutcomeFeedback.complete()
                   TunerAgent.on_trade_close()
                   CounterfactualEngine
                   Adaptive learners
```

### External Dependencies

| Dependency | Used For | Failure Impact |
|---|---|---|
| MetaTrader5 (Windows IPC) | MT5 broker connection, market data, order execution | MT5 trading halted; Deriv continues |
| websockets + aiohttp | Deriv WebSocket connection and REST auth | Deriv trading halted; MT5 continues |
| pandas | DataFrame operations throughout | System cannot function |
| numpy | Math in adaptive learners, some brain modules | Adaptive learning degraded |
| torch (PyTorch) | RL neural network (actor-critic) | RL disabled; rule-based trading continues |
| loguru | Logging throughout | Logging degraded, event store disrupted |
| SQLite (stdlib) | Position store, event store, all adaptive DBs | Persistence degraded |
| FastAPI + uvicorn | Dashboard API | Dashboard unavailable; trading unaffected |
| ddtrace (optional) | Datadog APM | No observability; trading unaffected |
| feedparser | RSS news feed for NewsGuard | Fail-closed: all trading blocked if enabled |
| nfs.faireconomy.media | Economic calendar data | News guard blocks trading if unavailable |

### Failure Domains

| Domain | Components | Blast Radius |
|---|---|---|
| **MT5 Connection** | MT5Connector, symbol mapper | MT5 instruments stop trading. Deriv unaffected. |
| **Deriv Connection** | DerivConnector, WebSocket | Deriv instruments stop trading. MT5 unaffected. |
| **Brain Modules** | All 9 analysis modules | Degraded scoring. Trades may execute with incomplete evidence. |
| **Risk Engine** | RiskEngine, DrawdownGuard, PositionSizer | ALL trading halted (correctly). |
| **Persistence** | SQLite stores (6+), JSONL journals | Crash recovery degraded. Adaptive learning paused. |
| **Adaptive Layer** | 29 components, TunerAgent | Learning pauses. Trading continues with stale parameters. |
| **Dashboard** | FastAPI, React frontend | No visibility. Trading unaffected. |
| **RL Subsystem** | 14 components | RL score augmentation disabled. Rule-based trading unaffected. |
| **News Feed** | NewsGuard, feedparser | **Fail-closed: ALL trading blocked.** |

---

## CRITICAL RECOMMENDATIONS (Priority Order)

1. **IMMEDIATE: Revoke the GitHub PAT in `access-token.py`** and scrub it from git history. This is a live credential leak.

2. **HIGH: Add Deriv position persistence** — Mirror the MT5 `PositionStore` pattern for Deriv positions. Without this, a crash during open Deriv positions means unmanaged money in the market.

3. **HIGH: Implement Deriv token auto-refresh** — Use the OAuth2 refresh token flow. Alert well before expiry.

4. **HIGH: Fix zero-balance falsy check** — Replace `if balance` with `if balance is not None and balance >= 0` throughout.

5. **HIGH: Persist PnLTracker daily state** — A restart mid-day must not reset daily loss tracking.

6. **HIGH: Add thread safety to CircuitBreaker** — Add a threading.Lock.

7. **HIGH: Make stall exit threshold instrument-aware** — Use ATR-based or pip-value-based thresholds instead of fixed 5 pips.

8. **HIGH: Add automated alerting** — Webhook/Telegram/email for EMERGENCY state, daily loss halt, FROZEN mode.

9. **MEDIUM: Add eviction to CandleCache** — LRU or size-bounded eviction policy.

10. **MEDIUM: Fix idempotency key boundary** — Use overlapping or sliding time windows.

---

## FINAL VERDICT

Apex is a **legitimate, deeply-engineered trading system** with approximately 45,000+ lines of Python implementing institutional ICT-style analysis, multi-timeframe confluence scoring, 9-module consensus voting, adaptive ML optimization, reinforcement learning, shadow trading, counterfactual attribution, and multi-tiered risk management across two broker platforms.

**What it gets right:** Defence in depth (7+ gates before any trade), graded sizing instead of binary decisions, position reconciliation with positive broker confirmation, crash-safe persistence for MT5, adaptive learning with safety bounds and rollback, shadow trading for rejected setups, and a genuinely institutional risk philosophy.

**What is dangerous:** Deriv positions are not persisted, token expiry has no auto-refresh, zero balance is silently ignored, daily P&L tracking resets on restart, several thread safety gaps, and a hardcoded GitHub credential is committed to the repository.

**The system is production-capable for MT5 instruments with the above fixes applied. Deriv trading should be considered unsafe until position persistence is added.**
