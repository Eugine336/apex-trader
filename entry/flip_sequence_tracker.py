"""APEX TRADER — Fast-then-slow flip sequencing (flip confirmation Check 7).

Stateful tracker that enforces a TEMPORAL ordering on a regime flip: the fast
timeframe (M1) must shift into the flip direction FIRST, and only THEN — on a
LATER slow-timeframe (M5) close within a bounded window — does the slow
timeframe confirm. It is the seventh gate of the hardened flip confirmation
(entry/flip_confirmer.py).

The key insight: both timeframes flipping *simultaneously* is usually noise (a
single violent candle drags M1 and M5 at once); a genuine regime change shows
up as the fast timeframe leading and the slow timeframe catching up shortly
after. Requiring "M1 first, then M5 on a subsequent bar" filters out the
simultaneous case while still reacting quickly to real reversals.

State machine, per symbol::

    IDLE ──(M1 confirms)──▶ FAST_CONFIRMED ──(later M5 confirms)──▶ FULLY_CONFIRMED
      ▲                          │
      └──────(timeout)───────────┘

Transitions:
  * ``on_m1_close`` — evaluates M1 momentum (via the existing static
    ``M1CandleConfirmer._check_momentum``). A confirmation from IDLE arms
    FAST_CONFIRMED and stamps the current M5 bar; a confirmation for the OPP.
    direction re-arms in the new direction.
  * ``on_m5_close`` — advances the per-symbol M5 bar clock by exactly one and,
    when the M5 trend matches the armed direction on a bar STRICTLY LATER than
    the one FAST_CONFIRMED was stamped on, promotes to FULLY_CONFIRMED. An M5
    trend that agrees on the SAME bar the fast confirmed (simultaneous) does not
    promote. An M5 trend arriving from IDLE never promotes.
  * ``check_timeouts`` — resets FAST_CONFIRMED back to IDLE once
    ``flip_sequence_window_bars`` M5 bars have elapsed with no slow confirmation.
  * ``reset`` — clears a symbol's confirmation state (called after a flip fires
    or a fresh zone touch), leaving the monotonic M5 bar clock intact.

The tracker is thread-safe (a single lock guards all state) and fully guarded —
a faulty momentum read or trend string degrades to "no confirmation", never a
raised exception, so it can only ever make a flip MORE conservative.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Callable, Optional

import pandas as pd
from loguru import logger

from brain.instrument_profile import get_profile
from entry.m1_confirmation import M1CandleConfirmer

# ── State constants ────────────────────────────────────────────────────────
IDLE = "IDLE"
FAST_CONFIRMED = "FAST_CONFIRMED"
FULLY_CONFIRMED = "FULLY_CONFIRMED"

# Default number of M5 bars to wait for slow confirmation after the fast
# timeframe confirmed (3 bars = 15 minutes) — mirrors the InstrumentProfile
# ``flip_sequence_window_bars`` default, used when no profile value resolves.
_DEFAULT_WINDOW_BARS = 3

# Default number of the last 5 M1 candles that must align for the fast
# momentum shift to count (matches M1CandleConfirmer's retuned default).
_DEFAULT_MIN_ALIGNED = 2


@dataclass
class _SymbolState:
    """Mutable per-symbol confirmation state."""

    state: str = IDLE
    direction: str = ""  # "LONG" / "SHORT" the fast timeframe armed on
    fast_m5_bar: int = 0  # M5-bar index at which FAST_CONFIRMED was stamped


def _norm_direction(direction: str) -> str:
    return str(direction or "").upper()


class FlipSequenceTracker:
    """Per-symbol fast-then-slow flip sequencing state machine."""

    def __init__(
        self,
        *,
        pip_size_lookup: Optional[Callable[[str], float]] = None,
        window_bars: int = _DEFAULT_WINDOW_BARS,
        min_aligned: int = _DEFAULT_MIN_ALIGNED,
    ) -> None:
        self._pip_size = pip_size_lookup or (lambda _s: 0.0001)
        self._default_window = max(1, int(window_bars))
        self._min_aligned = int(min_aligned)
        self._state: dict[str, _SymbolState] = {}
        # Monotonic per-symbol M5-bar clock — advanced once per ``on_m5_close``.
        self._m5_bar: dict[str, int] = {}
        self._lock = threading.Lock()

    # ── Public API ────────────────────────────────────────────────────
    def on_m1_close(
        self,
        symbol: str,
        direction: str,
        m1_df: Optional[pd.DataFrame],
    ) -> str:
        """Feed an M1 candle close; arm FAST_CONFIRMED when momentum shifts.

        Returns the resulting state string for the symbol. A no-confirmation M1
        close is a no-op (the timeout path handles staleness). Called on every
        M1 close for each active/recent flip direction.
        """
        want = _norm_direction(direction)
        with self._lock:
            st = self._get(symbol)
            if want not in ("LONG", "SHORT"):
                return st.state
            if not self._m1_confirms(symbol, want, m1_df):
                return st.state

            bar = self._m5_bar.get(symbol, 0)
            if st.state == IDLE:
                st.state = FAST_CONFIRMED
                st.direction = want
                st.fast_m5_bar = bar
            elif st.state == FAST_CONFIRMED:
                # A fast shift into the OPPOSITE direction supersedes the armed
                # one (re-stamp); same direction simply stays armed.
                if st.direction != want:
                    st.direction = want
                    st.fast_m5_bar = bar
            elif st.state == FULLY_CONFIRMED:
                # Already fully confirmed one way; a fast shift the OTHER way
                # restarts the sequence for the new direction.
                if st.direction != want:
                    st.state = FAST_CONFIRMED
                    st.direction = want
                    st.fast_m5_bar = bar
            return st.state

    def on_m5_close(
        self,
        symbol: str,
        direction: str,
        m5_trend: str,
    ) -> str:
        """Feed an M5 candle close; promote to FULLY_CONFIRMED when the slow
        timeframe confirms the armed direction on a LATER bar.

        ``direction`` is the flip direction under evaluation; pass an empty
        string to defer to the armed direction. ``m5_trend`` is the M5 structure
        trend ("BULLISH"/"BEARISH"/"RANGING"/…) read from the WorldModel.
        Advances the per-symbol M5-bar clock by exactly one, so this MUST be
        called once per M5 close per symbol. Returns the resulting state.
        """
        want = _norm_direction(direction)
        with self._lock:
            st = self._state.get(symbol)
            bar = self._m5_bar.get(symbol, 0)
            if st is not None and st.state == FAST_CONFIRMED:
                # The passed direction (when given) must match the armed one;
                # the M5 trend must agree; and this must be a STRICTLY LATER M5
                # bar than the fast confirmation (same-bar = simultaneous noise).
                if (
                    (not want or want == st.direction)
                    and self._trend_matches(m5_trend, st.direction)
                    and bar > st.fast_m5_bar
                ):
                    st.state = FULLY_CONFIRMED
            # Advance the monotonic M5-bar clock exactly once per close.
            self._m5_bar[symbol] = bar + 1
            return st.state if st is not None else IDLE

    def is_confirmed(self, symbol: str, direction: str) -> bool:
        """Return True when ``symbol`` is FULLY_CONFIRMED in ``direction``."""
        want = _norm_direction(direction)
        with self._lock:
            st = self._state.get(symbol)
            return st is not None and st.state == FULLY_CONFIRMED and st.direction == want

    def check_timeouts(self, symbol: str) -> None:
        """Reset FAST_CONFIRMED → IDLE once the slow-confirmation window lapses.

        Idempotent and safe to call every candle close. Once
        ``flip_sequence_window_bars`` M5 bars have elapsed since FAST_CONFIRMED
        was stamped with no promotion, the arming is discarded (a fast shift
        that the slow timeframe never confirmed was just volatility).
        """
        with self._lock:
            st = self._state.get(symbol)
            if st is None or st.state != FAST_CONFIRMED:
                return
            window = self._window(symbol)
            bar = self._m5_bar.get(symbol, 0)
            if (bar - st.fast_m5_bar) >= window:
                self._clear(symbol)

    def reset(self, symbol: str) -> None:
        """Clear a symbol's confirmation state (after a flip fires / zone touch).

        The monotonic M5-bar clock is intentionally preserved so re-arming after
        a reset still requires a strictly-later slow confirmation.
        """
        with self._lock:
            self._clear(symbol)

    def state_of(self, symbol: str) -> str:
        """Return the current state string for ``symbol`` (IDLE when unseen)."""
        with self._lock:
            st = self._state.get(symbol)
            return st.state if st is not None else IDLE

    def reset_all(self) -> None:
        """Clear ALL symbol state and bar clocks (e.g. between backtest runs)."""
        with self._lock:
            self._state.clear()
            self._m5_bar.clear()

    # ── Internals ─────────────────────────────────────────────────────
    def _get(self, symbol: str) -> _SymbolState:
        st = self._state.get(symbol)
        if st is None:
            st = _SymbolState()
            self._state[symbol] = st
        return st

    def _clear(self, symbol: str) -> None:
        st = self._state.get(symbol)
        if st is not None:
            st.state = IDLE
            st.direction = ""
            st.fast_m5_bar = 0

    def _m1_confirms(
        self,
        symbol: str,
        direction: str,
        m1_df: Optional[pd.DataFrame],
    ) -> bool:
        """True when M1 momentum has shifted to ``direction`` on ``m1_df``.

        Reuses the existing static momentum detector so the fast-confirmation
        vocabulary is identical to the live M1 confirmation path. ``zone=None``
        skips the zone-proximity gate — this is a standalone momentum read.
        """
        if m1_df is None:
            return False
        try:
            pip = float(self._pip_size(symbol) or 0.0001)
        except Exception:  # noqa: BLE001 — pip lookup must never break tracking
            pip = 0.0001
        try:
            res = M1CandleConfirmer._check_momentum(
                m1_df,
                direction,
                None,
                pip,
                min_aligned=self._min_aligned,
            )
            return bool(res.confirmed)
        except Exception as exc:  # noqa: BLE001 — a faulty read never confirms
            logger.debug("[flip-seq] {} M1 momentum read failed: {}", symbol, exc)
            return False

    def _window(self, symbol: str) -> int:
        """Resolve ``flip_sequence_window_bars`` per instrument, then default."""
        try:
            prof = get_profile(symbol)
            val = getattr(prof, "flip_sequence_window_bars", None)
            if val is not None:
                return max(1, int(val))
        except Exception:  # noqa: BLE001 — tuning lookup must never break tracking
            pass
        return self._default_window

    @staticmethod
    def _trend_matches(trend: str, direction: str) -> bool:
        """True when an M5 trend string agrees with a LONG/SHORT direction."""
        t = str(trend or "").upper()
        if direction == "LONG":
            return t in ("BULLISH", "LONG", "UP")
        if direction == "SHORT":
            return t in ("BEARISH", "SHORT", "DOWN")
        return False
