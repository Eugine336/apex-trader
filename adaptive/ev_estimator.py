"""
APEX TRADER — Expected Value Estimator
Every dollar must work. Deploy capital only when expected value exceeds risk.
This module estimates EV from the system's own trade history.

EV is computed on a normalized basis:
  - Preferred: true R-multiple (pnl_dollars / risk_dollars) per trade.
  - Fallback: raw dollar P&L when initial risk is unavailable.
  - The raw 'pnl' (pips) column is never used — pip magnitudes vary
    wildly across instruments and produce meaningless pooled statistics.
"""

from dataclasses import dataclass

import numpy as np
from loguru import logger

_EV_SANITY_BOUND = 1000.0


@dataclass
class EVEstimate:
    expected_value: float
    win_rate: float
    avg_win: float
    avg_loss: float
    sample_size: int
    confidence: str
    source: str
    unit: str


class EVEstimator:

    def __init__(self, min_trades_for_gate: int = 10):
        self.min_trades_for_gate = min_trades_for_gate
        # Memoize estimates within a stable trade_history. EV is a pure function
        # of the filtered trades, which only change when a trade closes (history
        # grows). Keying the cache on the history length lets repeated per-cycle
        # estimates (same history) reuse the result instead of rescanning, and
        # auto-invalidates the moment a new trade is appended.
        self._cache: dict[tuple, EVEstimate] = {}
        self._cache_hist_len: int = -1

    def estimate(
        self,
        pair: str,
        regime: str,
        session: str,
        trade_history: list[dict],
    ) -> EVEstimate:
        hist_len = len(trade_history)
        if hist_len != self._cache_hist_len:
            self._cache.clear()
            self._cache_hist_len = hist_len
        cache_key = (pair, regime, session)
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached

        # Single pass partitions the history into the three candidate buckets
        # instead of up to three independent full scans.
        pair_trades: list[dict] = []
        regime_trades: list[dict] = []
        session_trades: list[dict] = []
        for t in trade_history:
            if t.get("pair") == pair:
                pair_trades.append(t)
            if t.get("regime") == regime:
                regime_trades.append(t)
            if t.get("session") == session:
                session_trades.append(t)

        if len(pair_trades) >= self.min_trades_for_gate:
            result = self._compute(pair_trades, "pair")
        elif len(regime_trades) >= self.min_trades_for_gate:
            result = self._compute(regime_trades, "regime")
        elif len(session_trades) >= self.min_trades_for_gate:
            result = self._compute(session_trades, "session")
        else:
            result = EVEstimate(
                expected_value=0.0,
                win_rate=0.0,
                avg_win=0.0,
                avg_loss=0.0,
                sample_size=hist_len,
                confidence="insufficient",
                source="default",
                unit="R",
            )
        self._cache[cache_key] = result
        return result

    def _compute(self, trades: list[dict], source: str) -> EVEstimate:
        r_values: list[float] = []
        usd_values: list[float] = []

        for t in trades:
            pnl_d = t.get("pnl_dollars")
            risk_d = t.get("risk_dollars")

            if risk_d is not None and risk_d > 0 and pnl_d is not None:
                r_values.append(pnl_d / risk_d)

            if pnl_d is not None and not (pnl_d == 0.0 and risk_d is None):
                usd_values.append(float(pnl_d))

        if len(r_values) >= self.min_trades_for_gate:
            values = r_values
            unit = "R"
        elif len(usd_values) >= self.min_trades_for_gate:
            values = usd_values
            unit = "USD"
        else:
            return EVEstimate(
                expected_value=0.0,
                win_rate=0.0,
                avg_win=0.0,
                avg_loss=0.0,
                sample_size=len(trades),
                confidence="insufficient",
                source=source,
                unit="R",
            )

        n = len(values)
        wins = [v for v in values if v > 0]
        losses = [abs(v) for v in values if v < 0]

        win_rate = len(wins) / n if n else 0.0
        avg_win = float(np.mean(wins)) if wins else 0.0
        avg_loss = float(np.mean(losses)) if losses else 0.0

        ev = (win_rate * avg_win) - ((1 - win_rate) * avg_loss)

        if n >= 50:
            confidence = "high"
        elif n >= 20:
            confidence = "medium"
        elif n >= 5:
            confidence = "low"
        else:
            confidence = "insufficient"

        if not np.isfinite(ev) or abs(ev) > _EV_SANITY_BOUND:
            logger.warning(
                f"EV sanity guard ({source}): EV={ev} ({unit}) exceeds bound "
                f"±{_EV_SANITY_BOUND} — neutralizing to insufficient"
            )
            return EVEstimate(
                expected_value=0.0,
                win_rate=round(win_rate, 4),
                avg_win=0.0,
                avg_loss=0.0,
                sample_size=n,
                confidence="insufficient",
                source=source,
                unit=unit,
            )

        logger.debug(
            f"EV estimate ({source}): EV={ev:.4f} {unit}, WR={win_rate:.2%}, "
            f"n={n}, confidence={confidence}"
        )

        return EVEstimate(
            expected_value=round(ev, 4),
            win_rate=round(win_rate, 4),
            avg_win=round(avg_win, 4),
            avg_loss=round(avg_loss, 4),
            sample_size=n,
            confidence=confidence,
            source=source,
            unit=unit,
        )
