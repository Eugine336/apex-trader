"""Confirmed-pivot chart patterns and OHLC-derived supply/demand zone readings.

Patterns are descriptive structural hypotheses. Zones are price-action proxies;
they never claim hidden institutional orders or independently authorize trades.
"""
from __future__ import annotations

from cognition.concepts.base import (
    FLAT, LONG, SHORT, TIMEFRAME_SECONDS, ConceptReading, Implication,
    finite, reference_price,
)
from cognition.contracts import EvidenceDomain


def _clean(rows):
    out = []
    for row in rows or []:
        o, h, l, c = (finite(row.get(k)) for k in ("open", "high", "low", "close"))
        if None in (o, h, l, c) or h < l or h < max(o, c) or l > min(o, c):
            continue
        out.append({**row, "open": o, "high": h, "low": l, "close": c,
                    "timestamp_epoch": finite(row.get("timestamp_epoch")),
                    "source_timestamp_verified": row.get("source_timestamp_verified") is True})
    return out


def _pivots(bars):
    result = []
    for i in range(1, len(bars) - 1):
        if bars[i]["high"] > bars[i-1]["high"] and bars[i]["high"] >= bars[i+1]["high"]:
            result.append(("H", i, bars[i]["high"]))
        if bars[i]["low"] < bars[i-1]["low"] and bars[i]["low"] <= bars[i+1]["low"]:
            result.append(("L", i, bars[i]["low"]))
    return sorted(result, key=lambda p: p[1])


def _imp(direction, statement, active, invalidation, *, stop=None, target=None, requires=(), conflicts=()):
    return Implication(
        direction=direction, statement=statement, strength=None, activated=bool(active),
        activation_reason=statement if active else "The declared structural trigger is not satisfied",
        invalidation_price=stop, invalidation_reason=invalidation, objective_price=target,
        requires=tuple(requires), conflicts_with=tuple(conflicts),
        management={"reassess_on": "new confirmed pivot, retest or completed close through zone invalidation"},
    )


def _reading(concept, tf, bars, ref, ref_tf, observation, implications, facts, unknowns):
    last = bars[-1]
    epoch = last["timestamp_epoch"] if last["source_timestamp_verified"] else None
    return ConceptReading(
        concept=concept, timeframe=tf, domain=EvidenceDomain.STRUCTURE,
        applicable=True, applicability_reason="The declared completed-bar structure is present",
        observation=observation, implications=tuple(implications), facts=facts,source_bar_epoch=epoch,

        source_timestamp_verified=epoch is not None, reference_close=ref, reference_timeframe=ref_tf,
        unknowns=tuple(unknowns),
    )


