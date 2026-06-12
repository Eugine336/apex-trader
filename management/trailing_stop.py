"""
APEX TRADER — Structure-Based Trailing Stop
The trail follows structure, not fixed pips.
For longs: SL follows the latest higher-low.
For shorts: SL follows the latest lower-high.
This is how a professional trails — by reading the market.
"""

from typing import Optional

import pandas as pd

from brain.structure_engine import StructureEngine


class StructureTrailingStop:
    """
    Calculates trailing stop levels based on swing structure on M5.
    Only ever moves the stop in the profit direction — never backwards.
    """

    def __init__(
        self,
        buffer_pips: float = 3.0,
        min_pips_to_trail: float = 10.0,
        swing_lookback: int = 12,
    ):
        self.buffer_pips = buffer_pips
        self.min_pips_to_trail = min_pips_to_trail
        # P13: a 3-bar lookback trails on intracandle noise (~15min of M5).
        # 12 bars ≈ 1h of M5 structure, giving runners room to reach H1 swings.
        self._engine = StructureEngine(swing_lookback=swing_lookback)

    def calculate_trail(
        self,
        direction: str,
        current_stop: float,
        df_m5: pd.DataFrame,
        pip_size: float,
        buffer_pips: Optional[float] = None,
    ) -> Optional[float]:
        """
        Find the best trailing stop from recent M5 structure.
        Returns a new stop level only if it improves on *current_stop*.
        Returns None when no improvement is found.
        """
        buf = buffer_pips if buffer_pips is not None else self.buffer_pips

        analysis = self._engine.analyze(df_m5)
        if analysis.swing_low is None and analysis.swing_high is None:
            return None

        if direction == "LONG":
            return self._trail_long(analysis, current_stop, pip_size, buf)
        return self._trail_short(analysis, current_stop, pip_size, buf)

    def should_trail(self, pnl_pips: float, breakeven_active: bool) -> bool:
        return breakeven_active and pnl_pips >= self.min_pips_to_trail

    # ------------------------------------------------------------------

    def _trail_long(self, analysis, current_stop, pip_size, buf) -> Optional[float]:
        lows = [s for s in analysis.bullish_swing_points if s.kind == "HL"]
        all_lows = lows or [
            s for s in analysis.bearish_swing_points if s.kind == "LL"
        ]
        if not all_lows:
            return None
        best_low = max(all_lows, key=lambda s: s.price)
        candidate = best_low.price - buf * pip_size
        if candidate > current_stop:
            return round(candidate, 5)
        return None

    def _trail_short(self, analysis, current_stop, pip_size, buf) -> Optional[float]:
        highs = [s for s in analysis.bearish_swing_points if s.kind == "LH"]
        all_highs = highs or [
            s for s in analysis.bullish_swing_points if s.kind == "HH"
        ]
        if not all_highs:
            return None
        best_high = min(all_highs, key=lambda s: s.price)
        candidate = best_high.price + buf * pip_size
        if candidate < current_stop:
            return round(candidate, 5)
        return None
