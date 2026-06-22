"""APEX TRADER — Self-calibrating instrument statistics.

The instrument *profile* (``brain/instrument_profile.py``) historically carried
hardcoded, per-category geometry constants (FVG proximity, OB buffer, minimum
risk, swing size, …) expressed in pips.  Those numbers were human priors, not
measurements — XAUUSD and WTI shared one literal, and none of them adapted as a
market's volatility changed.

This module is the data-driven replacement substrate.  It keeps a rolling,
per-symbol ``InstrumentStats`` snapshot computed from data the system already
processes (candles per timeframe + live spread samples) and exposes **universal
ATR-normalised formulas** so one rule works for every instrument:

  * ``spread / ATR``                 → is the spread wide right now?
  * ``current_ATR / median_ATR``     → what volatility regime are we in?
  * geometry as ``k * ATR``          → proximity / buffers / minimum risk scale
                                       with the instrument's own volatility.

It also DISCOVERS, rather than declaring:

  * ``vol_by_hour[24]`` — average true range per UTC hour over a rolling window,
    so the system learns *which sessions matter* for each symbol (gold wakes at
    the London open; a synthetic is flat 24/7) instead of a hardcoded window.
  * ``structure_reliability`` — the fraction of recent BOS/CHOCH events that
    held, a measured trust score for the structure read.
  * ``zone_hit_rate`` / ``zone_hold_rate`` — how often price reaches a zone and
    then respects it.

It is pure over its inputs (no broker, no engine, no threads beyond the store's
own lock) and reads from MT5 only what genuinely cannot be derived — that lives
in ``config.INSTRUMENT_REGISTRY`` (``pip_size``, contract identity).  Everything
here is *computed*.
"""

from __future__ import annotations

import math
import threading
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Deque, Optional

# ── Rolling-window sizes ──────────────────────────────────────────────────
# Deliberately modest so a fresh symbol calibrates within a session, yet long
# enough to be stable. All are sample counts, not wall-clock.
_ATR_PERIOD = 14
_ATR_MEDIAN_BARS = 200        # bars used for the "normal" ATR baseline
_SPREAD_SAMPLES = 500         # live spread readings retained for median / p95
_HOUR_EMA_ALPHA = 2.0 / (20 + 1)   # ~20-day EMA of per-hour range (rolling)
_OUTCOME_WINDOW = 200         # structure/zone outcome memory


def _percentile(sorted_vals: list[float], pct: float) -> float:
    """Linear-interpolated percentile of an already-sorted list (pct in [0,1])."""
    if not sorted_vals:
        return 0.0
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    pos = pct * (len(sorted_vals) - 1)
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return sorted_vals[lo]
    frac = pos - lo
    return sorted_vals[lo] * (1.0 - frac) + sorted_vals[hi] * frac


# ── Universal ATR-normalised formulas (one rule for every instrument) ──────


def spread_ratio(spread_pips: float, atr_pips: float) -> float:
    """``spread / ATR`` — dimensionless 'how wide is the spread' for any symbol.

    Returns 0.0 when ATR is unknown so a missing read never *blocks* (the caller
    decides; a 0 ratio reads as 'not wide').
    """
    if atr_pips is None or atr_pips <= 0:
        return 0.0
    return max(0.0, float(spread_pips)) / float(atr_pips)


def is_spread_wide(spread_pips: float, atr_pips: float, *, max_ratio: float = 0.15) -> bool:
    """True when the live spread eats more than ``max_ratio`` of one ATR.

    ``0.15`` means 'spread costs >15% of a typical bar's range' — a single
    threshold that is correct for a 1-pip EURUSD spread and a $40 BTC spread
    alike, because both are measured against that instrument's own ATR.
    """
    if atr_pips is None or atr_pips <= 0:
        return False
    return spread_ratio(spread_pips, atr_pips) > max(0.0, float(max_ratio))


def atr_regime_ratio(current_atr: float, median_atr: float) -> float:
    """``current_ATR / median_ATR`` — >1 expanding volatility, <1 compressing."""
    if median_atr is None or median_atr <= 0 or current_atr is None or current_atr <= 0:
        return 1.0
    return float(current_atr) / float(median_atr)


