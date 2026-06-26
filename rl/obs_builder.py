"""
APEX RL — Observation Builder
==============================
Converts live APEX market data into the observation format
expected by the RL agent.

This is the bridge between APEX's internal data structures
and the RL network's input layer.

Two modes:
  1. From raw OHLCV DataFrame (for backtesting / training data prep)
  2. From live APEX tick feed (for shadow trading / live inference)
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from collections import deque
from typing import Optional


WINDOW     = 50
N_FEATURES = 12
ATR_PERIOD = 14


class ObservationBuilder:
    """
    Maintains a rolling window of market data and produces
    normalized observation arrays for the RL agent.

    Usage in APEX scanner per pair:

        # At init
        self.obs_builders = {}

        # Per tick
        if pair not in self.obs_builders:
            self.obs_builders[pair] = ObservationBuilder()

        obs = self.obs_builders[pair].update(
            open=bar.open, high=bar.high,
            low=bar.low,   close=bar.close,
            volume=bar.volume,
            in_trade=1.0 if position_open else 0.0,
        )

        if obs is not None:
            signal = bridge.augment_score(pair, score, obs, ...)
    """

    def __init__(self, window: int = WINDOW, atr_period: int = ATR_PERIOD):
        self.window     = window
        self.atr_period = atr_period

        # Raw price history
        self._opens   = deque(maxlen=window + atr_period + 20)
        self._highs   = deque(maxlen=window + atr_period + 20)
        self._lows    = deque(maxlen=window + atr_period + 20)
        self._closes  = deque(maxlen=window + atr_period + 20)
        self._volumes = deque(maxlen=window + atr_period + 20)

        self._ready = False

    def update(
        self,
        open:     float,
        high:     float,
        low:      float,
        close:    float,
        volume:   float = 1.0,
        in_trade: float = 0.0,
    ) -> Optional[np.ndarray]:
        """
        Feed one new bar. Returns observation array when window is full,
        None otherwise.

        in_trade: 0.0 = no position, positive = unrealised R if long,
                  negative = unrealised R if short.
        """
        self._opens.append(open)
        self._highs.append(high)
        self._lows.append(low)
        self._closes.append(close)
        self._volumes.append(volume)

        if len(self._closes) < self.window + self.atr_period:
            return None

        return self._build(in_trade)

    def from_dataframe(self, df: pd.DataFrame) -> np.ndarray:
        """
        Build a single observation from the last rows of a DataFrame.

        df must have columns: open, high, low, close, volume (optional)
        Returns shape (WINDOW, N_FEATURES) or None if not enough data.
        """
        df = df.tail(self.window + self.atr_period + 20).copy()
        if len(df) < self.window + self.atr_period:
            return None

        for _, row in df.iterrows():
            self.update(
                open=float(row.get("open", row.get("Open", 0))),
                high=float(row.get("high", row.get("High", 0))),
                low=float(row.get("low",  row.get("Low",  0))),
                close=float(row.get("close", row.get("Close", 0))),
                volume=float(row.get("volume", row.get("Volume", 1.0))),
            )

        return self._build(0.0)

    # ── Internal ─────────────────────────────────────────────────────────

    def _build(self, in_trade: float) -> np.ndarray:
        closes  = np.array(self._closes,  dtype=np.float64)
        opens   = np.array(self._opens,   dtype=np.float64)
        highs   = np.array(self._highs,   dtype=np.float64)
        lows    = np.array(self._lows,    dtype=np.float64)
        volumes = np.array(self._volumes, dtype=np.float64)

        # ATR (EWM)
        tr = np.maximum(
            highs[1:] - lows[1:],
            np.maximum(
                np.abs(highs[1:] - closes[:-1]),
                np.abs(lows[1:]  - closes[:-1]),
            )
        )
        atr = self._ewm(tr, self.atr_period)

        # Align all series to same length
        n = min(len(closes), len(atr) + 1)
        closes  = closes[-n:]
        opens   = opens[-n:]
        highs   = highs[-n:]
        lows    = lows[-n:]
        volumes = volumes[-n:]
        atr_s   = np.concatenate([[atr[0]], atr])[-n:]

        # Take last WINDOW bars
        c = closes[-self.window:]
        o = opens[-self.window:]
        h = highs[-self.window:]
        lo = lows[-self.window:]
        v = volumes[-self.window:]
        a = atr_s[-self.window:]

        if len(c) < self.window:
            return None

        # Normalise (z-score)
        def znorm(x):
            mu  = x.mean()
            std = x.std() + 1e-8
            return (x - mu) / std

        # Returns
        ret1  = np.diff(c, prepend=c[0])  / (c + 1e-8)
        ret5  = np.concatenate([[0]*5, (c[5:] - c[:-5]) / (c[:-5] + 1e-8)])
        ret14 = np.concatenate([[0]*14, (c[14:] - c[:-14]) / (c[:-14] + 1e-8)])

        rng       = (h - lo) + 1e-8
        hl_ratio  = rng / (c + 1e-8)
        oc_ratio  = (c - o) / rng

        in_trade_col = np.full(self.window, float(in_trade))

        obs = np.stack([
            znorm(o),
            znorm(h),
            znorm(lo),
            znorm(c),
            znorm(v),
            znorm(a),
            np.clip(ret1,  -0.05, 0.05) * 20,
            np.clip(ret5,  -0.05, 0.05) * 20,
            np.clip(ret14, -0.05, 0.05) * 20,
            np.clip(hl_ratio, 0, 0.02) * 50,
            np.clip(oc_ratio, -1, 1),
            np.clip(in_trade_col, -3, 3),
        ], axis=1).astype(np.float32)

        np.nan_to_num(obs, copy=False)
        return obs

    @staticmethod
    def _ewm(series: np.ndarray, span: int) -> np.ndarray:
        alpha  = 2.0 / (span + 1)
        result = np.zeros_like(series)
        result[0] = series[0]
        for i in range(1, len(series)):
            result[i] = alpha * series[i] + (1 - alpha) * result[i - 1]
        return result
