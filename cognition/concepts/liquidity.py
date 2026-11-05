"""Liquidity pools and sweeps — the full reading.

The concept: resting orders (stops and breakout entries) cluster just beyond obvious
price extremes — prior swing highs and lows, equal highs/lows, the previous day's
or week's extremes. Price is drawn toward untouched pools ("draw on liquidity") and,
when a candle trades through a pool and *closes back inside*, the move was a sweep
(a raid that collected the orders and was rejected). A close beyond the pool is
acceptance, not a sweep.

This module reads, on each timeframe from M5 up:

* the **untouched pools** around price (swing highs/lows, with how *equal* related
  extremes are measured against this instrument's own swing spacing, and the
  previous-day / previous-week extremes when daily data is present);
* **sweeps** on the latest completed bar: which pool, how decisively it was rejected
  (ranked against the instrument's own candles), the implication for the opposite
  side, the invalidation (beyond the sweep extreme), the objective (the nearest
  untouched pool in the trade direction — nothing is invented when there is none),
  and what is still unconfirmed;
* the **draws** above and below price as facts, so other concepts and the Brain can
  use them as objectives.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from cognition.contracts import EvidenceDomain
from cognition.concepts.base import (
    LONG, SHORT, TIMEFRAME_SECONDS, ConceptReading, Implication, finite,
    mean_known, percentile_rank, reference_price,
)
from cognition.concepts.market_structure import _clean, fractal_swings


def _untouched(bars: list[dict], idx: int, kind: str, level: float) -> bool:
    """No later completed bar has traded through the level."""
    later = bars[idx + 1:]
    # Stops rest just beyond the level: touching it exactly does not trade through it.
    return all((b["high"] <= level) if kind == "H" else (b["low"] >= level) for b in later)


def _equality_percentiles(swings: list[tuple[int, str, float]], kind: str) -> dict[int, float]:
    """For each swing of ``kind``: percentile rank (0..1) of its gap to the nearest
    other swing of the same kind among the instrument's own gaps. Lower = more equal."""
    pts = [s for s in swings if s[1] == kind]
    gaps = []
    for s in pts:
        others = [abs(s[2] - o[2]) for o in pts if o is not s]
        gaps.append(min(others) if others else None)
    known = [g for g in gaps if g is not None]
    out = {}
    for s, g in zip(pts, gaps):
        if g is not None and len(known) >= 3:
            out[s[0]] = percentile_rank(g, known)
    return out


def _day_week_levels(bars_by_tf: dict[str, list[dict]]) -> list[tuple[str, str, float, float]]:
    """Previous completed day / week extremes from daily bars as
    (label, 'H'|'L', price, epoch the level became a resting pool)."""
    d1 = [b for b in _clean(bars_by_tf.get("D1", [])) if finite(b.get("timestamp_epoch")) is not None]
    levels: list[tuple[str, str, float, float]] = []
    day = TIMEFRAME_SECONDS["D1"]
    if d1:
        end = d1[-1]["timestamp_epoch"] + day
        levels += [("previous day high", "H", d1[-1]["high"], end), ("previous day low", "L", d1[-1]["low"], end)]
    weeks: dict[tuple, list[dict]] = {}
    for b in d1:
        iso = datetime.fromtimestamp(b["timestamp_epoch"], tz=timezone.utc).isocalendar()
        weeks.setdefault((iso[0], iso[1]), []).append(b)
    if len(weeks) >= 2:
        wk = weeks[sorted(weeks)[-2]]
        end = max(b["timestamp_epoch"] for b in wk) + day
        levels += [("previous week high", "H", max(b["high"] for b in wk), end),
                   ("previous week low", "L", min(b["low"] for b in wk), end)]
    return levels


