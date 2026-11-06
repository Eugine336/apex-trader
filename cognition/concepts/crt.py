"""Candle Range Theory (CRT) — the full reading.

The concept (ICT / Smart-Money lineage): on one timeframe, **candle 1 (C1)** sets a
range whose high and low hold resting liquidity. **Candle 2 (C2)** trades beyond
one extreme and *closes back inside* the range (the manipulation / stop run); a
close beyond the extreme cancels it, because that is acceptance, i.e. a real
breakout. **Candle 3 (C3)** is expected to deliver toward the opposite extreme
(the distribution), with the range midpoint (50%) as the first milestone and the
opposite extreme as the objective. The stop belongs beyond the sweep extreme. A
higher timeframe defines the range and a lower timeframe times the entry. A sweep
that also takes other resting liquidity carries more weight than one in open space.

This module reads that whole structure from completed bars:

* which phase the sequence is in (C2 just closed / C3 closed and delivering /
  C3 failed / delivered), and the live implication for LONG or SHORT;
* the invalidation (a completed close beyond the sweep extreme), objective and
  milestone, as prices;
* how decisively C2 rejected the extreme, ranked against the instrument's own
  history on that timeframe (no configured thresholds);
* whether a lower timeframe has already shifted structure back inside, and
  which higher-timeframe liquidity the sweep also took (confluence);
* what to watch once a position exists.

It never picks a trade; it supplies a reading for the Brain to weigh.
"""

from __future__ import annotations

from typing import Optional

from cognition.contracts import EvidenceDomain
from cognition.concepts.base import (
    FLAT, LONG, SHORT, TIMEFRAME_SECONDS, ConceptReading, Implication, finite,
    mean_known, percentile_rank, reference_price,
)


def _clean(bars: list[dict]) -> list[dict]:
    out = []
    for b in bars or []:
        o, h, l, c = (finite(b.get(k)) for k in ("open", "high", "low", "close"))
        if None in (o, h, l, c) or h < l:
            continue
        out.append({**b, "open": o, "high": h, "low": l, "close": c})
    return out


def _classify(c1: dict, c2: dict) -> Optional[str]:
    """Relation of C2 to C1's range. None when C1 has no range."""
    r1 = c1["high"] - c1["low"]
    if r1 <= 0:
        return None
    swept_low = c2["low"] < c1["low"]
    swept_high = c2["high"] > c1["high"]
    if swept_low and swept_high:
        return "two_sided"
    if swept_low:
        return "bullish_crt" if c2["close"] > c1["low"] else "closed_below"
    if swept_high:
        return "bearish_crt" if c2["close"] < c1["high"] else "closed_above"
    return "inside"


def _rejection(c: dict, bullish: bool) -> Optional[float]:
    rng = c["high"] - c["low"]
    if rng <= 0:
        return None
    return (c["close"] - c["low"]) / rng if bullish else (c["high"] - c["close"]) / rng


def _history(bars: list[dict], bullish: bool) -> tuple[list[float], list[float]]:
    """The instrument's own distributions on this timeframe: (rejection of every
    candle, reclaim depth of every earlier CRT of this polarity)."""
    rejections = [r for r in (_rejection(b, bullish) for b in bars) if r is not None]
    reclaims: list[float] = []
    for c1, c2 in zip(bars, bars[1:]):
        kind = _classify(c1, c2)
        r1 = c1["high"] - c1["low"]
        if kind == "bullish_crt" and bullish:
            reclaims.append((c2["close"] - c1["low"]) / r1)
        elif kind == "bearish_crt" and not bullish:
            reclaims.append((c1["high"] - c2["close"]) / r1)
    return rejections, reclaims


def _lower_timeframe_confirmation(
    bars_by_tf: dict[str, list[dict]], tf: str, c2: dict, bullish: bool,
) -> Optional[bool]:
    """Has the next-lower timeframe shifted structure back inside after the sweep?

    Within C2's interval, the swing preceding the extreme (the highest high before
    the lowest low for a bullish sweep, mirrored for a bearish one) must be closed
    through afterwards. ``None`` when there is no lower-timeframe data to judge.
    """
    seconds = TIMEFRAME_SECONDS.get(tf)
    lowers = [t for t in bars_by_tf if TIMEFRAME_SECONDS.get(t, 10**9) < (seconds or 0)]
    if not seconds or not lowers:
        return None
    ltf = max(lowers, key=lambda t: TIMEFRAME_SECONDS[t])
    start = finite(c2.get("timestamp_epoch"))
    if start is None:
        return None
    rows = [b for b in _clean(bars_by_tf[ltf])
            if finite(b.get("timestamp_epoch")) is not None
            and finite(b.get("timestamp_epoch")) >= start]
    inside = [b for b in rows if finite(b["timestamp_epoch"]) < start + seconds]
    if len(inside) < 2:
        return None
    ext_idx = min(range(len(inside)), key=lambda i: inside[i]["low"]) if bullish \
        else max(range(len(inside)), key=lambda i: inside[i]["high"])
    pre = inside[: ext_idx + 1]
    swing = max(b["high"] for b in pre) if bullish else min(b["low"] for b in pre)
    after = rows[ext_idx + 1:]
    if not after:
        return False
    return any(b["close"] > swing for b in after) if bullish else any(b["close"] < swing for b in after)


