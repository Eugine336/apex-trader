"""
APEX TRADER — Market Data Utilities
Canonical helpers for the closed-bar contract: signal/entry logic must
operate on CLOSED bars only, never on the still-forming current bar.
"""

import pandas as pd


def drop_forming_bar(df: pd.DataFrame) -> pd.DataFrame:
    """Return a view of *df* with the last (forming) row removed.

    All broker connectors fetch OHLCV starting from the live bar
    (MT5 ``copy_rates_from_pos(..., 0, count)``; Deriv ``end: "latest"``),
    so ``iloc[-1]`` is always the incomplete, in-progress candle.

    Signal-generation code (BOS/CHOCH detection, candle-pattern matching)
    must call this before reading ``iloc[-1]`` so that decisions are based
    on confirmed, closed candles — eliminating repaint / look-ahead.

    Proximity checks that use ``close.iloc[-1]`` as a *live price proxy*
    may legitimately keep the forming bar.
    """
    if len(df) <= 1:
        return df
    return df.iloc[:-1]
