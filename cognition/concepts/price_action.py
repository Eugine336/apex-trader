"""Contextual candlestick, swing-level and failed-auction concept readings.

All observations use completed OHLC bars. A candle pattern is never promoted to
an entry trigger; price levels are structural references with finite provenance.
"""
from __future__ import annotations

from cognition.contracts import EvidenceDomain
from cognition.concepts.base import (
    FLAT, LONG, SHORT, TIMEFRAME_SECONDS, ConceptReading, Implication,
    finite, reference_price,
)


def _bars(rows):
    out = []
    for row in rows or []:
        o, h, l, c = (finite(row.get(k)) for k in ("open", "high", "low", "close"))
        if None in (o, h, l, c) or h < l or h < max(o, c) or l > min(o, c):
            continue
        out.append({**row, "open": o, "high": h, "low": l, "close": c})
    return out


def _read(concept, tf, domain, bars, ref, ref_tf, observation, implications, facts, unknowns=()):
    latest = bars[-1]
    epoch = finite(latest.get("timestamp_epoch"))
    if latest.get("source_timestamp_verified") is not True:
        epoch = None
    return ConceptReading(
        concept=concept, timeframe=tf, domain=domain, applicable=True,
        applicability_reason="The declared completed-bar structure is present",
        observation=observation, implications=tuple(implications), facts=facts,source_bar_epoch=epoch,

        source_timestamp_verified=epoch is not None, reference_close=ref, reference_timeframe=ref_tf,
        unknowns=tuple(unknowns),
    )


def _imp(direction, statement, active, invalidation, *, stop=None, requires=(), strength=None):
    return Implication(
        direction=direction, statement=statement, strength=strength,
        activated=bool(active), activation_reason=statement if active else "Defining condition is absent",
        invalidation_price=stop, invalidation_reason=invalidation,
        requires=tuple(requires), management={"reassess_on": "next completed candle"},
    )