def _higher_timeframe_liquidity_taken(
    bars_by_tf: dict[str, list[dict]], tf: str, bars: list[dict],
    c1: dict, c2: dict, bullish: bool,
) -> list[str]:
    """Higher-timeframe extremes inside the swept zone that were still untouched
    until this sweep (resting liquidity the sweep also took)."""
    seconds = TIMEFRAME_SECONDS.get(tf, 0)
    zone_lo, zone_hi = (c2["low"], c1["low"]) if bullish else (c1["high"], c2["high"])
    c2_open = finite(c2.get("timestamp_epoch"))
    taken: list[str] = []
    for htf, hbars in bars_by_tf.items():
        hs = TIMEFRAME_SECONDS.get(htf, 0)
        if hs <= seconds:
            continue
        for hb in _clean(hbars):
            level = hb["low"] if bullish else hb["high"]
            if not zone_lo <= level <= zone_hi:
                continue
            h_open = finite(hb.get("timestamp_epoch"))
            if h_open is None or c2_open is None:
                continue
            formed = h_open + hs
            between = [b for b in bars
                       if finite(b.get("timestamp_epoch")) is not None
                       and formed <= b["timestamp_epoch"] < c2_open]
            untouched = all((b["low"] > level) if bullish else (b["high"] < level) for b in between)
            if untouched:
                taken.append(f"{htf} {'low' if bullish else 'high'} {level:.10g}")
    return taken


