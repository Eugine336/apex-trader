"""
APEX RL — Multi-Timeframe Observation Builder
===============================================
Produces the ``(50, 48)`` observation tensor and instrument-context vector
defined by ``rl.contracts``.

Two entry points that MUST produce identical output on identical data
(train/serve parity):

* ``build_from_frames``  — batch / training / backtest  (accepts DataFrames)
* ``add_bar`` + ``build`` — incremental / live           (bar-by-bar feed)

Both converge on the same alignment and feature-construction path so there
is exactly *one* code path that touches the observation shape.
"""

from __future__ import annotations

import math
from collections import defaultdict
from datetime import datetime, timezone
from typing import Optional

import numpy as np
import pandas as pd

from rl.contracts import (
    CLOCK_TF,
    MARKET_FEATURES,
    N_CONTEXT_FEATURES,
    N_MARKET_FEATURES,
    N_TIMEFRAMES,
    OBS_SHAPE,
    TF_ORDER,
    TF_SECONDS,
    WINDOW,
    ATR_PERIOD,
)
from rl.obs_builder import ObservationBuilder


# ── Symbol classification helpers ────────────────────────────────────────────

_INDEX_SYMBOLS = frozenset([
    "US100", "US30", "US500", "GER40", "UK100", "JP225",
    "HK50", "AUS200", "FRA40",
])

_CRYPTO_SYMBOLS = frozenset([
    "BTCUSD", "ETHUSD", "SOLUSD", "XRPUSD", "ADAUSD",
    "BNBUSD", "DOTUSD", "LTCUSD",
])

_METAL_SYMBOLS = frozenset(["XAUUSD", "XAGUSD"])

_DEFAULT_PROFILE: dict[str, float] = {
    "pip_size": 0.0001,
    "typical_spread_pips": 1.5,
    "pip_value": 10.0,
}


def _is_jpy_pair(symbol: str) -> bool:
    return symbol.upper().endswith("JPY")


def _is_metal(symbol: str) -> bool:
    return symbol.upper() in _METAL_SYMBOLS


def _is_index(symbol: str) -> bool:
    return symbol.upper() in _INDEX_SYMBOLS


def _is_crypto(symbol: str) -> bool:
    return symbol.upper() in _CRYPTO_SYMBOLS


def _safe_log(x: float) -> float:
    return math.log(max(abs(x), 1e-12))


# ── Alignment ────────────────────────────────────────────────────────────────


def _parse_time(t) -> datetime:
    """Accept datetime, pd.Timestamp, or ISO-format string → UTC datetime."""
    if isinstance(t, datetime):
        if t.tzinfo is None:
            return t.replace(tzinfo=timezone.utc)
        return t
    if isinstance(t, pd.Timestamp):
        dt = t.to_pydatetime()
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt
    return datetime.fromisoformat(str(t)).replace(tzinfo=timezone.utc)


def _select_closed(
    df: pd.DataFrame,
    tf: str,
    anchor_open: datetime,
) -> pd.DataFrame:
    """Return rows from *df* whose bars are fully closed at *anchor_open*.

    For the clock TF (M5) the anchor bar itself is included (open <= anchor).
    For higher TFs a bar with open T is included only if T + period <= anchor
    (i.e. the bar has fully closed before the anchor opens).
    """
    times = pd.to_datetime(df["time"], utc=True)

    if tf == CLOCK_TF:
        mask = times <= anchor_open
    else:
        period = pd.Timedelta(seconds=TF_SECONDS[tf])
        mask = (times + period) <= anchor_open

    return df.loc[mask]


# ── Context vector ───────────────────────────────────────────────────────────


def build_context(
    instrument: str,
    profile: Optional[dict] = None,
) -> np.ndarray:
    """Return a float32 vector in ``INSTRUMENT_CONTEXT_FEATURES`` order."""
    p = {**_DEFAULT_PROFILE, **(profile or {})}
    sym = instrument.upper()

    vec = np.array(
        [
            _safe_log(p.get("pip_size", _DEFAULT_PROFILE["pip_size"])),
            float(p.get("typical_spread_pips", _DEFAULT_PROFILE["typical_spread_pips"])),
            _safe_log(p.get("pip_value", _DEFAULT_PROFILE["pip_value"])),
            float(_is_jpy_pair(sym)),
            float(_is_metal(sym)),
            float(_is_index(sym)),
            float(_is_crypto(sym)),
            float(p.get("is_always_open", _is_crypto(sym))),
        ],
        dtype=np.float32,
    )
    assert vec.shape == (N_CONTEXT_FEATURES,)
    return vec


