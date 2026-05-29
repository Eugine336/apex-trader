# APEX TRADER
### Institutional-Grade Automated Trading System

A professional multi-platform trading bot built with the mindset of a 100-year veteran trader.
Sharp. Precise. Always watching. In and out like a sniper.

---

## Architecture

```
apex-trader/
├── brain/                    # Phase 1 ✅ - Market Reading + Intelligence Engine (17 modules)
│   ├── structure_engine.py   # Market structure: HH/HL/LH/LL, BOS, CHOCH
│   ├── liquidity_mapper.py   # Where stops are resting — equal highs/lows
│   ├── fvg_detector.py       # Fair Value Gaps — price imbalances
│   ├── order_block.py        # Institutional order blocks & breakers
│   ├── currency_strength.py  # Real-time 8-currency strength ranking
│   ├── session_engine.py     # Session timing + news guard
│   ├── regime_detector.py    # Regime classification + score caps
│   ├── volume_analyzer.py    # Tick-volume spikes, divergence, PoC
│   ├── inducement_detector.py# Manipulation and fake breakout traps
│   ├── wyckoff_engine.py     # Wyckoff phases and spring/upthrust
│   ├── mtf_orchestrator.py   # H4→H1→M15/M5→M1 setup cascade
│   ├── trade_journal.py      # SQLite trade/decision auditing
│   ├── drawdown_guard.py     # Dynamic recovery and freeze logic
│   ├── execution_monitor.py  # Slippage, spread, and latency quality
│   ├── correlation_engine.py # Cross-pair exposure and hedge checks
│   └── backtest_engine.py    # Replay, walk-forward, Monte Carlo
│
├── scanner/                  # Phase 2 ✅ - Multi-Pair Scanner
│   ├── pair_scanner.py       # Core scanner — 7-factor confluence scoring
│   ├── pair_ranker.py        # Tie-breaking & prioritization engine
│   └── scan_scheduler.py     # Adaptive scan frequency controller
│
├── trigger/                  # Phase 3 ✅ - Entry Engine
│   ├── entry_engine.py       # Zone discovery, M1 CHOCH, SL/TP, position sizing
│   ├── entry_patterns.py     # 5 M1 patterns: engulfing, pin bar, inside bar, wick, volume
│   └── entry_validator.py    # 7 safety checks: spread, R:R, drawdown, expiry, max trades
│
├── management/               # Phase 4 ✅ - Trade Management
│   ├── trade_manager.py      # Full trade lifecycle: SL → TP1 → breakeven → trail → TP2
│   ├── trailing_stop.py      # Structure-based trailing stop (follows swing lows/highs)
│   ├── partial_close.py      # 50% partial close at TP1 + breakeven calculation
│   └── re_entry.py           # Re-entry logic after breakeven stops
│
├── risk/                     # Phase 5 ✅ - Risk Engine
│   ├── risk_engine.py        # Central risk authority — the ultimate gate
│   ├── position_sizer.py     # Dynamic position sizing with volatility adjust
│   ├── daily_tracker.py      # Daily/weekly/monthly P&L tracking
│   ├── spread_monitor.py     # Spread protection and alerting
│   └── risk_reporter.py      # Risk dashboard data aggregation
│
├── ml/                       # Phase 6 ✅ - ML Adapter (The Memory)
│   ├── trade_analyzer.py     # PerformanceProfile — dissects every trade
│   ├── score_optimizer.py    # Adaptive scoring weights (gradual, sums to 100)
│   ├── regime_learner.py     # Regime-specific strategy adaptation
│   ├── pair_learner.py       # Pair-specific confidence multipliers
│   ├── session_learner.py    # Session aggression levels
│   └── ml_adapter.py         # Master controller — coordinates all learners
│
├── platforms/                # Phase 7 ✅ - MT5 + Deriv Integration
│   ├── base_connector.py     # Abstract interface all connectors share
│   ├── platform_manager.py   # Unified routing — one interface, two arms
│   ├── main_loop.py          # Master trading loop — scan→enter→manage→repeat
│   ├── mt5/
│   │   └── mt5_connector.py  # MetaTrader 5 via official Python package
│   └── deriv/
│       └── deriv_connector.py# Deriv WebSocket API — synthetics 24/7
│
├── dashboard/                # Phase 8 ✅ - React Dashboard + FastAPI Backend
│   ├── api.py                # FastAPI REST + WebSocket API (8 endpoints)
│   ├── state.py              # Shared in-memory state store + demo data
│   ├── start.sh              # One-command launcher (API + React)
│   ├── requirements.txt      # Dashboard-specific Python deps (FastAPI, uvicorn)
│   ├── tests/
│   │   └── test_api.py       # API endpoint + WebSocket tests
│   └── frontend/             # React app (dark theme)
│       ├── src/pages/        # Overview, ActiveTrades, Scanner, Performance, Risk, ML, Controls, History
│       ├── src/components/   # Charts, Layout, ScoreBar, StatusBar, TradeCard
│       └── src/hooks/        # useApi, useWebSocket
├── data/                     # Trade logs and historical data
├── tests/                    # Unit tests (214+ passing)
├── config.py                 # Full instrument registry (59 instruments) + all settings
├── main.py                   # Entry point — boots all 8 phases, --dashboard flag
├── requirements.txt
└── .env.example              # Credentials template
```

