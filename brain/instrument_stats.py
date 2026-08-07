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
_NEWS_EMA_ALPHA = 2.0 / (20 + 1)   # ~20-event EMA of news impact per currency
_WBR_SAMPLES = 200            # M5 wick-to-body ratios retained for the median
_WBR_MIN_SAMPLES = 50         # min M5 candles before the wick/body read is trusted
_WBR_REFERENCE = 2.0          # typical M5 wick/body ratio for a major forex pair


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

    # Rolling M5 wick-to-body ratios → median on demand.  A high median marks a
    # "wicky" instrument (GBPJPY, XAUUSD) whose spike candles need a wider swing
    # lookback; a low median marks clean waves (EURGBP) where tight lookback works.
    _wick_body_samples: Deque[float] = field(default_factory=lambda: deque(maxlen=_WBR_SAMPLES))

    # Average true range per UTC hour (EMA), so the busy hours emerge from data.
    vol_by_hour: list[float] = field(default_factory=lambda: [0.0] * 24)
    _hour_seen: list[bool] = field(default_factory=lambda: [False] * 24)

    # Structure-event outcomes (BOS/CHOCH that held vs failed).
    _structure_outcomes: Deque[bool] = field(default_factory=lambda: deque(maxlen=_OUTCOME_WINDOW))
    # Zone outcomes (reached / respected).
    _zone_touch_outcomes: Deque[bool] = field(default_factory=lambda: deque(maxlen=_OUTCOME_WINDOW))
    _zone_hold_outcomes: Deque[bool] = field(default_factory=lambda: deque(maxlen=_OUTCOME_WINDOW))

    # Learned news sensitivity per currency: EMA of |price move over the event
    # window| / ATR.  The news guard can read this to weight a currency's events
    # by how much THIS instrument actually reacts, instead of a hardcoded flag.
    news_impact_by_currency: dict[str, float] = field(default_factory=dict)
    _news_count: dict[str, int] = field(default_factory=dict)

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
            if tf == "M5":
                self._roll_wick_body(df)
        except Exception:  # noqa: BLE001 — stats must never break analysis
            return

    def _roll_wick_body(self, df: Any) -> None:
        """Fold the last M5 bar's wick-to-body ratio into the rolling window.

        Ratio is ``(high - low) / |close - open|``.  Doji bars (a near-zero body)
        are skipped to avoid a divide-by-zero / runaway value.
        """
        try:
            last = df.iloc[-1]
            high = float(last["high"])
            low = float(last["low"])
            open_ = float(last["open"])
            close = float(last["close"])
        except Exception:
            return
        body = abs(close - open_)
        if body < 1e-9:
            return  # doji — undefined ratio, skip
        rng = high - low
        if rng <= 0:
            return
        ratio = rng / body
        if math.isfinite(ratio) and ratio > 0:
            self._wick_body_samples.append(ratio)

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

    def record_news_impact(self, currency: str, move_pips: float, atr_pips: float) -> None:
        """Fold one measured news reaction into the per-currency EMA.

        ``move_pips`` is ``|price_after - price_at_event|`` in pips and is
        normalised by ``atr_pips`` so the stored sensitivity is comparable
        across instruments (a 50-pip gold move and a 5-pip EURUSD move are both
        '≈1 ATR').  Guarded; a non-positive ATR or bad input is ignored.
        """
        cur = str(currency or "").upper()
        if not cur or atr_pips is None or atr_pips <= 0:
            return
        try:
            impact = max(0.0, float(move_pips)) / float(atr_pips)
        except (TypeError, ValueError):
            return
        if not math.isfinite(impact):
            return
        n = self._news_count.get(cur, 0)
        if n == 0:
            self.news_impact_by_currency[cur] = impact
        else:
            a = _NEWS_EMA_ALPHA
            self.news_impact_by_currency[cur] = (
                (1.0 - a) * self.news_impact_by_currency.get(cur, 0.0) + a * impact
            )
        self._news_count[cur] = n + 1

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

    def wick_body_ratio(self) -> Optional[float]:
        """Median M5 wick-to-body ratio, or ``None`` until enough samples exist.

        Returns ``None`` below :data:`_WBR_MIN_SAMPLES` so callers fall back to
        the category default; a high value (wicky instrument) warrants a wider
        swing lookback, a low value (clean waves) a tighter one.
        """
        if len(self._wick_body_samples) < _WBR_MIN_SAMPLES:
            return None
        return _percentile(sorted(self._wick_body_samples), 0.5)

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

    def news_sensitivity(self, currency: str) -> Optional[float]:
        """Learned reaction (ATR multiples) of this symbol to a currency's news.

        ``None`` until at least one event has been measured for the currency.
        """
        cur = str(currency or "").upper()
        if cur not in self.news_impact_by_currency:
            return None
        return float(self.news_impact_by_currency[cur])

    # ── Persistence ───────────────────────────────────────────────────────

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe snapshot of all rolling state (for cross-restart persistence)."""
        return {
            "symbol": self.symbol,
            "pip_size": self.pip_size,
            "atr_by_tf": dict(self.atr_by_tf),
            "atr_pips_by_tf": dict(self.atr_pips_by_tf),
            "atr_pips_window": {tf: list(w) for tf, w in self._atr_pips_window.items()},
            "spread_samples": list(self._spread_samples),
            "wick_body_samples": list(self._wick_body_samples),
            "wick_body_ratio": self.wick_body_ratio(),  # computed read for dashboards
            "vol_by_hour": list(self.vol_by_hour),
            "hour_seen": list(self._hour_seen),
            "structure_outcomes": [int(x) for x in self._structure_outcomes],
            "zone_touch_outcomes": [int(x) for x in self._zone_touch_outcomes],
            "zone_hold_outcomes": [int(x) for x in self._zone_hold_outcomes],
            "news_impact_by_currency": dict(self.news_impact_by_currency),
            "news_count": dict(self._news_count),
            "samples": self.samples,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "InstrumentStats":
        """Rebuild an ``InstrumentStats`` from a :meth:`to_dict` snapshot."""
        st = cls(
            symbol=str(data.get("symbol", "")),
            pip_size=float(data.get("pip_size", 0.0001) or 0.0001),
        )
        st.atr_by_tf = {str(k): float(v) for k, v in (data.get("atr_by_tf") or {}).items()}
        st.atr_pips_by_tf = {str(k): float(v) for k, v in (data.get("atr_pips_by_tf") or {}).items()}
        for tf, vals in (data.get("atr_pips_window") or {}).items():
            st._atr_pips_window[str(tf)] = deque(
                (float(v) for v in vals), maxlen=_ATR_MEDIAN_BARS
            )
        st._spread_samples = deque(
            (float(v) for v in (data.get("spread_samples") or [])), maxlen=_SPREAD_SAMPLES
        )
        st._wick_body_samples = deque(
            (float(v) for v in (data.get("wick_body_samples") or [])), maxlen=_WBR_SAMPLES
        )
        vbh = data.get("vol_by_hour") or []
        if len(vbh) == 24:
            st.vol_by_hour = [float(v) for v in vbh]
        hs = data.get("hour_seen") or []
        if len(hs) == 24:
            st._hour_seen = [bool(v) for v in hs]
        st._structure_outcomes = deque(
            (bool(v) for v in (data.get("structure_outcomes") or [])), maxlen=_OUTCOME_WINDOW
        )
        st._zone_touch_outcomes = deque(
            (bool(v) for v in (data.get("zone_touch_outcomes") or [])), maxlen=_OUTCOME_WINDOW
        )
        st._zone_hold_outcomes = deque(
            (bool(v) for v in (data.get("zone_hold_outcomes") or [])), maxlen=_OUTCOME_WINDOW
        )
        st.news_impact_by_currency = {
            str(k): float(v) for k, v in (data.get("news_impact_by_currency") or {}).items()
        }
        st._news_count = {
            str(k): int(v) for k, v in (data.get("news_count") or {}).items()
        }
        st.samples = int(data.get("samples", 0) or 0)
        return st


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

    def to_dict(self) -> dict[str, Any]:
        """Snapshot every symbol's stats (for persistence)."""
        with self._lock:
            return {sym: st.to_dict() for sym, st in self._store.items()}

    def load_dict(self, data: dict[str, Any]) -> int:
        """Replace the store contents from a :meth:`to_dict` snapshot.

        Returns the number of symbols loaded. Best-effort per symbol so one bad
        entry never aborts the whole reload.
        """
        loaded = 0
        with self._lock:
            self._store.clear()
            for sym, blob in (data or {}).items():
                try:
                    self._store[str(sym).upper()] = InstrumentStats.from_dict(blob)
                    loaded += 1
                except Exception:  # noqa: BLE001 — skip a corrupt entry
                    continue
        return loaded