def symbol_id_for(instrument: str, universe: Optional[list[str]] = None) -> int:
    """Stable integer id: index in *universe* if given, else deterministic hash.

    When a *universe* is supplied but the symbol is not part of it, return -1
    ("unknown symbol"). The network treats a negative/out-of-range id as a zero
    symbol embedding rather than crashing on an invalid embedding lookup.
    """
    sym = instrument.upper()
    if universe is not None:
        normed = [s.upper() for s in universe]
        if sym in normed:
            return normed.index(sym)
        return -1
    return int(hash(sym) % (2**31))


# ── Builder ──────────────────────────────────────────────────────────────────


class MultiTFObservationBuilder:
    """Produce a ``(50, 48)`` MTF observation and context vector.

    Reuses ``ObservationBuilder`` per timeframe so the per-window z-norm
    and feature construction is byte-identical to the existing live path.
    """

    def __init__(self) -> None:
        self._buffers: dict[str, list[dict]] = defaultdict(list)
        self._max_buf = WINDOW + ATR_PERIOD + 30

    # ── Incremental / live path ──────────────────────────────────────────

    def add_bar(
        self,
        tf: str,
        time,
        open: float,
        high: float,
        low: float,
        close: float,
        volume: float = 1.0,
    ) -> None:
        """Append one OHLCV bar for *tf*. Call ``build()`` after feeding bars."""
        buf = self._buffers[tf]
        buf.append({
            "time": _parse_time(time),
            "open": open,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume,
        })
        if len(buf) > self._max_buf * 2:
            self._buffers[tf] = buf[-self._max_buf:]

    def build(
        self,
        instrument: str,
        in_trade: float = 0.0,
        profile: Optional[dict] = None,
        universe: Optional[list[str]] = None,
    ) -> Optional[tuple[np.ndarray, np.ndarray, int]]:
        """Build from incrementally added bars.

        Returns ``(obs, context, symbol_id)`` or ``None`` if any TF has
        insufficient data.
        """
        if not self._buffers:
            return None

        m5_buf = self._buffers.get(CLOCK_TF)
        if not m5_buf:
            return None

        anchor_open = m5_buf[-1]["time"]
        frames: dict[str, pd.DataFrame] = {}

        for tf in TF_ORDER:
            buf = self._buffers.get(tf)
            if not buf:
                return None
            df = pd.DataFrame(buf)
            frames[tf] = df

        return self._assemble(frames, anchor_open, instrument, in_trade, profile, universe)

    # ── Batch / training path ────────────────────────────────────────────

    def build_from_frames(
        self,
        frames: dict[str, pd.DataFrame],
        instrument: str,
        in_trade: float = 0.0,
        profile: Optional[dict] = None,
        universe: Optional[list[str]] = None,
    ) -> Optional[tuple[np.ndarray, np.ndarray, int]]:
        """Build from pre-loaded DataFrames keyed by TF name.

        Each DataFrame must have columns ``time, open, high, low, close, volume``.
        Returns ``(obs, context, symbol_id)`` or ``None`` if insufficient data.
        """
        m5 = frames.get(CLOCK_TF)
        if m5 is None or m5.empty:
            return None

        anchor_open = _parse_time(m5["time"].iloc[-1])
        return self._assemble(frames, anchor_open, instrument, in_trade, profile, universe)

    # ── Shared assembly ──────────────────────────────────────────────────

    def _assemble(
        self,
        frames: dict[str, pd.DataFrame],
        anchor_open: datetime,
        instrument: str,
        in_trade: float,
        profile: Optional[dict],
        universe: Optional[list[str]],
    ) -> Optional[tuple[np.ndarray, np.ndarray, int]]:
        blocks: list[np.ndarray] = []
        it_idx = MARKET_FEATURES.index("in_trade")
        clamped_it = float(np.clip(in_trade, -3, 3))

        for tf in TF_ORDER:
            df = frames.get(tf)
            if df is None or df.empty:
                return None

            selected = _select_closed(df, tf, anchor_open)

            need = WINDOW + ATR_PERIOD
            if len(selected) < need:
                return None

            builder = ObservationBuilder(window=WINDOW, atr_period=ATR_PERIOD)
            block = builder.from_dataframe(selected)

            if block is None:
                return None

            blocks.append(block)

        obs = np.concatenate(blocks, axis=1).astype(np.float32)

        for k in range(N_TIMEFRAMES):
            obs[:, k * N_MARKET_FEATURES + it_idx] = clamped_it

        np.nan_to_num(obs, copy=False)

        assert obs.shape == OBS_SHAPE, f"Expected {OBS_SHAPE}, got {obs.shape}"

        ctx = build_context(instrument, profile)
        sid = symbol_id_for(instrument, universe)

        return obs, ctx, sid