class CandleRangeTheory:
    key = "crt"
    family = "ict_smc_named_models"
    covers_methods = ("crt",)       # analytical_knowledge row keys fully read by this module
    covers_lenses = ("crt",)        # strategy_knowledge lens keys fully read by this module

    def read(self, symbol: str, bars_by_tf: dict[str, list[dict]]) -> list[ConceptReading]:
        readings: list[ConceptReading] = []
        ref_close, ref_tf = reference_price(bars_by_tf)
        for tf, raw in bars_by_tf.items():
            if tf not in TIMEFRAME_SECONDS or TIMEFRAME_SECONDS[tf] < 300:
                continue
            bars = _clean(raw)
            reading = self._read_timeframe(symbol, tf, bars, bars_by_tf, ref_close, ref_tf)
            if reading is not None:
                readings.append(reading)
        return readings

    # ------------------------------------------------------------------
    def _read_timeframe(self, symbol, tf, bars, bars_by_tf, ref_close, ref_tf):
        if len(bars) < 3:
            return None
        latest = bars[-1]
        source_epoch = finite(latest.get("timestamp_epoch")) \
            if latest.get("source_timestamp_verified") is True else None

        # Phase A: C2 is the latest completed candle (C3 is forming now).
        rel_a = _classify(bars[-2], bars[-1])
        if rel_a in ("bullish_crt", "bearish_crt"):
            return self._crt(symbol, tf, bars, bars_by_tf, c1=bars[-2], c2=bars[-1], c3=None,
                             bullish=rel_a == "bullish_crt", source_epoch=source_epoch,
                             ref_close=ref_close, ref_tf=ref_tf)
        # Phase B: C3 has already completed after a CRT.
        rel_b = _classify(bars[-3], bars[-2])
        if rel_b in ("bullish_crt", "bearish_crt"):
            return self._crt(symbol, tf, bars, bars_by_tf, c1=bars[-3], c2=bars[-2], c3=bars[-1],
                             bullish=rel_b == "bullish_crt", source_epoch=source_epoch,
                             ref_close=ref_close, ref_tf=ref_tf)
        # A close beyond the previous candle's extreme, or an outside bar, is not a CRT.
        # Both happen on a large share of candles, so they are not emitted as readings.
        return None

    # ------------------------------------------------------------------
    def _crt(self, symbol, tf, bars, bars_by_tf, *, c1, c2, c3, bullish, source_epoch, ref_close, ref_tf):
        r1 = c1["high"] - c1["low"]
        mid = c1["low"] + r1 / 2.0
        extreme_c1 = c1["low"] if bullish else c1["high"]
        objective = c1["high"] if bullish else c1["low"]
        sweep_extreme = c2["low"] if bullish else c2["high"]
        side = LONG if bullish else SHORT
        word = "bullish" if bullish else "bearish"
        swept = "low" if bullish else "high"

        # How decisively C2 rejected the extreme, ranked against this instrument's own history.
        history = [b for b in bars if b is not c2 and b is not c3]
        rejections, reclaims = _history(history, bullish)
        rejection = _rejection(c2, bullish)
        reclaim = ((c2["close"] - c1["low"]) / r1) if bullish else ((c1["high"] - c2["close"]) / r1)
        excursion = (c1["low"] - c2["low"]) / r1 if bullish else (c2["high"] - c1["high"]) / r1
        rej_pct = percentile_rank(rejection, rejections) if rejection is not None else None
        rec_pct = percentile_rank(reclaim, reclaims) if len(reclaims) >= 2 else None
        strength = mean_known([rej_pct, rec_pct])

        ltf = _lower_timeframe_confirmation(bars_by_tf, tf, c2, bullish)
        htf_taken = _higher_timeframe_liquidity_taken(bars_by_tf, tf, bars, c1, c2, bullish)

        # Where price is now relative to C1's range (0 = at the swept extreme, 1 = objective).
        last = c3 if c3 is not None else c2
        progress = ((last["close"] - c1["low"]) / r1) if bullish else ((c1["high"] - last["close"]) / r1)
        failed = c3 is not None and ((c3["close"] < sweep_extreme) if bullish else (c3["close"] > sweep_extreme))
        delivered = progress >= 1.0
        phase = ("c2_complete" if c3 is None else
                 "failed_accepted_beyond_sweep" if failed else
                 "objective_delivered" if delivered else "c3_complete_delivering")
        intact = not failed and not delivered

        milestones = tuple(m for m in (mid,) if (m > last["close"] if bullish else m < last["close"]))
        requires: list[str] = []
        if ltf is False:
            requires.append("lower-timeframe structure shift back inside the range")
        if ltf is None:
            requires.append("lower-timeframe confirmation (no lower-timeframe data to judge)")
        conflicts: list[str] = []
        if not htf_taken:
            conflicts.append("sweep in open space: no higher-timeframe liquidity was also taken")
        if strength is None:
            requires.append("history too short to rank the quality of the sweep")

        reason = (
            f"C2 swept C1's {swept} ({extreme_c1:.10g}) to {sweep_extreme:.10g} and closed back "
            f"inside at {c2['close']:.10g}"
            + ("; C3 completed and the sweep extreme still holds" if (c3 is not None and intact) else "")
        )
        statement = (
            f"{word.capitalize()} CRT on {tf}: the {swept} of C1 was swept and rejected; expected "
            f"delivery toward C1's {'high' if bullish else 'low'} {objective:.10g} via the midpoint "
            f"{mid:.10g}. Phase: {phase}; progress through C1's range {progress:.2f}."
        )
        if failed:
            statement = (f"The {word} CRT on {tf} failed: C3 closed beyond the sweep extreme "
                         f"{sweep_extreme:.10g}, so the sweep was accepted rather than rejected.")
        if delivered:
            statement = (f"The {word} CRT on {tf} has delivered to its objective {objective:.10g}; "
                         "the range is spent and a new range forms.")

        implication = Implication(
            direction=side,
            statement=statement,
            strength=strength,
            activated=intact,
            activation_reason=reason if intact else "",
            invalidation_price=sweep_extreme,
            invalidation_reason=(
                f"completed-bar close {'below' if bullish else 'above'} the sweep extreme "
                f"{sweep_extreme:.10g} on {tf} means the sweep was accepted (CRT failed)"),
            objective_price=objective,
            milestones=milestones,
            requires=tuple(requires),
            conflicts_with=tuple(conflicts),
            management={
                "invalid_on_completed_close_beyond": sweep_extreme,
                "first_milestone": mid,
                "objective": objective,
                "timeframe": tf,
                "after_first_milestone": ("reassess: stalling or rejection at the midpoint weakens delivery "
                                          "to the objective; a completed-bar structure break against the "
                                          "trade weakens it further"),
                "after_objective": "range spent; take or trail the remainder and look for the next range",
            },
        )
        facts = {
            "phase": phase, "bias_direction": side,
            "c1": {k: c1[k] for k in ("open", "high", "low", "close")},
            "c2": {k: c2[k] for k in ("open", "high", "low", "close")},
            "c3": ({k: c3[k] for k in ("open", "high", "low", "close")} if c3 else None),
            "range_c1": r1, "midpoint": mid, "objective": objective, "sweep_extreme": sweep_extreme,
            "sweep_excursion_in_c1_ranges": excursion, "reclaim_depth": reclaim, "c2_rejection": rejection,
            "rejection_percentile": rej_pct, "reclaim_percentile": rec_pct,
            "history_candles": len(rejections), "history_crt_events": len(reclaims),
            "progress_through_range": progress, "lower_timeframe_structure_shift": ltf,
            "higher_timeframe_liquidity_taken": htf_taken,
        }
        obs = (f"{word.capitalize()} CRT on {tf} ({phase}): C1 range {c1['low']:.10g}-{c1['high']:.10g}, "
               f"C2 swept to {sweep_extreme:.10g} and closed {c2['close']:.10g}; midpoint {mid:.10g}; "
               f"invalidation {sweep_extreme:.10g}; objective {objective:.10g}.")
        return ConceptReading(
            concept=self.key, timeframe=tf, domain=EvidenceDomain.LIQUIDITY, applicable=True,
            applicability_reason="three-candle range / sweep / close-back-inside sequence present on completed bars",
            observation=obs, implications=(implication,), facts=facts,source_bar_epoch=source_epoch,
 source_timestamp_verified=source_epoch is not None,
            reference_close=ref_close, reference_timeframe=ref_tf,
            unknowns=tuple(requires),
        )
