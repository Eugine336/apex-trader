"""Backtest engine — public API aggregator.

Re-exports the harness's building blocks so callers can simply do
``from backtest.engine import BacktestRunner``. The implementation is split
across :mod:`backtest.data`, :mod:`backtest.broker`, :mod:`backtest.strategy`,
:mod:`backtest.results` and :mod:`backtest.runner` for clarity.
"""

from __future__ import annotations

from backtest.broker import ClosedFill, SimulatedBroker, SimulatedPosition
from backtest.data import Candle, DataValidationError, HistoricalDataLoader, validate_candles
from backtest.results import BacktestReporter, BacktestResults, TradeRecord
from backtest.runner import BacktestRunner
from backtest.strategy import (
    BarContext,
    MovingAverageCrossStrategy,
    Signal,
    Strategy,
)

__all__ = [
    "BacktestRunner",
    "BacktestResults",
    "BacktestReporter",
    "SimulatedBroker",
    "SimulatedPosition",
    "ClosedFill",
    "HistoricalDataLoader",
    "Candle",
    "DataValidationError",
    "validate_candles",
    "TradeRecord",
    "Signal",
    "Strategy",
    "BarContext",
    "MovingAverageCrossStrategy",
]
