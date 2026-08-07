"""APEX TRADER — M1 candle micro-pattern detection.

Detects the fast price-action confirmation patterns the live MARKET fast-path
looks for (engulfing / pin bar) on the most recent CLOSED M1 candles, deriving
``micro_confirmation`` from candle geometry.

Pure over candle data — no broker, no engine, no threads.  Returns ``""`` on
any missing/short/degenerate data so a thin feed never raises or changes
behaviour.
"""

from __future__ import annotations

from typing import Any

# A pin bar's body must be no larger than this fraction of the full range, and
# its rejection wick at least this multiple of the body, to qualify.
_PIN_BODY_MAX_FRAC = 0.34
_PIN_WICK_BODY_MULT = 2.0


def detect_m1_pattern(m1_df: Any, is_long: bool) -> str:
    """Return a micro-confirmation pattern for the last closed M1 candle.

    ``"engulfing"`` — the last candle's body fully engulfs the prior candle's
                      body in the trade direction (bullish engulfing for longs,
                      bearish engulfing for shorts).
    ``"pin_bar"``   — a small-bodied candle with a long rejection wick against
                      the trade direction (hammer for longs, shooting star for
                      shorts).
    ``""``          — no qualifying pattern (unchanged PENDING behaviour).

    Expects a frame with ``open``/``high``/``low``/``close`` columns where the
    last row is the most recent CLOSED candle.
    """
    try:
        if m1_df is None or len(m1_df) < 2:
            return ""
        o = m1_df["open"].values
        h = m1_df["high"].values
        low = m1_df["low"].values
        c = m1_df["close"].values
        o1, h1, l1, c1 = float(o[-1]), float(h[-1]), float(low[-1]), float(c[-1])
        o0, c0 = float(o[-2]), float(c[-2])
    except Exception:  # noqa: BLE001 — degenerate data never raises here
        return ""

    body = abs(c1 - o1)
    rng = h1 - l1
    if rng <= 0:
        return ""

    # ── Engulfing: current body reverses then overtakes the prior body ──
    if is_long:
        if c1 > o1 and c0 < o0 and c1 >= o0 and o1 <= c0 and body > 0:
            return "engulfing"
    else:
        if c1 < o1 and c0 > o0 and c1 <= o0 and o1 >= c0 and body > 0:
            return "engulfing"

    # ── Pin bar / hammer: small body, long rejection wick against direction ──
    upper_wick = h1 - max(o1, c1)
    lower_wick = min(o1, c1) - l1
    small_body = body <= _PIN_BODY_MAX_FRAC * rng
    if (
        is_long
        and small_body
        and lower_wick >= _PIN_WICK_BODY_MULT * body
        and lower_wick > upper_wick
    ):
        return "pin_bar"
    if (
        not is_long
        and small_body
        and upper_wick >= _PIN_WICK_BODY_MULT * body
        and upper_wick > lower_wick
    ):
        return "pin_bar"

    return ""