---

## Instruments Covered — 59 Total

| Category | Count | Examples |
|----------|-------|---------|
| Forex Majors | 7 | EURUSD, GBPUSD, USDJPY, AUDUSD |
| Forex Crosses | 21 | EURGBP, GBPJPY, EURAUD, NZDCAD |
| Commodities | 4 | XAUUSD (Gold), XAGUSD (Silver), XBRUSD, XTIUSD |
| Indices | 10 | US100, US30, GER40, UK100, JP225, HK50 |
| Deriv Synthetics | 17 | V75_1S, BOOM500, CRASH1000, STPIDX |

Every instrument has its own pip size, spread, and margin category in the registry.

---

## What This System Does

**The Brain reads the market like a professional:**
- Identifies market structure (HH/HL trend or LH/LL downtrend)
- Maps exactly where institutional stop clusters are resting
- Detects Fair Value Gaps — imbalances price must return to fill
- Finds Order Blocks — where institutions placed their large orders
- Ranks all 8 major currencies by real-time strength
- Knows which session is active and which pairs are hottest
- Guards against high-impact news events automatically
- Classifies market regimes (trend, range, volatility, accumulation, distribution)
- Confirms setups with tick-volume spikes, divergence, and climax behavior
- Detects inducement, fake breakouts, stop hunts, and turtle soup traps
- Reads Wyckoff phase transitions and highlights spring/upthrust entries
- Coordinates the full H4/H1 bias-to-M1 trigger cascade
- Logs all decisions and monitors execution quality and drawdown health
- Prevents hidden correlation overexposure across open trades
- Supports replay backtesting with walk-forward and Monte Carlo analysis

**The Risk Engine is the ultimate authority:**
- Every trade must pass through RiskEngine.assess() — no exceptions
- Dynamic sizing: 2% risk in NORMAL, 1.5% in CAUTION, 1% in RECOVERY, 0% in FROZEN
- Daily P&L tracked to the dollar — breaches -5% → instant FROZEN mode
- Weekly drawdown > 8% → automatic RECOVERY mode
- Spread monitor blocks entries when spreads spike above 3× average
- Correlation engine prevents hidden overexposure across currencies
- No duplicate trades on same pair+direction
- Position size auto-reduced when approaching daily loss limit
- RiskReporter aggregates everything into a single health dashboard
- Daily/weekly resets with graduated recovery (FROZEN → CAUTION, not straight to NORMAL)

**The Scanner watches everything simultaneously:**
- Scans all 59 instruments every 10 seconds during active sessions
- Scores every pair on 7 confluence factors (max 100)
- READY (85+) → trigger fires. WATCHLIST (70+) → monitoring. Below → wait.
- Ranks ties by regime, session, sweep, volume, and Wyckoff signals
- Adapts scan frequency: 10s overlap, 60s quiet, 300s dead zones

**The Entry System fires only on A+ setups:**
- Minimum 85/100 confluence score required
- Liquidity sweep must be confirmed before entry
- Multi-timeframe alignment (H4 → H1 → M5 → M1)
- Enters at the midpoint of FVGs or Order Blocks
- 5 M1 confirmation patterns: engulfing, pin bar, inside bar breakout, rejection wick, volume spike
- 7 pre-entry safety checks: spread, R:R, drawdown, expiry, max trades, correlation, session

