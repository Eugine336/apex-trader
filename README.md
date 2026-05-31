# APEX TRADER
### Automated Multi-Platform Trading System

A fully automated trading system for MetaTrader 5 and Deriv WebSocket API covering 66 instruments across forex, commodities, indices, synthetics, and crypto.

APEX TRADER is a capital allocation engine. It identifies opportunities, measures risk, deploys capital, and protects capital. It carries no label — not scalper, not day trader, not swing trader. The market determines the holding period. Risk is the final, non-negotiable authority.

---

## Architecture

```
apex-trader/
├── brain/                    # Market intelligence engine (17 modules)
│   ├── structure_engine.py   # Market structure: HH/HL/LH/LL, BOS, CHOCH
│   ├── liquidity_mapper.py   # Institutional stop clusters — equal highs/lows
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
│   ├── drawdown_guard.py     # NORMAL/CAUTION/RECOVERY/FROZEN mode transitions
│   ├── execution_monitor.py  # Slippage, spread, and latency quality
│   ├── correlation_engine.py # Cross-pair exposure and hedge checks
│   └── backtest_engine.py    # Replay, walk-forward, Monte Carlo
│
├── scanner/                  # Multi-pair scanner
│   ├── pair_scanner.py       # 10-factor confluence scoring (max 100)
│   ├── pair_ranker.py        # Opportunity ranking by EV × score × pair_multiplier
│   └── scan_scheduler.py     # Adaptive frequency: 10s overlap, 60s quiet, 15s with positions
│
├── trigger/                  # Entry engine
│   ├── entry_engine.py       # Zone discovery, M1 confirmation, SL/TP, sizing
│   ├── entry_patterns.py     # 5 M1 patterns: engulfing, pin bar, inside bar, wick, volume
│   └── entry_validator.py    # 7 safety checks: spread, R:R, drawdown, expiry, max trades
│
├── management/               # Trade lifecycle
│   ├── trade_manager.py      # SL → TP1 → breakeven → trail → TP2, timeframe-aware stall
│   ├── trailing_stop.py      # Structure-based trailing (M5 swing lows/highs)
│   ├── partial_close.py      # 50% partial at TP1 + breakeven
│   └── re_entry.py           # Re-entry after breakeven stops (15-min cooldown)
│
├── risk/                     # Risk engine — the ultimate authority
│   ├── risk_engine.py        # Multi-stage gate: drawdown, correlation, EV, sizing
│   ├── position_sizer.py     # Dynamic sizing with micro account support ($5+)
│   ├── daily_tracker.py      # Daily/weekly/monthly P&L tracking
│   ├── spread_monitor.py     # Spread spike protection
│   └── risk_reporter.py      # Aggregated risk health reporting
│
├── adaptive/                 # Adaptive optimizer (statistical heuristics)
│   ├── optimizer.py          # Master controller — coordinates all learners
│   ├── trade_analyzer.py     # PerformanceProfile — dissects every trade
│   ├── score_optimizer.py    # Adaptive scoring weights (gradual, sums to 100)
│   ├── regime_learner.py     # Regime-specific strategy adaptation
│   ├── pair_learner.py       # Pair-specific confidence multipliers
│   ├── session_learner.py    # Session aggression levels
│   └── ev_estimator.py       # Expected value estimation per pair/regime/session
│
├── platforms/                # MT5 + Deriv integration
│   ├── base_connector.py     # Abstract connector interface
│   ├── platform_manager.py   # Unified routing — one interface, two brokers
│   ├── main_loop.py          # Trading loop: scan → enter → manage → repeat
│   ├── mt5/
│   │   └── mt5_connector.py  # MetaTrader 5 connector
│   └── deriv/
│       └── deriv_connector.py# Deriv WebSocket API connector
│
├── persistence/              # Crash recovery
│   └── position_store.py     # SQLite WAL position persistence + reconciliation
│
├── dashboard/                # React + FastAPI UI
│   ├── api.py                # REST + WebSocket API with API key auth
│   ├── state.py              # LiveState orchestrator (delegates to mixins)
│   ├── state_helpers.py      # Shared utilities
│   ├── state_status.py       # Bot status endpoint
│   ├── state_trades.py       # Open trades endpoint
│   ├── state_history.py      # Trade history endpoint
│   ├── state_scanner.py      # Scanner results endpoint
│   ├── state_risk.py         # Risk status endpoint
│   ├── state_performance.py  # Performance metrics endpoint
│   ├── state_ml.py           # Adaptive optimizer insights endpoint
│   ├── state_controls.py     # Start/stop/pause controls
│   └── frontend/             # React app (dark theme)
│
├── ml/                       # Backward-compatibility shims → adaptive/
├── config.py                 # 66-instrument registry + all settings
├── main.py                   # Entry point — boots all phases, --dashboard flag
├── pyproject.toml            # Ruff + pytest config
├── requirements.txt          # Python dependencies
├── .github/workflows/ci.yml  # CI pipeline (pytest + ruff on push/PR)
└── docker-compose.yml        # Docker + Datadog Agent sidecar
```

---

## Instruments — 66 Total