class StructurePatternConcepts:
    key = "structure_patterns"
    family = "classical_structure"
    covers_methods = ("chart_patterns", "supply_demand")
    covers_lenses = ()

    def read(self, symbol: str, bars_by_tf: dict[str, list[dict]]) -> list[ConceptReading]:
        ref, ref_tf = reference_price(bars_by_tf)
        out = []
        for tf, raw in bars_by_tf.items():
            if tf not in TIMEFRAME_SECONDS or TIMEFRAME_SECONDS[tf] < 300:
                continue
            bars = _clean(raw)
            if len(bars) < 25:
                continue
            out.extend(self._patterns(tf, bars, ref, ref_tf))
            out.extend(self._zones(tf, bars, ref, ref_tf))
        return out

    def _patterns(self, tf, bars, ref, ref_tf):
        pivots = _pivots(bars)
        if len(pivots) < 3:
            return []
        ranges = [x["high"] - x["low"] for x in bars[-20:] if x["high"] > x["low"]]
        tolerance = (sum(ranges) / len(ranges)) * 0.35 if ranges else 0.0
        selected = pivots[-5:]
        tags, direction, invalid, neckline = [], FLAT, None, None
        # Double top / bottom: select the latest valid alternating triple.
        # Require at least two bars between each pivot; adjacent one-bar noise
        # must not manufacture a second equal-high/equal-low formation.
        for start in range(len(selected) - 3, -1, -1):
            a, b, c = selected[start:start + 3]
            if b[1] - a[1] < 2 or c[1] - b[1] < 2:
                continue
            if a[0] == c[0] == "H" and b[0] == "L" and abs(a[2]-c[2]) <= tolerance:
                tags.append("double_top")
                direction, invalid, neckline = SHORT, max(a[2], c[2]), b[2]
                break
            if a[0] == c[0] == "L" and b[0] == "H" and abs(a[2]-c[2]) <= tolerance:
                tags.append("double_bottom")
                direction, invalid, neckline = LONG, min(a[2], c[2]), b[2]
                break
        # Head-and-shoulders / inverse: H-L-H-L-H or its mirror.
        if len(selected) == 5:
            a, b, c, d, e = selected
            if [p[0] for p in selected] == ["H", "L", "H", "L", "H"]:
                shoulders = abs(a[2]-e[2]) <= tolerance
                head = c[2] > max(a[2], e[2]) + tolerance
                if shoulders and head:
                    tags.append("head_and_shoulders")
                    direction, invalid, neckline = SHORT, c[2], (b[2]+d[2])/2
            elif [p[0] for p in selected] == ["L", "H", "L", "H", "L"]:
                shoulders = abs(a[2]-e[2]) <= tolerance
                head = c[2] < min(a[2], e[2]) - tolerance
                if shoulders and head:
                    tags.append("inverse_head_and_shoulders")
                    direction, invalid, neckline = LONG, c[2], (b[2]+d[2])/2
        # Triangles: the last four pivots alternate and their high/low envelopes converge.
        if len(selected) >= 4 and not tags:
            tail = selected[-4:]
            highs = [p[2] for p in tail if p[0] == "H"]
            lows = [p[2] for p in tail if p[0] == "L"]
            if len(highs) == 2 and len(lows) == 2:
                high_slope = highs[-1] - highs[0]
                low_slope = lows[-1] - lows[0]
                if high_slope < -tolerance and low_slope > tolerance:
                    tags.append("symmetrical_triangle")
                elif abs(high_slope) <= tolerance and low_slope > tolerance:
                    tags.append("ascending_triangle")
                elif abs(low_slope) <= tolerance and high_slope < -tolerance:
                    tags.append("descending_triangle")
                if tags and tags[-1].endswith("triangle"):
                    direction, invalid, neckline = FLAT, None, None
        if not tags:
            return []
        confirmed = False
        if neckline is not None:
            confirmed = (bars[-1]["close"] < neckline if direction == SHORT
                         else bars[-1]["close"] > neckline if direction == LONG else False)
        active = direction in (LONG, SHORT) and confirmed
        return [_reading(
            "chart_patterns", tf, bars, ref, ref_tf,
            f"Confirmed pivot geometry tags={tags}; neckline/reference={neckline}; "
            f"latest close={bars[-1]['close']:.10g}; breakout_confirmed={confirmed}.",
            [_imp(direction, f"{direction} structural pattern hypothesis: {', '.join(tags)}",
                  active, "Pattern invalidates through the opposing structural extreme",
                  stop=invalid, target=None,
                  requires=("completed neckline break", "follow-through and retest") if not confirmed else
                           ("independent participation and cost-aware geometry",)),
             _imp(FLAT, "Chart-pattern geometry can fail and must be compared with the competing auction explanation",
                  True, "Not applicable", requires=("market-regime and volatility context",))],
            {"patterns": tags, "pivots": [{"kind": p[0], "index": p[1], "price": p[2]} for p in selected],
             "tolerance_price": tolerance, "neckline": neckline, "breakout_confirmed": confirmed,
             "definition": "confirmed_fractal_double_top_bottom_hs_triangle_v1"},
            ("Classical pattern taxonomy is ambiguous; fixed geometric tolerances are declared definitions, not fitted predictive thresholds.",)
        )]

    def _zones(self, tf, bars, ref, ref_tf):
        for impulse_i in range(len(bars)-1, max(4, len(bars)-12), -1):
            prior_ranges = [
                x["high"] - x["low"]
                for x in bars[max(0, impulse_i-20):impulse_i]
                if x["high"] > x["low"]
            ]
            if len(prior_ranges) < 10:
                continue
            baseline = sorted(prior_ranges)[len(prior_ranges)//2]
            if baseline <= 0:
                continue
            impulse = bars[impulse_i]
            span = impulse["high"] - impulse["low"]
            if span < 1.5 * baseline:
                continue
            body_ratio = abs(impulse["close"]-impulse["open"]) / max(span, 1e-12)
            if body_ratio < 0.65:
                continue
            bull = impulse["close"] > impulse["open"]
            for width in range(2, 6):
                base = bars[impulse_i-width:impulse_i]
                if len(base) != width:
                    continue
                base_high = max(x["high"] for x in base)
                base_low = min(x["low"] for x in base)
                if base_high-base_low > baseline * width * 0.9:
                    continue
                later = bars[impulse_i+1:]
                mitigated = any(x["low"] <= base_high and x["high"] >= base_low for x in later)
                invalid = any(x["close"] < base_low for x in later) if bull else any(x["close"] > base_high for x in later)
                side = LONG if bull else SHORT
                return [_reading(
                    "supply_demand", tf, bars, ref, ref_tf,
                    f"Candidate {'demand' if bull else 'supply'} base {base_low:.10g}–{base_high:.10g} "
                    f"preceded a relative impulse (range multiple={span/baseline:.2f}, body ratio={body_ratio:.2f}); "
                    f"mitigated={mitigated}; invalidated={invalid}.",
                    [_imp(side, f"{side} base-origin zone proxy", not invalid and not mitigated,
                          "Completed close through distal base boundary invalidates the zone",
                          stop=base_low if bull else base_high,
                          requires=("retest and reaction evidence",) if not mitigated else (),
                          conflicts=("base zone has already been revisited",) if mitigated else ()),
                     _imp(FLAT, "Base-and-impulse geometry does not prove resting institutional orders",
                          True, "Not applicable", requires=("independent volume or order-flow evidence",))],
                    {"zone_low": base_low, "zone_high": base_high, "direction": side,
                     "base_start_index": impulse_i-width, "base_end_index": impulse_i-1,
                     "impulse_index": impulse_i, "impulse_range_multiple": span/baseline,
                     "impulse_body_ratio": body_ratio, "mitigated": mitigated,
                     "invalidated": invalid, "definition": "two_to_five_bar_base_before_relative_impulse_v1"},
                    ("Supply/demand zone is a price-action proxy; it does not prove hidden limit orders or institutional ownership.",)
                )]
        return []