**The Trade Manager protects every winner:**
- 50% partial close at 1:1 R:R
- Stop moved to breakeven after TP1
- Structure-based trailing stop (follows M5 swing lows/highs)
- Time-based exit if price stalls for 75 minutes
- Re-entry logic if stopped at breakeven with 15-minute cooldown

**The ML Adapter gets smarter every day:**
- Learns from your bot's own trade results (starts blank — zero bias)
- Optimizes scoring weights gradually (max ±3 per cycle)
- Profiles pairs, sessions, and regimes with confidence multipliers
- Retrains every 50 trades or 7 days, whichever comes first

---

## Brain Intelligence Modules

- **Regime Detector**: ATR + directional strength model with ranging score caps and volatile freeze logic.
- **Volume Analyzer**: Tick-volume ratio, divergence, climax detection, and point-of-control estimation.
- **Inducement Detector**: Stop hunts, fake breakouts, and trap-pattern recognition.
- **Wyckoff Engine**: Phase A–E classification with spring and upthrust signaling.
- **MTF Orchestrator**: Unified setup builder from higher-timeframe bias to lower-timeframe trigger.
- **Trade Journal**: Async SQLite storage for taken trades and rejected setups.
- **Drawdown Guard**: Normal/Caution/Recovery/Frozen mode transitions with risk adaptation.
- **Execution Monitor**: Slippage, latency, spread, and requote quality scoring.
- **Correlation Engine**: Currency exposure maps and synthetic hedge conflict checks.
- **Backtest Engine**: Historical replay, walk-forward splits, and Monte Carlo robustness checks.

---

## Setup

```bash
# Clone the repo
git clone https://github.com/Eugine336/apex-trader.git
cd apex-trader

# Install core dependencies
pip install -r requirements.txt

# Set up environment
cp .env.example .env
# Edit .env with your credentials

# Run the bot
python main.py

# Run with dashboard
pip install -r dashboard/requirements.txt
python main.py --dashboard
# API: http://localhost:8000  |  Swagger: http://localhost:8000/docs

# Or launch full dashboard (API + React frontend)
bash dashboard/start.sh
# API: http://localhost:8000  |  Frontend: http://localhost:3000
```

---

## Target Performance

| Metric | Target |
|--------|--------|
| Win Rate | 80%+ |
| Risk per Trade | 2% |
| Min R:R | 1:1.5 |
| Daily Trades | 10–30 |
| Monthly Return | 10–25% |
| Max Daily Loss | 5% |

---

## Build Status

| Phase | Module | Status |
|-------|--------|--------|
| 1 | Brain — Market Reading + Intelligence Engine (17 modules) | ✅ Complete |
| 2 | Scanner — Multi-Pair Scanner | ✅ Complete |
| 3 | Trigger — Entry Engine | ✅ Complete |
| 4 | Management — Trade Manager | ✅ Complete |
| 5 | Risk — Risk Engine | ✅ Complete |
| 6 | ML — Adaptive Learning | ✅ Complete |
| 7 | Platforms — MT5 + Deriv Integration | ✅ Complete |
| 8 | Dashboard — React UI + FastAPI Backend | ✅ Complete |

---

## Platform Integration (MT5 + Deriv)

Two arms, one mind. MT5 for Forex/indices. Deriv for synthetics — 24/7.

- **BaseConnector**: Abstract interface — `connect`, `place_order`, `modify_order`, `close_order`, `get_ohlcv`, etc. Every platform speaks the same language.
- **MT5Connector**: MetaTrader 5 via official Python package. Windows-only with auto-detection. Handles Forex, commodities, indices. Auto-discovers broker symbol suffixes (`m`, `.raw`, `#`).
- **DerivConnector**: WebSocket API connector for Deriv. Handles all synthetics (V75, Boom/Crash, Step, Jump, Range Break) plus Forex via multiplier contracts. Auto-reconnect on drop.
- **PlatformManager**: Routes every trade to the correct platform based on the instrument registry. Merges positions, balances, and market data from both platforms into a single view.
- **TradingLoop**: The heartbeat — Scan → Entry → Manage → Repeat. Integrates scanner, orchestrator, drawdown guard, correlation engine, execution monitor, and trade journal into one continuous loop.

---

## ⚠️ Disclaimer

This software is for educational and personal use.
Trading involves significant risk. Past performance does not guarantee future results.
Always test on a demo account before going live.
