"""
APEX TRADER — Expected Value Estimator
Every dollar must work. Deploy capital only when expected value exceeds risk.
This module estimates EV from the system's own trade history.
"""

from dataclasses import dataclass

import numpy as np
from loguru import logger


@dataclass
class EVEstimate:
    expected_value: float
    win_rate: float
    avg_win_r: float
    avg_loss_r: float
    sample_size: int
    confidence: str
    source: str


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
            avg_win_r=0.0,
            avg_loss_r=0.0,
            sample_size=len(trade_history),
            confidence="insufficient",
            source="default",
        )

    def _compute(self, trades: list[dict], source: str) -> EVEstimate:
        pnls = [float(t.get("pnl", 0)) for t in trades]
        n = len(pnls)
        wins = [p for p in pnls if p > 0]
        losses = [abs(p) for p in pnls if p < 0]

        win_rate = len(wins) / n if n else 0.0
        avg_win_r = float(np.mean(wins)) if wins else 0.0
        avg_loss_r = float(np.mean(losses)) if losses else 0.0

        ev = (win_rate * avg_win_r) - ((1 - win_rate) * avg_loss_r)

        if n >= 50:
            confidence = "high"
        elif n >= 20:
            confidence = "medium"
        elif n >= 5:
            confidence = "low"
        else:
            confidence = "insufficient"

        logger.debug(
            f"EV estimate ({source}): EV={ev:.4f}, WR={win_rate:.2%}, "
            f"n={n}, confidence={confidence}"
        )

        return EVEstimate(
            expected_value=round(ev, 4),
            win_rate=round(win_rate, 4),
            avg_win_r=round(avg_win_r, 4),
            avg_loss_r=round(avg_loss_r, 4),
            sample_size=n,
            confidence=confidence,
            source=source,
        )
