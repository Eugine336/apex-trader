"""
APEX TRADER — Global Compression Detector

Classifies the volatility state of ANY instrument into one of four
``MarketState`` values, working uniformly across forex, commodities, indices,
crypto and synthetics — never gold-only.

The system already reads directional structure (BULLISH / BEARISH / RANGING)
but had no concept of volatility *compression* — the Bollinger-Band squeeze
that precedes breakouts. This module fills that gap and is the foundation for
later features (pre-staged breakout orders, session-aware sizing).

Core idea
---------
On every configured timeframe (M5 and M15 by default) it computes the
Bollinger-Band width::

    BBW = (upper - lower) / middle

and ranks the current BBW as a percentile over a rolling lookback window. From
that percentile plus ADX it classifies the market:

    COMPRESSING — BBW sits in the bottom ``compression_threshold_pct`` percentile
    EXPANDING   — BBW just broke back above ``expansion_threshold_pct`` after
                  having been compressed (a squeeze resolving into a breakout)
    TRENDING    — ADX above ``adx_trending_threshold``
    RANGING     — everything else (the neutral default)

Per-instrument tuning comes from ``InstrumentProfile`` (a profile may override
any threshold); the global fallbacks live on ``EntryConfig``.

The heavy lifting uses the ``ta`` library's ``BollingerBands`` and
``ADXIndicator`` (already pinned in requirements.txt). Pure-pandas fallbacks are
provided so the detector stays importable and computable even if ``ta`` is
unavailable.

Thread-safety: ``update`` runs on the CandleCloseHandler worker pool, so all
mutable per-symbol state is guarded by an ``RLock``.
"""

from __future__ import annotations

import math
import threading
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Optional

import pandas as pd
from loguru import logger

from brain.instrument_profile import get_profile
from entry.models import EntryConfig

# Prefer the ``ta`` implementations (as pinned in requirements.txt); fall back to
# equivalent pure-pandas maths so the module never hard-fails on import if the
# optional dependency is missing.
try:  # pragma: no cover - the fallback branch is only hit when ``ta`` is absent
    from ta.trend import ADXIndicator
    from ta.volatility import BollingerBands
    _HAS_TA = True
except Exception:  # pragma: no cover
    ADXIndicator = None
    BollingerBands = None
    _HAS_TA = False


class MarketState(str, Enum):
    """Global volatility state of an instrument."""

    TRENDING = "TRENDING"
    RANGING = "RANGING"
    COMPRESSING = "COMPRESSING"
    EXPANDING = "EXPANDING"


# Precedence used to collapse several per-timeframe states into one per-symbol
# state (highest priority wins). EXPANDING is the most actionable (a squeeze
# just resolved), then COMPRESSING (a squeeze is building), then TRENDING;
# RANGING is the neutral default.
_STATE_PRIORITY: dict[MarketState, int] = {
    MarketState.EXPANDING: 3,
    MarketState.COMPRESSING: 2,
    MarketState.TRENDING: 1,
    MarketState.RANGING: 0,
}

# Bollinger-Band / ADX geometry. These are structural to the indicators (a
# 20/2 band and a 14-period ADX are the universally-used defaults) rather than
# per-instrument tuning, so they live here rather than on the profile.
_BB_WINDOW = 20
_BB_DEV = 2.0
_ADX_WINDOW = 14

# Minimum bars required before ADX/BBW are meaningful at all.
_MIN_BARS = _BB_WINDOW + 2


@dataclass
class TimeframeState:
    """Latest per-(symbol, timeframe) compression read."""

    state: MarketState = MarketState.RANGING
    bbw: float = 0.0
    percentile: float = -1.0        # -1.0 == not yet computed / insufficient data
    adx: float = 0.0
    compression_score: float = 0.0  # 0.0 = no compression, 1.0 = maximum squeeze
    was_compressed: bool = False


# ---------------------------------------------------------------------------
# Pure indicator maths (no side effects) — reusable + directly testable
# ---------------------------------------------------------------------------

def bollinger_band_width(
    close: pd.Series, window: int = _BB_WINDOW, dev: float = _BB_DEV,
) -> pd.Series:
    """Return the Bollinger-Band width series ``(upper - lower) / middle``.

    Warm-up rows (before ``window`` bars exist) are NaN — callers drop them.
    """
    if _HAS_TA:
        bb = BollingerBands(close=close, window=window, window_dev=dev, fillna=False)
        upper = bb.bollinger_hband()
        lower = bb.bollinger_lband()
        middle = bb.bollinger_mavg()
    else:  # pragma: no cover - exercised only when ``ta`` is absent
        middle = close.rolling(window, min_periods=window).mean()
        std = close.rolling(window, min_periods=window).std(ddof=0)
        upper = middle + dev * std
        lower = middle - dev * std
    middle = middle.replace(0.0, float("nan"))
    return (upper - lower) / middle