class PriceActionConcepts:
    key = "price_action"
    family = "price_action"
    covers_methods = ("candlestick_patterns", "candle_microstructure", "support_resistance", "failed_auction")
    covers_lenses = ()

    def read(self, symbol: str, bars_by_tf: dict[str, list[dict]]) -> list[ConceptReading]:
        ref, ref_tf = reference_price(bars_by_tf)
        out = []
        for tf, raw in bars_by_tf.items():
            if tf not in TIMEFRAME_SECONDS or TIMEFRAME_SECONDS[tf] < 300:
                continue
            bars = _bars(raw)
            if len(bars) < 12:
                continue
            out.extend(self._microstructure(tf, bars, ref, ref_tf))
            out.extend(self._candles(tf, bars, ref, ref_tf))
            out.extend(self._levels(tf, bars, ref, ref_tf))
            out.extend(self._failed_auction(tf, bars, ref, ref_tf))
        return out


    def _microstructure(self, tf, bars, ref, ref_tf):
        current = bars[-1]
        span = current["high"] - current["low"]
        if span <= 0:
            return []
        body = abs(current["close"] - current["open"])
        upper = current["high"] - max(current["open"], current["close"])
        lower = min(current["open"], current["close"]) - current["low"]
        close_location = (current["close"] - current["low"]) / span
        history_ranges = [x["high"]-x["low"] for x in bars[-21:-1] if x["high"] > x["low"]]
        median_range = sorted(history_ranges)[len(history_ranges)//2] if history_ranges else None
        prior_closes = [x["close"] for x in bars[-6:-1]]
        up = sum(1 for a,b in zip(prior_closes, prior_closes[1:]) if b>a)
        down = sum(1 for a,b in zip(prior_closes, prior_closes[1:]) if b<a)
        facts = {
            "body_ratio": body/span, "upper_wick_ratio": upper/span,
            "lower_wick_ratio": lower/span, "close_location": close_location,
            "range_price": span, "median_prior_range": median_range,
            "range_vs_prior_median": span/median_range if median_range else None,
            "prior_five_close_up_steps": up, "prior_five_close_down_steps": down,
            "definition": "completed_candle_anatomy_and_five_close_sequence_v1",
        }
        return [_read(
            "candle_microstructure", tf, EvidenceDomain.STRUCTURE, bars, ref, ref_tf,
            f"Completed candle body/range={body/span:.3f}; upper wick/range={upper/span:.3f}; "
            f"lower wick/range={lower/span:.3f}; close-location={close_location:.3f}; "
            f"range/prior-median={facts['range_vs_prior_median']}; "
            f"prior close sequence up-steps={up}, down-steps={down}.",
            [_imp(FLAT, "Candle anatomy and short close sequence describe auction behaviour but do not determine direction",
                  True, "A new completed candle replaces the current anatomy",
                  requires=("compare with confirmed structure, volatility and participation",))],
            facts, ("Candle geometry is not bid/ask delta, executed volume-at-price or direct order flow.",)
        )]

    def _candles(self, tf, bars, ref, ref_tf):
        current, previous = bars[-1], bars[-2]
        span = current["high"] - current["low"]
        if span <= 0:
            return []
        body = abs(current["close"] - current["open"])
        upper = current["high"] - max(current["open"], current["close"])
        lower = min(current["open"], current["close"]) - current["low"]
        close_location = (current["close"] - current["low"]) / span
        inside = current["high"] <= previous["high"] and current["low"] >= previous["low"]
        bull_engulf = (current["close"] > current["open"] and previous["close"] < previous["open"]
                       and current["open"] <= previous["close"] and current["close"] >= previous["open"])
        bear_engulf = (current["close"] < current["open"] and previous["close"] > previous["open"]
                       and current["open"] >= previous["close"] and current["close"] <= previous["open"])
        doji = body / span <= 0.10
        hammer = lower >= 2.0 * max(body, 1e-12) and upper <= 0.35 * span and close_location >= 0.60
        shooting = upper >= 2.0 * max(body, 1e-12) and lower <= 0.35 * span and close_location <= 0.40
        tags = []
        if inside: tags.append("inside_bar")
        if bull_engulf: tags.append("bullish_engulfing")
        if bear_engulf: tags.append("bearish_engulfing")
        if doji: tags.append("doji")
        if hammer: tags.append("hammer_or_bullish_pin")
        if shooting: tags.append("shooting_star_or_bearish_pin")
        if not tags:
            return []
        direction = LONG if bull_engulf or hammer else SHORT if bear_engulf or shooting else FLAT
        implications = [
            _imp(direction, f"Completed candle anatomy: {', '.join(tags)}", True,
                 "A subsequent completed candle negates the observed pattern context",
                 stop=current["low"] if direction == LONG else current["high"] if direction == SHORT else None,
                 requires=("location at a meaningful structural level", "subsequent follow-through")),
            _imp(FLAT, "Candle pattern is descriptive; direction is not established by shape alone",
                 True, "Not applicable", requires=("structural and participation context",)),
        ]
        return [_read(
            "candlestick_patterns", tf, EvidenceDomain.STRUCTURE, bars, ref, ref_tf,
            f"Latest completed candle tags={tags}; body/range={body/span:.3f}; "
            f"upper-wick/range={upper/span:.3f}; lower-wick/range={lower/span:.3f}; "
            f"close-location={close_location:.3f}.",
            implications,
            {"tags": tags, "body_ratio": body/span, "upper_wick_ratio": upper/span,
             "lower_wick_ratio": lower/span, "close_location": close_location,
             "inside_bar": inside, "bullish_engulfing": bull_engulf,
             "bearish_engulfing": bear_engulf, "doji": doji, "hammer": hammer,
             "shooting_star": shooting, "definition": "completed_candle_geometry_v1"},
            ("Candle geometry alone does not establish trend, liquidity or order-flow confirmation.",)
        )]

    def _levels(self, tf, bars, ref, ref_tf):
        pivots = []
        for i in range(1, len(bars) - 1):
            if bars[i]["high"] > bars[i-1]["high"] and bars[i]["high"] >= bars[i+1]["high"]:
                pivots.append(("resistance", i, bars[i]["high"]))
            if bars[i]["low"] < bars[i-1]["low"] and bars[i]["low"] <= bars[i+1]["low"]:
                pivots.append(("support", i, bars[i]["low"]))
        if not pivots:
            return []
        supports = [p for p in pivots if p[0] == "support"]
        resistances = [p for p in pivots if p[0] == "resistance"]
        last = bars[-1]["close"]
        nearest_support = max((p for p in supports if p[2] <= last), key=lambda p: p[2], default=None)
        nearest_resistance = min((p for p in resistances if p[2] >= last), key=lambda p: p[2], default=None)
        if nearest_support is None and nearest_resistance is None:
            return []
        facts = {
            "nearest_support": nearest_support[2] if nearest_support else None,
            "nearest_resistance": nearest_resistance[2] if nearest_resistance else None,
            "support_pivot_index": nearest_support[1] if nearest_support else None,
            "resistance_pivot_index": nearest_resistance[1] if nearest_resistance else None,
            "confirmed_support_count": len(supports), "confirmed_resistance_count": len(resistances),
            "definition": "confirmed_three_bar_fractal_levels_v1",
        }
        return [_read(
            "support_resistance", tf, EvidenceDomain.STRUCTURE, bars, ref, ref_tf,
            f"Nearest confirmed fractal support={facts['nearest_support']}; "
            f"nearest confirmed fractal resistance={facts['nearest_resistance']}; "
            f"these are reference levels, not guaranteed barriers.",
            [_imp(FLAT, "Confirmed swing levels frame location and invalidation; no directional reaction is assumed",
                  True, "A completed close through a level changes its structural status",
                  requires=("observe retest, acceptance or rejection at the level",))],
            facts, ("A three-bar pivot is confirmed only after the following bar closes; clustered zones require tick-size-aware tolerance.",)
        )]

    def _failed_auction(self, tf, bars, ref, ref_tf):
        # Sweep a prior 10-bar extreme and close back inside that same range.
        prior = bars[-11:-1]
        high, low = max(x["high"] for x in prior), min(x["low"] for x in prior)
        last = bars[-1]
        bull = last["low"] < low and last["close"] > low
        bear = last["high"] > high and last["close"] < high
        if not (bull or bear):
            return []
        side = LONG if bull else SHORT
        swept = low if bull else high
        return [_read(
            "failed_auction", tf, EvidenceDomain.LIQUIDITY, bars, ref, ref_tf,
            f"{side} failed auction proxy: price breached prior 10-bar {'low' if bull else 'high'} "
            f"{swept:.10g} intrabar and completed back inside the reference range.",
            [_imp(side, f"{side} failed-auction/reclaim hypothesis", True,
                  "A subsequent completed close beyond the swept extreme invalidates the reclaim",
                  stop=last["low"] if bull else last["high"],
                  requires=("follow-through away from the swept boundary", "check independent order-flow evidence"))],
            {"direction": side, "swept_level": swept, "range_high": high, "range_low": low,
             "reclaimed": True, "lookback_bars": 10, "definition": "wick_breach_close_reclaim_v1"},
            ("A failed auction inferred from OHLC is not direct evidence of trapped participants or absorption.",)
        )]
