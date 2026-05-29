# APEX TRADER
### Institutional-Grade Automated Trading System

A professional multi-platform trading bot built with the mindset of a 100-year veteran trader.
Sharp. Precise. Always watching. In and out like a sniper.

---

## Architecture

```
apex-trader/
├── brain/                    # Phase 1.5 ✅ - Market Reading + Intelligence Engine
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
├── trigger/                  # Phase 3 🔄 - Entry Engine
├── management/               # Phase 4 🔄 - Trade Management
├── risk/                     # Phase 5 🔄 - Risk Engine
├── ml/                       # Phase 6 🔄 - ML Adapter
├── platforms/                # Phase 7 🔄 - MT5 + Deriv Integration
│   ├── mt5/                  # MQL5 Expert Advisor
│   └── deriv/                # Python WebSocket bot
├── dashboard/                # Phase 8 ✅ - Live Trading Dashboard
│   ├── api.py                # FastAPI backend (9 endpoints + WebSocket)
│   ├── state.py              # Shared dashboard state with demo data
│   ├── start.sh              # Launch script for API + React
│   ├── requirements.txt      # Dashboard-specific Python deps
│   ├── frontend/             # React app
│   │   ├── src/
│   │   │   ├── pages/        # 8 pages: Overview, Trades, History, Scanner...
│   │   │   ├── components/   # Layout, StatusBar, Charts, TradeCard, ScoreBar
│   │   │   └── hooks/        # useApi, useWebSocket
│   │   └── package.json
│   └── tests/
│       └── test_api.py       # 24 API endpoint tests
│
├── data/                     # Trade logs and historical data
├── tests/                    # Unit tests
├── config.py                 # Full instrument registry + all settings
├── main.py                   # Entry point (--dashboard flag for web UI)
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

**The Trade Manager protects every winner:**
- 50% partial close at 1:1 R:R
- Stop moved to breakeven after TP1
- Structure-based trailing stop
- Time-based exit if price stalls
- Re-entry logic if stopped at breakeven

**Risk Engine protects the account:**
- Fixed 2% risk per trade
- Max 5% daily drawdown — bot pauses
- Max 6 correlated trades at once
- Spread monitor — skips wide spreads

---

## New in Phase 1.5

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
git clone https://github.com/yourusername/apex-trader.git
cd apex-trader

# Install dependencies
pip install -r requirements.txt

# Set up environment
cp .env.example .env
# Edit .env with your credentials

# Run the bot
python main.py

# Run with the web dashboard
python main.py --dashboard
# Then open http://localhost:8000

# Or launch both API + React dev server
cd dashboard && bash start.sh
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
| 1 | Brain — Market Reading Engine | ✅ Complete |
| 1.5 | Brain — Intelligence & Validation Layer | ✅ Complete |
| 2 | Scanner — Multi-Pair Scanner | ✅ Complete |
| 3 | Trigger — Entry Engine | 🔄 Pending |
| 4 | Management — Trade Manager | 🔄 Pending |
| 5 | Risk — Risk Engine | 🔄 Pending |
| 6 | ML — Adaptive Learning | 🔄 Pending |
| 7 | Platforms — MT5 + Deriv | 🔄 Pending |
| 8 | Dashboard — React UI | ✅ Complete |

---

## ⚠️ Disclaimer

This software is for educational and personal use.
Trading involves significant risk. Past performance does not guarantee future results.
Always test on a demo account before going live.
