"""
APEX TRADER — Execution Monitor
Signal quality means nothing without fill quality.
This module tracks latency, slippage, spread, and requotes in real time,
per-symbol and windowed to stay sensitive to recent execution changes.
"""

from collections import deque
from dataclasses import dataclass
from datetime import datetime

import numpy as np


@dataclass
class ExecutionStats:
    avg_slippage_pips: float
    max_slippage_pips: float
    avg_latency_ms: float
    spread_current: float
    spread_average: float
    spread_is_wide: bool
    requote_count: int
    execution_quality: str


_QUALITY_MULTIPLIERS: dict[str, float] = {
    "EXCELLENT": 1.0,
    "GOOD": 1.0,
    "POOR": 0.5,
    "UNACCEPTABLE": 0.25,
}


class ExecutionMonitor:
    """
    Tracks execution friction to ensure the strategy edge survives in real fills.
    Per-symbol windowed buffers keep the monitor sensitive to recent changes.
    """

    def __init__(
        self,
        pip_size: float = 0.0001,
        slippage_alert_pips: float = 1.2,
        wide_spread_multiplier: float = 1.8,
        window: int = 200,
    ):
        self.pip_size = pip_size
        self.slippage_alert_pips = slippage_alert_pips
        self.wide_spread_multiplier = wide_spread_multiplier
        self._window = max(window, 1)
        self._slippages: dict[str, deque] = {}
        self._latencies: dict[str, deque] = {}
        self._spreads: dict[str, deque] = {}
        self._requotes: dict[str, int] = {}

    def _ensure_symbol(self, symbol: str) -> None:
        if symbol not in self._slippages:
            self._slippages[symbol] = deque(maxlen=self._window)
            self._latencies[symbol] = deque(maxlen=self._window)
            self._spreads[symbol] = deque(maxlen=self._window)
            self._requotes[symbol] = 0

    def record_execution(
        self,
        requested_price: float,
        filled_price: float,
        signal_timestamp: datetime,
        fill_timestamp: datetime,
        spread: float,
        requote: bool = False,
        pip_size: float | None = None,
        symbol: str = "_global",
    ) -> None:
        actual_pip = pip_size if pip_size and pip_size > 0 else self.pip_size
        slippage_pips = abs(filled_price - requested_price) / actual_pip
        latency_ms = max(
            (fill_timestamp - signal_timestamp).total_seconds() * 1000.0, 0.0
        )

        self._ensure_symbol(symbol)
        self._slippages[symbol].append(float(slippage_pips))
        self._latencies[symbol].append(float(latency_ms))
        self._spreads[symbol].append(float(spread))
        if requote:
            self._requotes[symbol] += 1

    def _aggregate_deques(self, store: dict[str, deque]) -> list[float]:
        result: list[float] = []
        for dq in store.values():
            result.extend(dq)
        return result

    def get_stats(self, symbol: str | None = None) -> ExecutionStats:
        if symbol and symbol in self._slippages:
            slips = list(self._slippages[symbol])
            lats = list(self._latencies[symbol])
            sprs = list(self._spreads[symbol])
            reqs = self._requotes.get(symbol, 0)
        else:
            slips = self._aggregate_deques(self._slippages)
            lats = self._aggregate_deques(self._latencies)
            sprs = self._aggregate_deques(self._spreads)
            reqs = sum(self._requotes.values())

        avg_slippage = float(np.mean(slips)) if slips else 0.0
        max_slippage = float(np.max(slips)) if slips else 0.0
        avg_latency = float(np.mean(lats)) if lats else 0.0
        spread_current = float(sprs[-1]) if sprs else 0.0
        spread_average = float(np.mean(sprs)) if sprs else 0.0

        spread_is_wide = (
            spread_average > 0
            and spread_current > spread_average * self.wide_spread_multiplier
        )
        quality = self._grade_execution(avg_slippage, avg_latency, spread_is_wide)

        return ExecutionStats(
            avg_slippage_pips=round(avg_slippage, 4),
            max_slippage_pips=round(max_slippage, 4),
            avg_latency_ms=round(avg_latency, 2),
            spread_current=round(spread_current, 6),
            spread_average=round(spread_average, 6),
            spread_is_wide=spread_is_wide,
            requote_count=reqs,
            execution_quality=quality,
        )

    def should_alert(self) -> bool:
        for sym in self._slippages:
            stats = self.get_stats(symbol=sym)
            if (
                stats.avg_slippage_pips >= self.slippage_alert_pips
                or stats.execution_quality in {"POOR", "UNACCEPTABLE"}
                or stats.spread_is_wide
            ):
                return True
        return False

    def get_typical_spread(self, symbol: str) -> float | None:
        if symbol not in self._spreads or not self._spreads[symbol]:
            return None
        return float(np.mean(list(self._spreads[symbol])))

    def get_size_multiplier(self, symbol: str) -> float:
        if symbol not in self._slippages or not self._slippages[symbol]:
            return 1.0
        stats = self.get_stats(symbol=symbol)
        return _QUALITY_MULTIPLIERS.get(stats.execution_quality, 1.0)

    def _grade_execution(
        self, avg_slippage: float, avg_latency: float, spread_is_wide: bool
    ) -> str:
        if spread_is_wide or avg_slippage > 1.5 or avg_latency > 750:
            return "UNACCEPTABLE"
        if avg_slippage > 1.0 or avg_latency > 450:
            return "POOR"
        if avg_slippage > 0.5 or avg_latency > 220:
            return "GOOD"
        return "EXCELLENT"
