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
        source_bar_epoch=_epoch(bars[-1]), reference_close=ref_close,
        reference_timeframe=ref_tf, unknowns=tuple(unknowns),
    )


class PriceDeliveryConcepts:
    """Read fair-value gaps, order blocks, balanced ranges and dealing-range location."""
    key = "price_delivery"
    family = "institutional_price_action"
    covers_methods = ("fair_value_gap", "order_blocks", "balanced_price_range", "dealing_range")
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