def adx_series(df: pd.DataFrame, window: int = _ADX_WINDOW) -> pd.Series:
    """Return the ADX series for a high/low/close DataFrame."""
    if _HAS_TA:
        ind = ADXIndicator(
            high=df["high"], low=df["low"], close=df["close"],
            window=window, fillna=False,
        )
        return ind.adx()
    return _adx_fallback(df, window)  # pragma: no cover


def _adx_fallback(df: pd.DataFrame, window: int) -> pd.Series:  # pragma: no cover
    """Wilder-smoothed ADX in pure pandas (used only when ``ta`` is absent)."""
    high = df["high"]
    low = df["low"]
    close = df["close"]
    up = high.diff()
    down = -low.diff()
    plus_dm = ((up > down) & (up > 0)) * up
    minus_dm = ((down > up) & (down > 0)) * down
    prev_close = close.shift(1)
    tr = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()],
        axis=1,
    ).max(axis=1)
    alpha = 1.0 / window
    atr = tr.ewm(alpha=alpha, min_periods=window).mean()
    plus_di = 100.0 * (plus_dm.ewm(alpha=alpha, min_periods=window).mean() / atr)
    minus_di = 100.0 * (minus_dm.ewm(alpha=alpha, min_periods=window).mean() / atr)
    di_sum = (plus_di + minus_di).replace(0.0, float("nan"))
    dx = 100.0 * (plus_di - minus_di).abs() / di_sum
    return dx.ewm(alpha=alpha, min_periods=window).mean()


def percentile_rank(series: pd.Series, lookback: int) -> tuple[float, float]:
    """Percentile rank (0–100) of the latest value within ``lookback`` samples.

    Returns ``(percentile, current_value)``. ``percentile`` is ``-1.0`` when
    fewer than ``lookback`` finite samples are available so the caller can tell
    "not computed" apart from "0th percentile". Mirrors the convention in
    ``brain.atr_percentile``.
    """
    usable = series.dropna()
    if lookback > 0:
        usable = usable.tail(lookback)
    if len(usable) < max(lookback, 1):
        return -1.0, float("nan")
    current = float(usable.iloc[-1])
    if not math.isfinite(current):
        return -1.0, current
    rank = float((usable < current).sum()) / len(usable) * 100.0
    return rank, current


def compression_score(percentile: float, threshold_pct: float) -> float:
    """Map a BBW percentile to a squeeze score in ``[0.0, 1.0]``.

    ``0.0`` = not compressed (BBW at or above the compression threshold),
    ``1.0`` = maximum squeeze (BBW at the 0th percentile of the window). The
    score is only non-zero inside the compression zone so it aligns exactly
    with the COMPRESSING classification boundary.
    """
    if percentile < 0 or threshold_pct <= 0:
        return 0.0
    if percentile >= threshold_pct:
        return 0.0
    return max(0.0, min(1.0, (threshold_pct - percentile) / threshold_pct))


def classify_state(
    *,
    percentile: float,
    adx: float,
    was_compressed: bool,
    compression_threshold_pct: float,
    adx_trending_threshold: float,
    expansion_threshold_pct: float,
) -> tuple[MarketState, bool]:
    """Pure classifier: map indicators + prior memory to ``(state, was_compressed)``.

    ``was_compressed`` is the running memory that a squeeze occurred and has not
    yet resolved; it is what makes EXPANDING detectable (a breakout is only an
    expansion if a compression preceded it). The returned flag is the updated
    memory the caller should persist.
    """
    if percentile < 0:
        # Not enough data to classify — stay neutral, preserve squeeze memory.
        return MarketState.RANGING, was_compressed
    if was_compressed and percentile >= expansion_threshold_pct:
        # A prior squeeze has resolved — the breakout expansion. Consume memory.
        return MarketState.EXPANDING, False
    if percentile <= compression_threshold_pct:
        return MarketState.COMPRESSING, True
    if adx >= adx_trending_threshold:
        return MarketState.TRENDING, was_compressed
    return MarketState.RANGING, was_compressed


# ---------------------------------------------------------------------------
# Stateful detector
# ---------------------------------------------------------------------------

