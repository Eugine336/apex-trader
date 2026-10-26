# APEX TRADER
### Continuously Operating, Market-Aware Trading Intelligence

APEX TRADER is a multi-platform trading system for MetaTrader 5 and Deriv. Its identity applies across the entire repository, every supported instrument, each analytical family, the whole portfolio and every campaign—not only Gold.

## Canonical identity

> Apex is a continuously operating, market-aware trading intelligence. It does not belong to one strategy, timeframe or trading style. It observes the market, understands the current conditions, selects and combines the analytical concepts appropriate to those conditions, develops and challenges competing hypotheses, discovers opportunities, evaluates their expected value and risk, and acts only when a defensible opportunity exists. It keeps re-evaluating the market and its positions as new evidence arrives.

This is binding. Apex must observe market state before choosing analytical methods. ICT/SMC, price action, M1 candlestick patterns, trend following, momentum, mean reversion, breakouts, Wyckoff, CRT, volume/auction, volatility, session/macro, intermarket and statistical methods are analytical tools—not permanent trading identities or independent order authorities.

Read [the canonical identity](docs/APEX_IDENTITY.md) and [the system-wide implementation audit](docs/APEX_IDENTITY_IMPLEMENTATION_AUDIT.md). Those documents distinguish implemented/wired/tested behaviour from shadow, legacy, data-limited and unverified capabilities.


---

## Architecture

```
apex-trader/
├── brain/                    # Specialist analytical observers and market measurements
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
│   ├── mtf_orchestrator.py   # Legacy orchestration; see identity audit for live wiring
│   ├── trade_journal.py      # SQLite trade/decision auditing
│   ├── drawdown_guard.py     # NORMAL/CAUTION/RECOVERY/FROZEN mode transitions
│   ├── execution_monitor.py  # Slippage, spread, and latency quality
│   ├── correlation_engine.py # Cross-pair exposure and hedge checks
│   └── backtest_engine.py    # Replay, walk-forward, Monte Carlo
│
├── scanner/                  # Multi-pair scanner
│   ├── pair_scanner.py       # Legacy scanner score; not the CognitiveBrain's sole authority
│   ├── pair_ranker.py        # Legacy candidate ranking; live decision authority must be verified
│   └── scan_scheduler.py     # Adaptive frequency: 10s overlap, 60s quiet, 15s with positions
│
├── trigger/                  # Entry engine
│   ├── entry_engine.py       # Legacy entry utilities; single-path cognition is authoritative when enabled
│   ├── entry_patterns.py     # Legacy M1 confirmations; shared Brain observer is separate
│   └── entry_validator.py    # 7 safety checks: spread, R:R, drawdown, expiry, max trades
│
├── management/               # Trade lifecycle
│   ├── trade_manager.py      # SL → TP1 → breakeven → trail → TP2, timeframe-aware stall
│   ├── trailing_stop.py      # Structure-based trailing (M5 swing lows/highs)
│   ├── partial_close.py      # 50% partial at TP1 + breakeven
│   └── re_entry.py           # Re-entry after breakeven stops (15-min cooldown)
│
├── risk/                     # Risk utilities; verify the authoritative live path
│   ├── risk_engine.py        # Multi-stage gate: drawdown, correlation, EV, sizing
│   ├── position_sizer.py     # Dynamic sizing with micro account support ($5+)
│   ├── daily_tracker.py      # Daily/weekly/monthly P&L tracking
│   ├── spread_monitor.py     # Spread spike protection
│   └── risk_reporter.py      # Aggregated risk health reporting
│
├── adaptive/                 # Statistical learning and adaptation; individual modules require live-wiring verification
│   ├── optimizer.py          # Master controller — coordinates all learners
│   ├── trade_analyzer.py     # PerformanceProfile — dissects every trade
│   ├── score_optimizer.py    # Legacy scoring-weight tuner; not the Brain's identity or sole decision authority
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

## How Apex reasons

1. **Observe.** Reconstruct live quotes, ticks, recent price action and multi-timeframe candles, plus session/event context, instrument-specific structure and the current book when those sources are available.
2. **Assess the state.** Measure trend, range, liquidity, displacement, volatility, momentum, volume, market structure, execution costs and missing/conflicting evidence.
3. **Select relevant methods.** Consult the contextual strategy-knowledge map. It can surface relevant or watch-list lenses; it cannot issue orders, and missing inputs are not invented.
4. **Develop competing hypotheses.** Native reasoning analyses evidence, detects structural relationships, generates and criticizes hypotheses. An external adviser may broaden the analysis; it is not the sole source of truth.
5. **Evaluate opportunities.** A pattern is not an opportunity and an opportunity is not an order. Apex must consider activation, invalidation, expected value after costs, time horizon, the whole book and execution feasibility.
6. **Act through common controls.** Qualified Brain decisions flow through the shared origination/execution/risk plane. Broker validity, account/portfolio constraints and capital-preservation safeguards remain mandatory.
7. **Re-evaluate and learn.** Market changes can wake reasoning between candle closes. Open campaigns are re-evaluated; outcomes are attributed and learning is promoted only after validation.

No M1 pattern, indicator, score, strategy label or time frame is a universal prerequisite. The presence of a pattern does not imply an order. Forming-candle observations are provisional and must remain distinguishable from confirmed candles.

---

## Current architecture principles

- **One market decision authority:** the CognitiveBrain interprets evidence, hypotheses, opportunities and campaign theses.
- **Specialists are observers:** FVG, order-block, structure, liquidity, volume, Wyckoff, momentum, volatility and candle-pattern modules provide measurements; overlapping observations are not independent votes.
- **Adaptive knowledge, not a fixed recipe:** the native strategy map assesses analytical lenses from current evidence and explicitly marks limitations. It is a seed for the broader redesign, not evidence that every known strategy is implemented.
- **M1 patterns are retained:** engulfing, pin bar/rejection wick, range reclaim, inside-bar breakout and volume-at-boundary observations can inform context; they do not automatically trigger entries.
- **Native reasoning plus optional advisers:** provider failure is different from a reasoned no-opportunity state. Native fallback must retain the same evidence and qualification discipline.
- **Shared execution and risk:** exposure-increasing actions must pass through the authoritative execution plane and its risk controls.
- **External-signal relay is paper-only:** the optional ChatGPT→Binance integration cannot authorize LIVE exposure independently of CognitiveBrain and whole-book admission.
- **Auditability:** decisions need a thesis, evidence provenance, uncertainty, invalidation and action/no-action reason.

---

## Quick Start

```bash
# Clone
git clone https://github.com/eugine336/apex-trader.git
cd apex-trader

