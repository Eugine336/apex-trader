"""APEX TRADER — M1 Candle Confirmation (Phase 7).

After a zone touch is detected, waits for the next M1 candle close
and checks for structural confirmation:

    1. CHoCH (Change of Character) on M1 in the trade direction.
    2. BOS (Break of Structure) on M1 in the trade direction.
    3. Momentum confirmation: ≥3/5 candles aligned + volume filter.

Logic extracted from ``trigger/entry_engine.py`` _detect_m1_choch()
and _detect_momentum_confirmation().  Uses the same StructureEngine
with M1_SWING_LOOKBACK = 3 (7-bar window) for M1 micro-structure.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import pandas as pd
from loguru import logger

from brain.market_data_utils import drop_forming_bar
from brain.structure_engine import StructureEngine, StructureEvent, Trend
from entry.models import EntryConfig, EntryZone

M1_SWING_LOOKBACK = 3
M1_DEFAULT_BARS = 100


@dataclass(frozen=True)
class ConfirmationResult:
    """Outcome of M1 candle-close confirmation."""

    confirmed: bool
    method: str  # "choch", "bos", "choch_level", "momentum", "none"
    reason: str
    candles_used: int = 0


class M1CandleConfirmer:
    """Waits for M1 candle close and runs confirmation checks."""

    def __init__(
        self,
        config: Optional[EntryConfig] = None,
        pip_size_lookup: Optional[callable] = None,
    ) -> None:
        self._config = config or EntryConfig()
        self._pip_size_lookup = pip_size_lookup or (lambda _s: 0.0001)
        self._pending_candle_counts: dict[str, int] = {}

    def on_m1_close(
        self,
        symbol: str,
        direction: str,
        zone: EntryZone,
        m1_df: pd.DataFrame,
    ) -> ConfirmationResult:
        """Check M1 confirmation after a candle close.

        Returns ConfirmationResult.confirmed=True if any confirmation
        pattern is detected, or expired/rejected if the timeout is reached.
        """
        count = self._pending_candle_counts.get(symbol, 0) + 1
        self._pending_candle_counts[symbol] = count

        timeout = self._config.m1_confirmation_timeout_candles
        if count >= timeout:
            self.clear(symbol)
            return ConfirmationResult(
                confirmed=False,
                method="none",
                reason=f"M1 timeout after {timeout} candles",
                candles_used=count,
            )

        if m1_df is None or len(m1_df) < self._config.m1_min_bars:
            return ConfirmationResult(
                confirmed=False,
                method="none",
                reason=f"Insufficient M1 bars ({0 if m1_df is None else len(m1_df)})",
                candles_used=count,
            )

        pip_size = self._pip_size_lookup(symbol)
        result = self._check_structure(m1_df, direction, pip_size)
        if result.confirmed:
            self.clear(symbol)
            logger.info(
                "[m1-confirm] {} {} confirmed via {} ({})",
                symbol, direction, result.method, result.reason,
            )
            return ConfirmationResult(
                confirmed=True,
                method=result.method,
                reason=result.reason,
                candles_used=count,
            )

        result = self._check_momentum(m1_df, direction, zone, pip_size)
        if result.confirmed:
            self.clear(symbol)
            logger.info(
                "[m1-confirm] {} {} momentum confirmed ({})",
                symbol, direction, result.reason,
            )
            return ConfirmationResult(
                confirmed=True,
                method="momentum",
                reason=result.reason,
                candles_used=count,
            )

        return ConfirmationResult(
            confirmed=False,
            method="none",
            reason=f"Waiting ({count}/{timeout} candles)",
            candles_used=count,
        )

    def clear(self, symbol: str) -> None:
        """Reset candle count for a symbol."""
        self._pending_candle_counts.pop(symbol, None)

    def reset(self) -> None:
        self._pending_candle_counts.clear()

    @staticmethod
    def _check_structure(
        m1_df: pd.DataFrame,
        direction: str,
        pip_size: float,
    ) -> ConfirmationResult:
        """CHoCH / BOS detection on M1, matching entry_engine logic."""
        try:
            bars = min(len(m1_df), M1_DEFAULT_BARS)
            df_slice = m1_df.iloc[-bars:]
            engine = StructureEngine(swing_lookback=M1_SWING_LOOKBACK, pip_size=pip_size)
            analysis = engine.analyze(drop_forming_bar(df_slice))

            event = analysis.last_event
            trend = analysis.trend

            if direction == "LONG":
                if event == StructureEvent.CHOCH_BULLISH:
                    return ConfirmationResult(True, "choch", "M1 CHoCH bullish")
                if event == StructureEvent.BOS_BULLISH:
                    return ConfirmationResult(True, "bos", "M1 BOS bullish")
                # The CHoCH-level fallback requires the M1 trend to actually
                # match the entry direction. A RANGING M1 has no direction, so
                # it must NOT confirm — previously RANGING satisfied BOTH the
                # LONG and SHORT branches, confirming entries in both directions
                # from the same neutral structure.
                if (
                    analysis.last_choch_level is not None
                    and trend == Trend.BULLISH
                ):
                    return ConfirmationResult(True, "choch_level", "M1 CHoCH level + trend aligned")
            else:
                if event == StructureEvent.CHOCH_BEARISH:
                    return ConfirmationResult(True, "choch", "M1 CHoCH bearish")
                if event == StructureEvent.BOS_BEARISH:
                    return ConfirmationResult(True, "bos", "M1 BOS bearish")
                if (
                    analysis.last_choch_level is not None
                    and trend == Trend.BEARISH
                ):
                    return ConfirmationResult(True, "choch_level", "M1 CHoCH level + trend aligned")

        except Exception as exc:
            logger.debug("[m1-confirm] structure check error: {}", exc)

        return ConfirmationResult(False, "none", "No M1 structure confirmation")

    @staticmethod
    def _check_momentum(
        m1_df: pd.DataFrame,
        direction: str,
        zone: Optional[EntryZone],
        pip_size: float,
    ) -> ConfirmationResult:
        """Momentum confirmation matching entry_engine logic.

        Three gates + three pattern checks:
        1. Minimum 5 bars.
        2. Zone proximity — last candle must be near the zone.
        3. Volume filter — at least one aligned candle has above-average volume.
        4. ≥3/5 aligned candles, or last-two consecutive, or higher-low pattern.
        """
        if len(m1_df) < 5:
            return ConfirmationResult(False, "none", "M1 < 5 bars for momentum")

        # Drop the still-forming bar so momentum is judged on CLOSED candles
        # only (the structure path already does this via drop_forming_bar).
        m1_df = drop_forming_bar(m1_df)
        if len(m1_df) < 5:
            return ConfirmationResult(False, "none", "M1 < 5 closed bars for momentum")

        if zone is not None:
            proximity = 10.0 * pip_size  # ~10 pips
            last_row = m1_df.iloc[-1]
            if direction == "LONG":
                if last_row["low"] > zone.top + proximity:
                    return ConfirmationResult(False, "none", "Price too far above zone for LONG momentum")
            else:
                if last_row["high"] < zone.bottom - proximity:
                    return ConfirmationResult(False, "none", "Price too far below zone for SHORT momentum")

        if "tick_volume" in m1_df.columns and len(m1_df) >= 20:
            avg_vol = m1_df["tick_volume"].iloc[-20:].mean()
            last5 = m1_df.iloc[-5:]
            if direction == "LONG":
                aligned = last5[last5["close"] > last5["open"]]
            else:
                aligned = last5[last5["close"] < last5["open"]]
            if len(aligned) > 0 and not (aligned["tick_volume"] > avg_vol).any():
                return ConfirmationResult(False, "none", "No volume-backed momentum candle")

        closes = m1_df["close"].iloc[-5:].values
        opens = m1_df["open"].iloc[-5:].values
        highs = m1_df["high"].iloc[-5:].values
        lows = m1_df["low"].iloc[-5:].values

        if direction == "LONG":
            bullish = sum(1 for c, o in zip(closes, opens) if c > o)
            if bullish >= 3:
                return ConfirmationResult(True, "momentum", f"{bullish}/5 bullish candles")
            if len(closes) >= 2 and closes[-1] > opens[-1] and closes[-2] > opens[-2] and closes[-1] > closes[-2]:
                return ConfirmationResult(True, "momentum", "Two consecutive rising bullish candles")
            if len(lows) >= 3 and lows[-1] > lows[-3] and closes[-1] > closes[-3]:
                return ConfirmationResult(True, "momentum", "Higher low + higher close pattern")
        else:
            bearish = sum(1 for c, o in zip(closes, opens) if c < o)
            if bearish >= 3:
                return ConfirmationResult(True, "momentum", f"{bearish}/5 bearish candles")
            if len(closes) >= 2 and closes[-1] < opens[-1] and closes[-2] < opens[-2] and closes[-1] < closes[-2]:
                return ConfirmationResult(True, "momentum", "Two consecutive falling bearish candles")
            if len(highs) >= 3 and highs[-1] < highs[-3] and closes[-1] < closes[-3]:
                return ConfirmationResult(True, "momentum", "Lower high + lower close pattern")

        return ConfirmationResult(False, "none", "No M1 momentum pattern")