class CompressionDetector:
    """Per-symbol volatility-state classifier driven by candle closes.

    Call :meth:`update` on each configured M5/M15 candle close with the same
    WorldModel candle DataFrame the brain already consumes. Read the current
    state with :meth:`get_market_state` and the squeeze intensity with
    :meth:`get_compression_score`.

    Tuning is resolved per-symbol: the ``InstrumentProfile`` value is used when
    present, otherwise the ``EntryConfig`` default — so a profile can override
    the global defaults per instrument.
    """

    def __init__(
        self,
        entry_config: Optional[EntryConfig] = None,
        profile_lookup: Optional[Callable[[str], Any]] = get_profile,
        timeframes: tuple[str, ...] = ("M5", "M15"),
    ) -> None:
        self._entry_config = entry_config or EntryConfig()
        self._profile_lookup = profile_lookup
        self._timeframes = tuple(str(tf).upper() for tf in timeframes)
        self._states: dict[tuple[str, str], TimeframeState] = {}
        self._lock = threading.RLock()

    @property
    def timeframes(self) -> tuple[str, ...]:
        return self._timeframes

    # ── Tuning resolution ────────────────────────────────────────────
    def _param(self, symbol: str, name: str, default: Any) -> Any:
        """Profile value first, then EntryConfig default, then the literal."""
        if self._profile_lookup is not None:
            try:
                prof = self._profile_lookup(symbol)
            except Exception:  # noqa: BLE001 — tuning lookup must never break analysis
                prof = None
            if prof is not None:
                val = getattr(prof, name, None)
                if val is not None:
                    return val
        val = getattr(self._entry_config, name, None)
        return default if val is None else val

    # ── Update path (candle close) ───────────────────────────────────
    def update(
        self, symbol: str, timeframe: str, df: Optional[pd.DataFrame],
    ) -> Optional[TimeframeState]:
        """Recompute the compression state for ``(symbol, timeframe)``.

        Returns the fresh :class:`TimeframeState`, or ``None`` when the
        timeframe is not tracked or there is not enough data. Never raises —
        analysis failures degrade to a neutral read.
        """
        tf = str(timeframe or "").upper()
        if tf not in self._timeframes:
            return None
        if df is None or len(df) < _MIN_BARS or "close" not in df:
            return None

        lookback = int(self._param(symbol, "compression_lookback", 100))
        comp_thr = float(self._param(symbol, "compression_threshold_pct", 15.0))
        adx_thr = float(self._param(symbol, "adx_trending_threshold", 25.0))
        exp_thr = float(self._param(symbol, "expansion_threshold_pct", 70.0))

        try:
            bbw = bollinger_band_width(df["close"], _BB_WINDOW, _BB_DEV)
            percentile, bbw_now = percentile_rank(bbw, lookback)
            adx_now = self._latest_adx(df)
        except Exception as exc:  # noqa: BLE001 — never break the candle-close path
            logger.debug(
                "[compression] {} {} indicator compute failed: {}", symbol, tf, exc,
            )
            return None

        with self._lock:
            prev = self._states.get((symbol, tf)) or TimeframeState()
            state, was_compressed = classify_state(
                percentile=percentile,
                adx=adx_now,
                was_compressed=prev.was_compressed,
                compression_threshold_pct=comp_thr,
                adx_trending_threshold=adx_thr,
                expansion_threshold_pct=exp_thr,
            )
            new = TimeframeState(
                state=state,
                bbw=bbw_now if math.isfinite(bbw_now) else 0.0,
                percentile=percentile,
                adx=adx_now,
                compression_score=compression_score(percentile, comp_thr),
                was_compressed=was_compressed,
            )
            self._states[(symbol, tf)] = new

        logger.debug(
            "[compression] {} {} → {} (bbw_pctl={:.1f} adx={:.1f} squeeze={:.2f})",
            symbol, tf, state.value, percentile, adx_now, new.compression_score,
        )
        return new

    def _latest_adx(self, df: pd.DataFrame) -> float:
        try:
            usable = adx_series(df, _ADX_WINDOW).dropna()
            if usable.empty:
                return 0.0
            val = float(usable.iloc[-1])
            return val if math.isfinite(val) else 0.0
        except Exception:  # noqa: BLE001
            return 0.0

    # ── Read path ────────────────────────────────────────────────────
    def get_market_state(self, symbol: str) -> MarketState:
        """Current aggregate market state for ``symbol`` (RANGING if unknown).

        Collapses the per-timeframe states via :data:`_STATE_PRIORITY` so the
        most actionable signal across M5/M15 wins.
        """
        with self._lock:
            states = [
                s.state
                for (sym, _tf), s in self._states.items()
                if sym == symbol and s.percentile >= 0
            ]
        if not states:
            return MarketState.RANGING
        return max(states, key=lambda st: _STATE_PRIORITY[st])

    def get_compression_score(self, symbol: str) -> float:
        """Current squeeze intensity for ``symbol`` in ``[0.0, 1.0]``.

        ``0.0`` = no compression, ``1.0`` = maximum squeeze. Reports the
        tightest squeeze across the tracked timeframes.
        """
        with self._lock:
            scores = [
                s.compression_score
                for (sym, _tf), s in self._states.items()
                if sym == symbol and s.percentile >= 0
            ]
        if not scores:
            return 0.0
        return max(scores)

    def get_state_detail(self, symbol: str) -> dict[str, TimeframeState]:
        """Per-timeframe snapshot for ``symbol`` (observability / dashboard)."""
        with self._lock:
            return {
                tf: s
                for (sym, tf), s in self._states.items()
                if sym == symbol
            }