# Install dependencies
pip install -r requirements.txt

# Runtime configuration note
# `.env` is currently tracked and restored exactly from `main` per operator instruction.
# Do not edit `.env` through this migration. `.env.example` is a separate sanitized template.
# Because tracked files are visible to repository readers, rotate exposed active credentials separately.

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

### Cognition and origination controls

The effective values depend on config.py plus environment overrides. Review runtime logs before starting an unattended session.

| Variable | Meaning |
|---|---|
| `COGNITION_GATE_MODE` | Default `authoritative`: stale/missing cognition cannot silently authorize a legacy exposure-increasing route |
| `COGNITION_EV_PRIMARY_GATE` | Default `true`: requires positive net expected value after estimated costs, not only confidence |
| `COGNITION_MIN_EVIDENCE_DOMAINS` | Default `3`: requires multi-domain evidence before advisor-assisted origination |
| `COGNITION_EVENT_MIN_INTERVAL_SECONDS` | Default `1`: material market changes can wake native cognition once per second per symbol; provider calls have separate throttles |
| `COGNITION_ORIGINATION_MODE` | Whether Brain-authored campaigns are off, shadow-recorded or submitted to the configured execution sink |
| `COGNITION_SINGLE_PATH` | Suppresses legacy entry routes when enabled; verify its effective runtime value before relying on it |
| `APEX_GOLD_DESK_SHADOW_ENABLED` | Enables/disables the Gold-specific observation layer; the observer itself cannot place orders |

A live origination setting does not prove that the advisor, feed, broker or strategy is healthy and does not establish profitability. Follow the rollout runbook and identity audit.

## Testing

```bash
python -m pytest tests/ -v
```

The repository contains a large legacy and current test suite. Use the latest CI run as the authority for pass/fail status; a passing focused cognition suite does not imply that the entire repository is green.

---

## Implementation limits

Apex is being redesigned toward this identity; the repository is not yet proof of exhaustive trading knowledge or production readiness. Some legacy scanner/entry utilities remain in the source tree, the native hypothesis generator is narrower than the desired target, and several analytical techniques require data a broker feed may not provide. The full repository test/lint baseline has previously shown unrelated failures, and end-to-end demo validation is still required.

Do not treat source-file presence, a successful import, or an attractive backtest as proof of a live capability. Do not promise guaranteed profit or capture of every profitable movement. Test on a demo account before using real capital.

## Disclaimer

This software is for educational and personal use.
Trading involves significant risk. Past performance does not guarantee future results.
Always test on a demo account before going live.
