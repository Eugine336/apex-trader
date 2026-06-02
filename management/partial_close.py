"""
APEX TRADER — Partial Close Calculator
50% off at TP1. Clean. Precise. Lock the profit.
The remaining 50% runs with a free ride.

Platform-aware:
  MT5   — partial close reduces lots; pnl = pips × pip_value × lots
  Deriv — no partial close; pnl at TP1 = stake × price_move% × multiplier
"""



class PartialCloseCalculator:
    """Handles the math of partial closes and breakeven levels."""

    @staticmethod
    def calculate_partial(
        total_lots: float,
        close_ratio: float = 0.5,
    ) -> tuple[float, float]:
        """
        Returns (lots_to_close, lots_remaining).
        Rounds to 2 decimal places (minimum lot increment).
        Only called for MT5 — Deriv takes the close+reopen path in main_loop.
        """
        lots_to_close = round(total_lots * close_ratio, 2)
        lots_remaining = round(total_lots - lots_to_close, 2)
        return lots_to_close, lots_remaining

    @staticmethod
    def calculate_pnl_at_partial(
        entry_price: float,
        tp1_price: float,
        lots_closed: float,
        pip_size: float,
        pip_value: float = 10.0,
        # Deriv parameters
        stake_usd: float = 0.0,
        multiplier: int = 100,
        sizing_mode: str = "lots",
    ) -> float:
        """
        Dollar P&L from the partial close.

        MT5  (sizing_mode='lots'):  pips × pip_value × lots
        Deriv (sizing_mode='stake'): stake × (Δprice / entry) × multiplier
        """
        if sizing_mode == "stake" and stake_usd > 0 and entry_price > 0:
            price_move_pct = abs(tp1_price - entry_price) / entry_price
            return round(stake_usd * price_move_pct * multiplier, 2)

        # MT5 lot-based path
        pips = abs(tp1_price - entry_price) / pip_size if pip_size > 0 else 0.0
        return round(pips * pip_value * lots_closed, 2)

    @staticmethod
    def calculate_breakeven_level(
        entry_price: float,
        direction: str,
        buffer_pips: float,
        pip_size: float,
    ) -> float:
        """Exact breakeven price including a small buffer above/below entry."""
        if direction == "LONG":
            return round(entry_price + buffer_pips * pip_size, 5)
        return round(entry_price - buffer_pips * pip_size, 5)