def classify_atr_regime(
    current_atr: float,
    median_atr: float,
    *,
    expansion: float = 1.5,
    compression: float = 0.6,
) -> str:
    """Map the ATR ratio to a regime label using universal thresholds.

    ``"EXPANSION"`` / ``"COMPRESSION"`` / ``"NORMAL"`` — same rule for forex,
    gold, indices, crypto and synthetics; only the per-symbol ATR differs.
    """
    r = atr_regime_ratio(current_atr, median_atr)
    if r >= expansion:
        return "EXPANSION"
    if r <= compression:
        return "COMPRESSION"
    return "NORMAL"


@dataclass
class InstrumentStats:
    """Rolling, self-calibrating statistics for ONE symbol.

    All fields are *measured*; nothing here is a per-category human prior.
    Cheap to read (plain attribute access); updates are O(1) amortised.
    """

    symbol: str
    pip_size: float = 0.0001

    # Latest ATR per timeframe, in price units and in pips.
    atr_by_tf: dict[str, float] = field(default_factory=dict)
    atr_pips_by_tf: dict[str, float] = field(default_factory=dict)
    # Rolling baseline ("normal") ATR per timeframe, in pips, for the regime ratio.
    _atr_pips_window: dict[str, Deque[float]] = field(default_factory=dict)

    # Live spread samples (pips) → median / p95 on demand.
    _spread_samples: Deque[float] = field(default_factory=lambda: deque(maxlen=_SPREAD_SAMPLES))

    # Average true range per UTC hour (EMA), so the busy hours emerge from data.
    vol_by_hour: list[float] = field(default_factory=lambda: [0.0] * 24)
    _hour_seen: list[bool] = field(default_factory=lambda: [False] * 24)

    # Structure-event outcomes (BOS/CHOCH that held vs failed).
    _structure_outcomes: Deque[bool] = field(default_factory=lambda: deque(maxlen=_OUTCOME_WINDOW))
    # Zone outcomes (reached / respected).
    _zone_touch_outcomes: Deque[bool] = field(default_factory=lambda: deque(maxlen=_OUTCOME_WINDOW))
    _zone_hold_outcomes: Deque[bool] = field(default_factory=lambda: deque(maxlen=_OUTCOME_WINDOW))

    samples: int = 0

    # ── Updates ───────────────────────────────────────────────────────────

    def update_candles(self, tf: str, df: Any) -> None:
        """Recompute ATR (price + pips) for ``tf`` and roll the hour-of-day map.

        ``df`` is a frame with ``high``/``low``/``close`` (and ideally ``time``)
        columns. Guarded so a short/degenerate frame never raises or mutates
        state. Uses the SAME ATR formula as ``brain.volatility_stop`` so the
        baseline matches every other ATR read in the system.
        """
        try:
            from brain.volatility_stop import latest_atr

            atr = latest_atr(df, _ATR_PERIOD)
            if atr is None or atr <= 0:
                return
            self.atr_by_tf[tf] = float(atr)
            pip = self.pip_size if self.pip_size > 0 else 0.0001
            atr_pips = float(atr) / pip
            self.atr_pips_by_tf[tf] = atr_pips
            win = self._atr_pips_window.setdefault(
                tf, deque(maxlen=_ATR_MEDIAN_BARS)
            )
            win.append(atr_pips)
            self.samples += 1
            self._roll_hour_of_day(df)
        except Exception:  # noqa: BLE001 — stats must never break analysis
            return

    def _roll_hour_of_day(self, df: Any) -> None:
        """Fold the last bar's range into the per-UTC-hour EMA (session discovery)."""
        try:
            last = df.iloc[-1]
            rng = float(last["high"]) - float(last["low"])
            if rng <= 0:
                return
            pip = self.pip_size if self.pip_size > 0 else 0.0001
            rng_pips = rng / pip
            ts = last.get("time") if hasattr(last, "get") else last["time"]
            hour = int(getattr(ts, "hour", None) if hasattr(ts, "hour") else None)  # type: ignore[arg-type]
        except Exception:
            return
        if hour is None or not (0 <= hour < 24):
            return
        if not self._hour_seen[hour]:
            self.vol_by_hour[hour] = rng_pips
            self._hour_seen[hour] = True
        else:
            a = _HOUR_EMA_ALPHA
            self.vol_by_hour[hour] = (1.0 - a) * self.vol_by_hour[hour] + a * rng_pips

    def update_spread(self, spread_pips: float) -> None:
        """Record one live spread sample (pips)."""
        try:
            v = float(spread_pips)
        except (TypeError, ValueError):
            return
        if v >= 0 and math.isfinite(v):
            self._spread_samples.append(v)

    def record_structure_outcome(self, held: bool) -> None:
        """Record whether a BOS/CHOCH event subsequently held (True) or failed."""
        self._structure_outcomes.append(bool(held))

    def record_zone_outcome(self, *, touched: bool, held: Optional[bool] = None) -> None:
        """Record a zone outcome: was it reached, and (if reached) did it hold?"""
        self._zone_touch_outcomes.append(bool(touched))
        if touched and held is not None:
            self._zone_hold_outcomes.append(bool(held))

    # ── Reads ─────────────────────────────────────────────────────────────

    def atr_pips(self, tf: str = "M5") -> float:
        """Latest ATR for ``tf`` in pips (0.0 when not yet computed)."""
        return float(self.atr_pips_by_tf.get(tf, 0.0))

    def median_atr_pips(self, tf: str = "M5") -> float:
        """Rolling median ATR (pips) for ``tf`` — the 'normal' volatility baseline."""
        win = self._atr_pips_window.get(tf)
        if not win:
            return 0.0
        return _percentile(sorted(win), 0.5)

    def atr_regime(self, tf: str = "M5") -> str:
        """Volatility regime label from current vs median ATR on ``tf``."""
        return classify_atr_regime(self.atr_pips(tf), self.median_atr_pips(tf))

    @property
    def spread_median(self) -> float:
        if not self._spread_samples:
            return 0.0
        return _percentile(sorted(self._spread_samples), 0.5)

    @property
    def spread_p95(self) -> float:
        if not self._spread_samples:
            return 0.0
        return _percentile(sorted(self._spread_samples), 0.95)

    @property
    def structure_reliability(self) -> Optional[float]:
        """Fraction of recent BOS/CHOCH events that held, or None if untracked."""
        if not self._structure_outcomes:
            return None
        return sum(1 for x in self._structure_outcomes if x) / len(self._structure_outcomes)

    @property
    def zone_hit_rate(self) -> Optional[float]:
        if not self._zone_touch_outcomes:
            return None
        return sum(1 for x in self._zone_touch_outcomes if x) / len(self._zone_touch_outcomes)

    @property
    def zone_hold_rate(self) -> Optional[float]:
        if not self._zone_hold_outcomes:
            return None
        return sum(1 for x in self._zone_hold_outcomes if x) / len(self._zone_hold_outcomes)

    def active_hours(self, *, ratio: float = 1.2) -> list[int]:
        """UTC hours whose average range exceeds ``ratio``× the all-hours mean.

        The DISCOVERED 'sessions that matter' for this symbol — empty until
        enough hours have been observed.
        """
        seen = [self.vol_by_hour[h] for h in range(24) if self._hour_seen[h]]
        if len(seen) < 6:
            return []
        mean = sum(seen) / len(seen)
        if mean <= 0:
            return []
        return [h for h in range(24) if self._hour_seen[h] and self.vol_by_hour[h] > ratio * mean]

    def is_calibrated(self, tf: str = "M5", *, min_samples: int = _ATR_PERIOD + 5) -> bool:
        """True once enough ATR bars exist on ``tf`` to trust the derived geometry."""
        win = self._atr_pips_window.get(tf)
        return bool(win) and len(win) >= min_samples and self.atr_pips(tf) > 0


class InstrumentStatsStore:
    """Thread-safe per-symbol ``InstrumentStats`` registry.

    Mirrors the publish/read discipline of ``brain.world_model.WorldModelStore``:
    one lock, ``get_or_create`` for writers, ``get`` for readers.
    """

    def __init__(self) -> None:
        self._store: dict[str, InstrumentStats] = {}
        self._lock = threading.RLock()

    def get_or_create(self, symbol: str, pip_size: float = 0.0001) -> InstrumentStats:
        key = symbol.upper()
        with self._lock:
            st = self._store.get(key)
            if st is None:
                st = InstrumentStats(symbol=key, pip_size=pip_size)
                self._store[key] = st
            elif pip_size > 0 and st.pip_size != pip_size:
                st.pip_size = pip_size
            return st

    def get(self, symbol: str) -> Optional[InstrumentStats]:
        with self._lock:
            return self._store.get(symbol.upper())

    def symbols(self) -> list[str]:
        with self._lock:
            return list(self._store.keys())

    def clear(self) -> None:
        with self._lock:
            self._store.clear()
