"""Contextual price-delivery concept readings from completed OHLC bars.

No reading is an entry signal. Definitions are versioned in facts, missing data
stays unknown, and OHLC proxies are not represented as institutional order data.
"""
from __future__ import annotations

from cognition.contracts import EvidenceDomain
from cognition.concepts.base import (
    FLAT, LONG, SHORT, TIMEFRAME_SECONDS, ConceptReading, Implication,
    finite, percentile_rank, reference_price,
)


def _clean(rows):
    out = []
    for row in rows or []:
        o, h, l, c = (finite(row.get(k)) for k in ("open", "high", "low", "close"))
        if None in (o, h, l, c) or h < l or h < max(o, c) or l > min(o, c):
            continue
        out.append({**row, "open": o, "high": h, "low": l, "close": c})
    return out


def _epoch(bar):
    value = finite(bar.get("timestamp_epoch"))
    return value if value and bar.get("source_timestamp_verified") is True else None


def _imp(direction, statement, active, invalidation, *, stop=None, target=None,
         requires=(), conflicts=(), strength=None):
    return Implication(
        direction=direction, statement=statement, strength=strength,
        activated=bool(active),
        activation_reason=statement if active else "The defining condition is not currently satisfied",
        invalidation_price=stop, invalidation_reason=invalidation,
        objective_price=target, requires=tuple(requires), conflicts_with=tuple(conflicts),
        management={"reassess_on": "next completed bar and any interaction with the defined zone"},
    )


def _record(concept, tf, domain, observation, implications, facts, bars, ref_close, ref_tf, unknowns=()):
    return ConceptReading(
        concept=concept, timeframe=tf, domain=domain, applicable=True,
        applicability_reason="The concept's explicitly defined completed-bar structure is present",
        observation=observation, implications=tuple(implications), facts=facts,
        source_bar_epoch=_epoch(bars[-1]),
        source_timestamp_verified=_epoch(bars[-1]) is not None, reference_close=ref_close,
        reference_timeframe=ref_tf, unknowns=tuple(unknowns),
    )