class LiquidityPools:
    key = "liquidity"
    family = "liquidity"
    covers_methods = ("liquidity_pools", "liquidity_sweep")
    covers_lenses: tuple[str, ...] = ()

    def read(self, symbol: str, bars_by_tf: dict[str, list[dict]]) -> list[ConceptReading]:
        ref_close, ref_tf = reference_price(bars_by_tf)
        day_week = _day_week_levels(bars_by_tf)
        readings = []
        for tf, raw in bars_by_tf.items():
            if tf not in TIMEFRAME_SECONDS or TIMEFRAME_SECONDS[tf] < 300:
                continue
            bars = _clean(raw)
            if len(bars) < 8:
                continue
            reading = self._reading(symbol, tf, bars, day_week, ref_close, ref_tf)
            if reading is not None:
                readings.append(reading)
        return readings

    # ------------------------------------------------------------------
    def _reading(self, symbol, tf, bars, day_week, ref_close, ref_tf):
        last = bars[-1]
        swings = fractal_swings(bars[:-1])          # pools exist before the latest bar acts on them
        eq_h = _equality_percentiles(swings, "H")
        eq_l = _equality_percentiles(swings, "L")
        pools: list[dict] = []
        for idx, kind, price in swings:
            if _untouched(bars[:-1], idx, kind, price):
                pools.append({"kind": kind, "price": price, "source": "swing", "index": idx,
                              "equality_percentile": (eq_h if kind == "H" else eq_l).get(idx)})
        for label, kind, price, since in day_week:
            # A previous-day/week extreme is a pool only until a completed bar after it formed has traded through it.
            after = [b for b in bars[:-1]
                     if finite(b.get("timestamp_epoch")) is not None and b["timestamp_epoch"] >= since]
            if all((b["high"] <= price) if kind == "H" else (b["low"] >= price) for b in after):
                pools.append({"kind": kind, "price": price, "source": label, "index": None,
                              "equality_percentile": None})

        # --- sweep on the latest completed bar --------------------------------------
        history = bars[:-1]
        swept = None
        for p in pools:
            if p["kind"] == "H" and last["high"] > p["price"] and last["close"] < p["price"]:
                cand = ("H", p)
            elif p["kind"] == "L" and last["low"] < p["price"] and last["close"] > p["price"]:
                cand = ("L", p)
            else:
                continue
            kind, pool = cand
            depth = (last["high"] - pool["price"]) if kind == "H" else (pool["price"] - last["low"])
            if swept is None or depth > swept[2]:
                swept = (kind, pool, depth)

        epoch = finite(last.get("timestamp_epoch")) if last.get("source_timestamp_verified") is True else None
        px = last["close"]
        above = sorted((p for p in pools if p["price"] > px), key=lambda p: p["price"])
        below = sorted((p for p in pools if p["price"] < px), key=lambda p: -p["price"])
        facts = {
            "pools_above": [{"price": p["price"], "source": p["source"], "kind": p["kind"],
                             "equality_percentile": p["equality_percentile"]} for p in above[:5]],
            "pools_below": [{"price": p["price"], "source": p["source"], "kind": p["kind"],
                             "equality_percentile": p["equality_percentile"]} for p in below[:5]],
            "swept": None, "history_candles": len(bars),
        }
        implications: list[Implication] = []
        if swept is not None:
            kind, pool, depth = swept
            buy_side = kind == "H"                                   # highs hold buy-side liquidity
            side = SHORT if buy_side else LONG
            rng = last["high"] - last["low"]
            rejection = ((last["high"] - last["close"]) if buy_side else (last["close"] - last["low"])) / rng if rng > 0 else None
            rej_sample = [((b["high"] - b["close"]) if buy_side else (b["close"] - b["low"])) / (b["high"] - b["low"])
                          for b in history if b["high"] > b["low"]]
            rej_pct = percentile_rank(rejection, rej_sample) if rejection is not None else None
            strength = mean_known([rej_pct, (pool["equality_percentile"] and 1.0 - pool["equality_percentile"])])
            stop = last["high"] if buy_side else last["low"]
            objective = (below[0]["price"] if below else None) if buy_side else (above[0]["price"] if above else None)
            facts["swept"] = {"pool_price": pool["price"], "source": pool["source"], "kind": kind,
                              "sweep_extreme": stop, "depth": depth, "rejection_percentile": rej_pct}
            requires = ["lower-timeframe structure shift confirming the rejection"]
            if objective is None:
                requires.append("an objective: no untouched pool in the trade direction")
            implications.append(Implication(
                direction=side,
                statement=(f"{tf}: the {'buy-side' if buy_side else 'sell-side'} liquidity at {pool['price']:.10g} "
                           f"({pool['source']}) was swept to {stop:.10g} and rejected (closed back "
                           f"{'below' if buy_side else 'above'} it)."),
                strength=strength, activated=True,
                activation_reason=(f"completed bar traded through {pool['price']:.10g} and closed back inside "
                                   f"at {last['close']:.10g}"),
                invalidation_price=stop,
                invalidation_reason=(f"completed-bar close {'above' if buy_side else 'below'} the sweep extreme "
                                     f"{stop:.10g} means the raid was accepted, not rejected"),
                objective_price=objective,
                requires=tuple(requires),
                conflicts_with=(),
                management={"invalid_on_completed_close_beyond": stop, "objective": objective, "timeframe": tf},
            ))
        if not implications and not (above or below):
            return None
        obs = (f"{tf} liquidity: " + (f"swept {facts['swept']['source']} at {facts['swept']['pool_price']:.10g}; "
                                      if swept else "no sweep; ")
               + f"{len(above)} untouched pool(s) above, {len(below)} below.")
        if not implications:
            # Pools alone: facts for objectives; no side is implied.
            return ConceptReading(
                concept=self.key, timeframe=tf, domain=EvidenceDomain.LIQUIDITY, applicable=True,
                applicability_reason="untouched liquidity pools around price (draws on liquidity)",
                observation=obs, implications=(), facts=facts, source_bar_epoch=epoch,
                reference_close=ref_close, reference_timeframe=ref_tf,
            )
        return ConceptReading(
            concept=self.key, timeframe=tf, domain=EvidenceDomain.LIQUIDITY, applicable=True,
            applicability_reason="a completed bar swept an untouched pool and closed back inside",
            observation=obs, implications=tuple(implications), facts=facts, source_bar_epoch=epoch,
            reference_close=ref_close, reference_timeframe=ref_tf,
            unknowns=tuple(sorted({r for i in implications for r in i.requires})),
        )
