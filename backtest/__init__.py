"""APEX TRADER — Backtest Harness (P0).

A historical-replay engine that exercises the REAL trading components against
recorded or synthetic market data. The harness does NOT reimplement trading
logic: it replaces the live broker connector with a faithful
:class:`~backtest.broker.SimulatedBroker` (implementing the same
``BaseConnector`` interface the live system uses) and drives the real adaptive
layers — the L7 :class:`~adaptive.regime_detector.RegimeDetector` and the L8
:class:`~adaptive.risk_manager.RiskManager` — exactly as production would.

Design constraints honoured here:

* **No lookahead bias** — the SimulatedBroker only ever exposes candles up to
  the current replay cursor; future bars are never visible to a strategy.
* **Reproducible** — all randomness (slippage) is driven by a seeded RNG, so
  the same data + config + seed always produces identical results.
* **Production-safe** — nothing in this package touches a live broker or a
  production database. SQLite-backed adaptive layers are pointed at temp paths.

The public entry points are re-exported from :mod:`backtest.engine`.
"""

from backtest.engine import (  # noqa: F401
    BacktestReporter,
    BacktestResults,
    BacktestRunner,
    Candle,
    HistoricalDataLoader,
    Signal,
    SimulatedBroker,
    TradeRecord,
)

__all__ = [
    "BacktestReporter",
    "BacktestResults",
    "BacktestRunner",
    "Candle",
    "HistoricalDataLoader",
    "Signal",
    "SimulatedBroker",
    "TradeRecord",
]