class PriceDeliveryConcepts:
    """Read fair-value gaps, order blocks, balanced ranges and dealing-range location."""
    key = "price_delivery"
    family = "institutional_price_action"
    covers_methods = ("fair_value_gap", "order_blocks", "balanced_price_range", "dealing_range", "break_retest", "gap_analysis", "breaker_block", "displacement", "ict_smc")
    covers_lenses = ()

    def read(self, symbol: str, bars_by_tf: dict[str, list[dict]]) -> list[ConceptReading]:
        close, close_tf = reference_price(bars_by_tf)
        readings = []
        for tf, raw in bars_by_tf.items():
            if tf not in TIMEFRAME_SECONDS or TIMEFRAME_SECONDS[tf] < 300:
                continue
            bars = _clean(raw)
            if len(bars) < 22:
                continue
            readings.extend(self._fvg(tf, bars, close, close_tf))
            readings.extend(self._order_block(tf, bars, close, close_tf))
            readings.extend(self._bpr(tf, bars, close, close_tf))
            readings.append(self._dealing_range(tf, bars, close, close_tf))
            readings.extend(self._break_retest(tf, bars, close, close_tf))
            readings.extend(self._gaps(tf, bars, close, close_tf))
            readings.extend(self._displacement(tf, bars, close, close_tf))
            readings.extend(self._breaker_block(tf, bars, close, close_tf))
        return readings

    def _fvg(self, tf, bars, close, close_tf):
        # Three-candle non-overlap. Return the most recent imbalance only.
        for i in range(len(bars) - 1, 1, -1):
            a, b, c = bars[i - 2], bars[i - 1], bars[i]
            bull, bear = c["low"] > a["high"], c["high"] < a["low"]
            if not (bull or bear):
                continue
            side = LONG if bull else SHORT
            lo, hi = (a["high"], c["low"]) if bull else (c["high"], a["low"])
            later = bars[i + 1:]
            touched = any(x["low"] <= hi and x["high"] >= lo for x in later)
            filled = any(x["low"] <= lo for x in later) if bull else any(x["high"] >= hi for x in later)
            invalid = any(x["close"] < lo for x in later) if bull else any(x["close"] > hi for x in later)
            active = not invalid and not filled
            sample = [abs(x["close"] - x["open"]) / max(x["high"] - x["low"], 1e-12)
                      for x in bars[max(0, i - 20):i] if x["high"] > x["low"]]
            body = abs(b["close"] - b["open"]) / max(b["high"] - b["low"], 1e-12)
            strength = percentile_rank(body, sample)
            return [_record(
                "fair_value_gap", tf, EvidenceDomain.STRUCTURE,
                f"{side} three-candle imbalance {lo:.10g}–{hi:.10g}; touched={touched}; "
                f"fully_filled={filled}; invalidated={invalid}.",
                [_imp(side, f"{side} imbalance remains active" if active else
                      f"{side} imbalance has been mitigated or invalidated", active,
                      "Completed close through the far edge invalidates the gap",
                      stop=lo if bull else hi,
                      requires=("price to revisit the gap before a mitigation thesis",) if not touched else (),
                      conflicts=(("gap has been fully filled",) if filled else (("gap has already been touched",) if touched else ())), strength=strength),
                 _imp(FLAT, "A gap is a location hypothesis, not proof of future fill", False,
                      "Not applicable")],
                {"lower": lo, "upper": hi, "midpoint": (lo + hi) / 2, "direction": side,
                 "touched": touched, "fully_filled": filled, "invalidated": invalid,
                 "displacement_body_percentile": strength, "definition": "three_candle_non_overlap_v1"},
                bars, close, close_tf,
                ("OHLC imbalance does not prove unfilled institutional orders.",)
            )]
        return []

    def _order_block(self, tf, bars, close, close_tf):
        # Last opposite-colour candle within three bars before a relative impulse.
        for i in range(len(bars) - 1, max(2, len(bars) - 18), -1):
            impulse = bars[i]
            body = abs(impulse["close"] - impulse["open"]) / max(impulse["high"] - impulse["low"], 1e-12)
            sample = [abs(x["close"] - x["open"]) / max(x["high"] - x["low"], 1e-12)
                      for x in bars[max(0, i - 20):i] if x["high"] > x["low"]]
            rank = percentile_rank(body, sample)
            if rank is None or rank < 0.80 or impulse["close"] == impulse["open"]:
                continue
            bull = impulse["close"] > impulse["open"]
            origin = next((j for j in range(i - 1, max(-1, i - 4), -1)
                           if (bars[j]["close"] < bars[j]["open"] if bull
                               else bars[j]["close"] > bars[j]["open"])), None)
            if origin is None:
                continue
            block = bars[origin]
            lo, hi = block["low"], block["high"]
            later = bars[i + 1:]
            mitigated = any(x["low"] <= hi and x["high"] >= lo for x in later)
            invalid = any(x["close"] < lo for x in later) if bull else any(x["close"] > hi for x in later)
            side = LONG if bull else SHORT
            return [_record(
                "order_blocks", tf, EvidenceDomain.STRUCTURE,
                f"Candidate {side} origin-candle zone {lo:.10g}–{hi:.10g}; impulse body percentile "
                f"{rank:.2f}; mitigated={mitigated}; invalidated={invalid}.",
                [_imp(side, f"{side} origin zone {'invalidated' if invalid else 'intact'}", not invalid,
                      "Completed close through distal zone boundary invalidates the block",
                      stop=lo if bull else hi,
                      requires=("retest and reaction evidence",) if not mitigated else (),
                      conflicts=("zone has already been mitigated",) if mitigated else (), strength=rank),
                 _imp(FLAT, "Origin-candle zone requires a reaction before directional interpretation", False,
                      "Not applicable")],
                {"lower": lo, "upper": hi, "direction": side, "origin_index": origin,
                 "impulse_index": i, "body_percentile": rank, "mitigated": mitigated,
                 "invalidated": invalid, "definition": "opposite_origin_before_relative_impulse_v1"},
                bars, close, close_tf,
                ("OHLC cannot establish institutional ownership or resting order inventory.",)
            )]
        return []

    def _bpr(self, tf, bars, close, close_tf):
        gaps = []
        for i in range(2, len(bars)):
            a, c = bars[i - 2], bars[i]
            if c["low"] > a["high"]:
                gaps.append(("bullish", a["high"], c["low"], i))
            elif c["high"] < a["low"]:
                gaps.append(("bearish", c["high"], a["low"], i))
        bulls, bears = [g for g in gaps if g[0] == "bullish"], [g for g in gaps if g[0] == "bearish"]
        for bull in reversed(bulls):
            for bear in reversed(bears):
                lo, hi = max(bull[1], bear[1]), min(bull[2], bear[2])
                if lo >= hi or abs(bull[3] - bear[3]) > 20:
                    continue
                latest = max(bull[3], bear[3])
                invalid = any(x["close"] < lo or x["close"] > hi for x in bars[latest + 1:])
                return [_record(
                    "balanced_price_range", tf, EvidenceDomain.STRUCTURE,
                    f"Opposing FVGs overlap at {lo:.10g}–{hi:.10g}; latest close "
                    f"{bars[-1]['close']:.10g}; invalidated={invalid}.",
                    [_imp(FLAT, "BPR is a two-sided reaction area; direction requires acceptance or rejection",
                          not invalid, "Completed close outside the overlap invalidates the balance zone",
                          requires=("observe which side accepts or rejects the overlap",))],
                    {"lower": lo, "upper": hi, "bullish_fvg_index": bull[3],
                     "bearish_fvg_index": bear[3], "invalidated": invalid,
                     "definition": "overlap_of_opposing_three_candle_fvgs_v1"},
                    bars, close, close_tf,
                    ("OHLC overlap does not establish order-book balance.",)
                )]
        return []

    def _dealing_range(self, tf, bars, close, close_tf):
        # Declared rolling range avoids pretending that a subjective anchor is canonical.
        window = bars[-20:]
        high, low = max(x["high"] for x in window), min(x["low"] for x in window)
        mid, last = (high + low) / 2, bars[-1]["close"]
        location = "premium" if last > mid else "discount" if last < mid else "equilibrium"
        return _record(
            "dealing_range", tf, EvidenceDomain.STRUCTURE,
            f"Rolling 20-bar reference range {low:.10g}–{high:.10g}; equilibrium {mid:.10g}; "
            f"latest close in {location}.",
            [_imp(FLAT, f"Price is in {location} of the declared rolling reference range", True,
                  "A new rolling extreme changes the reference range",
                  requires=("validate anchors against external market structure",))],
            {"range_low": low, "range_high": high, "equilibrium": mid, "latest_close": last,
             "location": location, "lookback_bars": 20,
             "definition": "rolling_20_completed_bar_reference_v1"},
            bars, close, close_tf,
            ("A rolling reference range is not a uniquely defined institutional dealing range.",)
        )

    def _break_retest(self, tf, bars, close, close_tf):
        # Find the most recent close beyond a prior 20-bar extreme. Only bars
        # after that break can establish retest, hold or failure.
        start = max(20, len(bars) - 12)
        for i in range(len(bars) - 1, start - 1, -1):
            prior = bars[i - 20:i]
            if len(prior) != 20:
                continue
            ceiling = max(x["high"] for x in prior)
            floor = min(x["low"] for x in prior)
            current = bars[i]
            if current["close"] > ceiling and bars[i-1]["close"] <= ceiling:
                side, level = LONG, ceiling
            elif current["close"] < floor and bars[i-1]["close"] >= floor:
                side, level = SHORT, floor
            else:
                continue
            later = bars[i+1:]
            if side == LONG:
                retested = any(x["low"] <= level for x in later)
                invalid = any(x["close"] <= level for x in later)
                accepted = bars[-1]["close"] > level
            else:
                retested = any(x["high"] >= level for x in later)
                invalid = any(x["close"] >= level for x in later)
                accepted = bars[-1]["close"] < level
            active = retested and accepted and not invalid
            return [_record(
                "break_retest", tf, EvidenceDomain.STRUCTURE,
                f"{side} close broke the prior 20-bar boundary {level:.10g}; "
                f"post-break retest={retested}; latest acceptance={accepted}; invalidated={invalid}.",
                [_imp(side, f"{side} breakout/retest hypothesis at {level:.10g}", active,
                      "A completed close back through the broken boundary invalidates the retest",
                      stop=level, requires=("post-break retest", "completed hold beyond boundary",
                                            "independent participation evidence"),
                      conflicts=("breakout failed back into the prior range",) if invalid else ()),
                 _imp(FLAT, "Breakout without a completed retest and hold remains provisional",
                      not active, "Not applicable")],
                {"direction": side, "boundary": level, "break_index": i,
                 "retest": retested, "accepted": accepted, "invalidated": invalid,
                 "lookback_bars": 20, "definition": "prior_20_bar_close_break_then_post_break_retest_v1"},
                bars, close, close_tf,
                ("A 20-bar boundary is an explicit mechanical reference, not necessarily a major market swing.",)
            )]
        return []



    def _displacement(self, tf, bars, close, close_tf):
        current = bars[-1]
        span = current["high"] - current["low"]
        if span <= 0:
            return []
        prior_ranges = [x["high"]-x["low"] for x in bars[-21:-1] if x["high"]>x["low"]]
        if len(prior_ranges) < 10:
            return []
        baseline = sorted(prior_ranges)[len(prior_ranges)//2]
        body_ratio = abs(current["close"]-current["open"])/span
        multiple = span/baseline if baseline>0 else 0.0
        close_location = (current["close"]-current["low"])/span
        displaced = multiple>=1.5 and body_ratio>=0.65
        side = LONG if current["close"]>current["open"] else SHORT if current["close"]<current["open"] else FLAT
        return [_record(
            "displacement",tf,EvidenceDomain.STRUCTURE,
            f"Latest completed candle range={multiple:.2f}x prior median; body/range={body_ratio:.2f}; "
            f"close-location={close_location:.2f}; displacement={displaced}; side={side}.",
            [_imp(side,f"{side} relative-range displacement" if displaced else "No qualifying displacement on latest completed bar",
                  displaced and side!=FLAT,"A completed close back through the displacement origin invalidates the impulse",
                  stop=current["open"],requires=("confirmed structure break or shift","participation/volume confirmation"),
                  conflicts=("range expansion without directional body dominance",) if multiple>=1.5 and body_ratio<0.65 else ())],
            {"direction":side,"range_multiple":multiple,"body_ratio":body_ratio,
             "close_location":close_location,"displacement":displaced,
             "prior_median_range":baseline,"definition":"latest_range_1_5x_median_and_body_ratio_0_65_v1"},
            bars,close,close_tf,
            ("OHLC displacement is a price expansion proxy; it does not prove aggressive institutional execution.",)
        )]

    def _breaker_block(self, tf, bars, close, close_tf):
        # An origin zone is a breaker only after the original block is invalidated
        # and price subsequently retests it from the opposite side.
        for i in range(len(bars)-1,max(2,len(bars)-30),-1):
            impulse=bars[i]
            span=impulse["high"]-impulse["low"]
            if span<=0:
                continue
            sample=[abs(x["close"]-x["open"])/max(x["high"]-x["low"],1e-12)
                    for x in bars[max(0,i-20):i] if x["high"]>x["low"]]
            ratio=abs(impulse["close"]-impulse["open"])/span
            rank=percentile_rank(ratio,sample)
            if rank is None or rank<0.80 or impulse["close"]==impulse["open"]:
                continue
            original_bull=impulse["close"]>impulse["open"]
            origin=next((j for j in range(i-1,max(-1,i-4),-1)
                         if (bars[j]["close"]<bars[j]["open"] if original_bull
                             else bars[j]["close"]>bars[j]["open"])),None)
            if origin is None:
                continue
            zone=bars[origin]
            lo,hi=zone["low"],zone["high"]
            invalidation_index=None
            for j in range(i+1,len(bars)):
                if (bars[j]["close"]<lo if original_bull else bars[j]["close"]>hi):
                    invalidation_index=j
                    break
            if invalidation_index is None:
                continue
            later=bars[invalidation_index+1:]
            if original_bull:
                side=SHORT
                retested=any(x["high"]>=lo and x["low"]<=hi for x in later)
                accepted=bars[-1]["close"]<lo
                invalid=any(x["close"]>hi for x in later)
                stop=hi
            else:
                side=LONG
                retested=any(x["low"]<=hi and x["high"]>=lo for x in later)
                accepted=bars[-1]["close"]>hi
                invalid=any(x["close"]<lo for x in later)
                stop=lo
            active=retested and accepted and not invalid
            return [_record(
                "breaker_block",tf,EvidenceDomain.STRUCTURE,
                f"Original {'bullish' if original_bull else 'bearish'} origin block {lo:.10g}–{hi:.10g} "
                f"was invalidated at bar {invalidation_index}; opposite-side retest={retested}; "
                f"acceptance={accepted}; invalidated_again={invalid}.",
                [_imp(side,f"{side} breaker-block role reversal",active,
                      "A completed close through the breaker zone's far edge invalidates the role reversal",
                      stop=stop,requires=("retest from the opposite side","completed acceptance away from zone",
                                          "independent structure and participation evidence"),
                      conflicts=("price reclaimed the original block",) if invalid else ()),
                 _imp(FLAT,"An invalidated order block is not a breaker until the role-reversal retest occurs",
                      not active,"Not applicable")],
                {"direction":side,"zone_low":lo,"zone_high":hi,"origin_index":origin,
                 "impulse_index":i,"original_block_direction":"LONG" if original_bull else "SHORT",
                 "invalidation_index":invalidation_index,"retested":retested,"accepted":accepted,
                 "invalidated_again":invalid,"body_percentile":rank,
                 "definition":"origin_block_invalidation_then_opposite_side_retest_v1"},
                bars,close,close_tf,
                ("Breaker block is inferred from OHLC origin/impulse geometry; hidden order inventory is not observable.",)
            )]
        return []

    def _gaps(self, tf, bars, close, close_tf):
        seconds = TIMEFRAME_SECONDS.get(tf)
        if seconds is None:
            return []
        for i in range(len(bars)-1, 0, -1):
            previous, current = bars[i-1], bars[i]
            if not (previous.get("source_timestamp_verified") is True
                    and current.get("source_timestamp_verified") is True):
                continue
            t0 = finite(previous.get("timestamp_epoch"))
            t1 = finite(current.get("timestamp_epoch"))
            if t0 is None or t1 is None or t1 <= t0 or t1-t0 < seconds*1.5:
                continue
            prior_close, opening = previous["close"], current["open"]
            if opening == prior_close:
                continue
            side = LONG if opening > prior_close else SHORT
            lo, hi = sorted((prior_close, opening))
            later = bars[i+1:]
            filled = any(x["low"] <= lo for x in later) if side == LONG else any(x["high"] >= hi for x in later)
            invalid = any(x["close"] < lo for x in later) if side == LONG else any(x["close"] > hi for x in later)
            accepted = bars[-1]["close"] > hi if side == LONG else bars[-1]["close"] < lo
            active = not filled and not invalid and accepted
            return [_record(
                "gap_analysis", tf, EvidenceDomain.STRUCTURE,
                f"{side} timestamped discontinuity after {t1-t0:.0f}s; prior close={prior_close:.10g}; "
                f"next open={opening:.10g}; gap zone={lo:.10g}–{hi:.10g}; "
                f"filled={filled}; accepted={accepted}; invalidated={invalid}.",
                [_imp(side, f"{side} gap remains unfilled with price accepted beyond the gap",
                      active, "A completed close through the opposite gap boundary invalidates the directional gap read",
                      stop=lo if side == LONG else hi,
                      requires=("continued acceptance and follow-through", "verify venue/session cause"),
                      conflicts=("gap has been filled or rejected",) if filled or invalid else ()),
                 _imp(FLAT, "A time discontinuity can be a session closure or missing feed data, not a price imbalance thesis",
                      True, "Not applicable", requires=("verify trading calendar and broker feed continuity",))],
                {"direction": side, "gap_low": lo, "gap_high": hi, "previous_close": prior_close,
                 "next_open": opening, "elapsed_seconds": t1-t0, "timeframe_seconds": seconds,
                 "filled": filled, "accepted": accepted, "invalidated": invalid,
                 "definition": "verified_time_gap_and_open_close_discontinuity_v1"},
                bars, close, close_tf,
                ("Without venue calendar and feed-health context, a timestamp gap cannot be attributed to a scheduled session close.",)
            )]
        return []