| Category | Count | Examples |
|----------|-------|---------|
| Forex Majors | 7 | EURUSD, GBPUSD, USDJPY, AUDUSD, NZDUSD, USDCAD, USDCHF |
| Forex Crosses | 21 | EURGBP, GBPJPY, EURAUD, NZDCAD, GBPNZD |
| Commodities | 4 | XAUUSD (Gold), XAGUSD (Silver), XBRUSD, XTIUSD |
| Indices | 10 | US100, US30, US500, GER40, UK100, JP225, HK50 |
| Synthetics | 15 | V75_1S, V100_1S, BOOM500, CRASH1000, STPIDX |
| Crypto | 9 | BTCUSD, ETHUSD, SOLUSD, XRPUSD, ADAUSD |

Each instrument has its own pip size, spread baseline, pip value, margin category, trading hours, and platform routing.

---

## How It Works

1. **Scan** — All 66 instruments scored on 10 confluence factors every 10 seconds during active sessions
2. **Analyse** — Structure, FVG, order blocks, liquidity, currency strength, volume, Wyckoff, regime, session, inducement
3. **Trigger** — M1 confirmation at entry zones with dynamic signal expiry per timeframe
4. **Risk** — Multi-stage gate: drawdown mode, daily P&L, correlation, EV estimate, score-based sizing, micro account awareness
5. **Execute** — Routes to MT5 or Deriv via PlatformManager
6. **Manage** — TP1 partial → breakeven → structure trailing → TP2, with timeframe-aware stall exits
7. **Persist** — SQLite WAL position store with crash recovery and broker reconciliation
8. **Learn** — Adaptive optimizer profiles pairs, regimes, and sessions from trade outcomes

---

## Key Features

- **10-factor confluence scoring** with configurable entry threshold
- **Multi-stage risk engine** (NORMAL → CAUTION → RECOVERY → FROZEN)
- **Dual-platform** MT5 + Deriv WebSocket with unified routing
- **Position persistence** with crash recovery and broker reconciliation
- **Dashboard with API key authentication** (DD_DASHBOARD_API_KEY)
- **Adaptive optimizer** — statistical heuristics that learn from trade outcomes
- **High-water mark tracking** with equity curve awareness
- **Micro account support** ($5+) with dynamic minimum viable trade detection
- **Expected value gate** — blocks only proven losers, permissive by default
- **Score-based dynamic risk scaling** — higher conviction = more capital
- **Opportunity cost ranking** — EV × score × pair_multiplier
- **Timeframe-aware stall exits** — M5 entry stalls at 60 min, H1 at 180 min
- **Dynamic signal expiry** — scales with the timeframe that generated the setup
- **Datadog APM integration** via ddtrace
- **CI pipeline** — pytest + ruff on every push/PR

---

## Quick Start

```bash
# Clone
git clone https://github.com/eugine336/apex-trader.git
cd apex-trader

# Install dependencies
pip install -r requirements.txt

# Configure
cp .env.example .env
# Edit .env with your broker credentials

# Run headless
python main.py

# Run with dashboard
pip install -r dashboard/requirements.txt
python main.py --dashboard
# Dashboard: http://localhost:8000

# Run tests
python -m pytest tests/ -v
```

### Docker

```bash
docker-compose up -d
```

---

## Configuration

### Environment Variables

| Variable | Required | Description |
|---|---|---|
| `MT5_LOGIN` | MT5 only | MT5 account number |
| `MT5_PASSWORD` | MT5 only | MT5 account password |
| `MT5_SERVER` | MT5 only | Broker server name |
| `DERIV_API_TOKEN` | Deriv only | API token from app.deriv.com |
| `DERIV_APP_ID` | Deriv only | App ID from app.deriv.com |
| `DD_DASHBOARD_API_KEY` | No | API key for dashboard authentication |
| `DD_API_KEY` | No | Datadog API key for APM |
| `APP_ENV` | No | development or production (default: development) |

### Key Settings (config.py)

| Setting | Default | Description |
|---|---|---|
| `scoring.min_entry_score` | 85 | Minimum confluence to trigger entry |
| `risk.risk_per_trade_pct` | 0.5 | Base risk per trade (%) |
| `risk.max_daily_drawdown_pct` | 3.0 | Daily loss limit before FROZEN |
| `risk.max_open_trades` | 3 | Maximum simultaneous positions |
| `risk.max_spread_multiplier` | 2.0 | Block entry if spread > typical × this |
| `risk.ev_threshold` | -0.1 | Minimum expected value (blocks proven losers) |

---

## Testing

```bash
python -m pytest tests/ -v
```

Currently: 390+ tests across 16 test files covering risk engine, trade manager, entry engine, pair scanner, structure engine, FVG detector, session engine, symbol mapper, platforms, position store, dashboard auth, adaptive optimizer, capital context, intelligence (Phase 2), and full integration pipeline.

CI runs automatically on every push to main and every pull request.

---

## What This Is NOT

- Not a scalper, day trader, or swing trader — it identifies opportunities
- Not machine learning — the adaptive optimizer uses statistical heuristics
- Timeframe-agnostic — the market decides holding period (3 seconds to 3 days)
- Not institutional-grade — lacks FIX protocol, HA, compliance infrastructure

---

## Disclaimer

This software is for educational and personal use.
Trading involves significant risk. Past performance does not guarantee future results.
Always test on a demo account before going live.
