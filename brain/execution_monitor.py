"""
APEX TRADER — Execution Monitor
Signal quality means nothing without fill quality.
This module tracks latency, slippage, spread, and requotes in real time.
"""

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


class ExecutionMonitor:
    """
    Tracks execution friction to ensure the strategy edge survives in real fills.
    """

    def __init__(
        self,
        pip_size: float = 0.0001,
        slippage_alert_pips: float = 1.2,
        wide_spread_multiplier: float = 1.8,
    ):
        self.pip_size = pip_size
        self.slippage_alert_pips = slippage_alert_pips
        self.wide_spread_multiplier = wide_spread_multiplier
        self._slippages: list[float] = []
        self._latencies: list[float] = []
        self._spreads: list[float] = []
        self._requotes = 0

    def record_execution(
        self,
        requested_price: float,
        filled_price: float,
        signal_timestamp: datetime,
        fill_timestamp: datetime,
        spread: float,
        requote: bool = False,
    ) -> None:
        slippage_pips = abs(filled_price - requested_price) / self.pip_size
        latency_ms = max(
            (fill_timestamp - signal_timestamp).total_seconds() * 1000.0, 0.0
        )

        self._slippages.append(float(slippage_pips))
        self._latencies.append(float(latency_ms))
        self._spreads.append(float(spread))
        if requote:
            self._requotes += 1

    def get_stats(self) -> ExecutionStats:
        avg_slippage = float(np.mean(self._slippages)) if self._slippages else 0.0
        max_slippage = float(np.max(self._slippages)) if self._slippages else 0.0
        avg_latency = float(np.mean(self._latencies)) if self._latencies else 0.0
        spread_current = float(self._spreads[-1]) if self._spreads else 0.0
        spread_average = float(np.mean(self._spreads)) if self._spreads else 0.0

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
            requote_count=self._requotes,
            execution_quality=quality,
        )

    def should_alert(self) -> bool:
        stats = self.get_stats()
        return (
            stats.avg_slippage_pips >= self.slippage_alert_pips
            or stats.execution_quality in {"POOR", "UNACCEPTABLE"}
            or stats.spread_is_wide
        )

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
