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

    def estimate(
        self,
        pair: str,
        regime: str,
        session: str,
        trade_history: list[dict],
    ) -> EVEstimate:
        pair_trades = [t for t in trade_history if t.get("pair") == pair]
        if len(pair_trades) >= self.min_trades_for_gate:
            return self._compute(pair_trades, "pair")

        regime_trades = [t for t in trade_history if t.get("regime") == regime]
        if len(regime_trades) >= self.min_trades_for_gate:
            return self._compute(regime_trades, "regime")

        session_trades = [t for t in trade_history if t.get("session") == session]
        if len(session_trades) >= self.min_trades_for_gate:
            return self._compute(session_trades, "session")

        return EVEstimate(
            expected_value=0.0,
            win_rate=0.0,
            avg_win=0.0,
            avg_loss=0.0,
            sample_size=len(trade_history),
            confidence="insufficient",
            source="default",
            unit="R",
        )

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
