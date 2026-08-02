"""Tests for the fast-then-slow flip sequence tracker (entry/flip_sequence_tracker.py).

Exercises the IDLE → FAST_CONFIRMED → FULLY_CONFIRMED state machine: M1 must
confirm first and M5 must confirm on a LATER bar (simultaneous flips are noise),
the slow-confirmation timeout, reset, and per-symbol independence.
"""

import pandas as pd

from entry.flip_sequence_tracker import FAST_CONFIRMED, FULLY_CONFIRMED, IDLE, FlipSequenceTracker


# ── M1 momentum frame builders ──────────────────────────────────────────────
def _bull_m1(n=6):
    """An M1 frame of ``n`` bullish candles (close > open) — confirms LONG.

    ``drop_forming_bar`` removes the last row, leaving ``n-1`` closed bullish
    bars for the momentum check (needs ≥5 closed).
    """
    return pd.DataFrame(
        {
            "open": [100.0 + i for i in range(n)],
            "high": [100.6 + i for i in range(n)],
            "low": [99.9 + i for i in range(n)],
            "close": [100.4 + i for i in range(n)],
        }
    )


def _bear_m1(n=6):
    """An M1 frame of ``n`` bearish candles (close < open) — confirms SHORT."""
    return pd.DataFrame(
        {
            "open": [110.0 - i for i in range(n)],
            "high": [110.6 - i for i in range(n)],
            "low": [109.4 - i for i in range(n)],
            "close": [109.6 - i for i in range(n)],
        }
    )


SYM = "XAUUSD"


class TestFastThenSlow:
    def test_m1_first_then_m5_later_bar_fully_confirms(self):
        t = FlipSequenceTracker(window_bars=3)
        assert t.on_m1_close(SYM, "LONG", _bull_m1()) == FAST_CONFIRMED
        # First M5 close completes the SAME bar the fast armed on → no promote.
        t.on_m5_close(SYM, "LONG", "BULLISH")
        assert t.state_of(SYM) == FAST_CONFIRMED
        # A LATER M5 bar confirming the direction → FULLY_CONFIRMED.
        t.on_m5_close(SYM, "LONG", "BULLISH")
        assert t.is_confirmed(SYM, "LONG") is True
        assert t.state_of(SYM) == FULLY_CONFIRMED

    def test_m5_first_without_m1_stays_idle(self):
        t = FlipSequenceTracker(window_bars=3)
        t.on_m5_close(SYM, "LONG", "BULLISH")
        assert t.state_of(SYM) == IDLE
        assert t.is_confirmed(SYM, "LONG") is False

    def test_m1_and_m5_same_bar_stays_fast_only(self):
        # Both confirm within the SAME M5 bar (simultaneous) → not fully confirmed.
        t = FlipSequenceTracker(window_bars=3)
        t.on_m1_close(SYM, "LONG", _bull_m1())
        t.on_m5_close(SYM, "LONG", "BULLISH")  # completes the fast's own bar
        assert t.state_of(SYM) == FAST_CONFIRMED
        assert t.is_confirmed(SYM, "LONG") is False

    def test_opposing_m5_does_not_confirm(self):
        t = FlipSequenceTracker(window_bars=3)
        t.on_m1_close(SYM, "LONG", _bull_m1())
        t.on_m5_close(SYM, "LONG", "BEARISH")  # bar 0 (same) — irrelevant anyway
        t.on_m5_close(SYM, "LONG", "BEARISH")  # later bar but wrong direction
        assert t.is_confirmed(SYM, "LONG") is False
        assert t.state_of(SYM) == FAST_CONFIRMED


class TestTimeout:
    def test_fast_times_out_after_window_bars(self):
        t = FlipSequenceTracker(window_bars=3)
        assert t.on_m1_close(SYM, "LONG", _bull_m1()) == FAST_CONFIRMED
        for _ in range(2):
            t.on_m5_close(SYM, "LONG", "RANGING")
            t.check_timeouts(SYM)
            assert t.state_of(SYM) == FAST_CONFIRMED
        # Third M5 bar with no slow confirmation → the arming lapses.
        t.on_m5_close(SYM, "LONG", "RANGING")
        t.check_timeouts(SYM)
        assert t.state_of(SYM) == IDLE
        assert t.is_confirmed(SYM, "LONG") is False


class TestResetAndIsolation:
    def test_reset_clears_state(self):
        t = FlipSequenceTracker(window_bars=3)
        t.on_m1_close(SYM, "LONG", _bull_m1())
        t.on_m5_close(SYM, "LONG", "BULLISH")
        t.on_m5_close(SYM, "LONG", "BULLISH")
        assert t.is_confirmed(SYM, "LONG") is True
        t.reset(SYM)
        assert t.state_of(SYM) == IDLE
        assert t.is_confirmed(SYM, "LONG") is False

    def test_multiple_symbols_tracked_independently(self):
        t = FlipSequenceTracker(window_bars=3)
        t.on_m1_close("XAUUSD", "LONG", _bull_m1())
        t.on_m1_close("EURUSD", "SHORT", _bear_m1())
        for sym, trend in (("XAUUSD", "BULLISH"), ("EURUSD", "BEARISH")):
            t.on_m5_close(sym, "", trend)  # same-bar close (no promote)
            t.on_m5_close(sym, "", trend)  # later bar → promote
        assert t.is_confirmed("XAUUSD", "LONG") is True
        assert t.is_confirmed("EURUSD", "SHORT") is True
        assert t.is_confirmed("XAUUSD", "SHORT") is False
        assert t.is_confirmed("EURUSD", "LONG") is False

    def test_opposite_m1_shift_rearms_direction(self):
        t = FlipSequenceTracker(window_bars=5)
        t.on_m1_close(SYM, "LONG", _bull_m1())
        assert t.state_of(SYM) == FAST_CONFIRMED
        # A fresh fast shift the other way supersedes the armed direction.
        t.on_m1_close(SYM, "SHORT", _bear_m1())
        assert t.state_of(SYM) == FAST_CONFIRMED
        t.on_m5_close(SYM, "SHORT", "BEARISH")  # same bar as the re-arm
        t.on_m5_close(SYM, "SHORT", "BEARISH")  # later bar → confirm SHORT
        assert t.is_confirmed(SYM, "SHORT") is True
        assert t.is_confirmed(SYM, "LONG") is False


class TestNoMomentum:
    def test_flat_m1_does_not_arm(self):
        # Flat candles (close == open) carry no momentum → stays IDLE.
        flat = pd.DataFrame(
            {
                "open": [100.0] * 6,
                "high": [100.5] * 6,
                "low": [99.5] * 6,
                "close": [100.0] * 6,
            }
        )
        t = FlipSequenceTracker(window_bars=3)
        assert t.on_m1_close(SYM, "LONG", flat) == IDLE
        assert t.state_of(SYM) == IDLE

    def test_none_frame_is_noop(self):
        t = FlipSequenceTracker(window_bars=3)
        assert t.on_m1_close(SYM, "LONG", None) == IDLE
