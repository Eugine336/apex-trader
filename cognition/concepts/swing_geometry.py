"""Swing-anchored Fibonacci, OTE and regression-channel concept readings.

All anchors are confirmed from completed candles. These are geometric context
reads, not predictive levels, trade signals or evidence of institutional orders.
"""
from __future__ import annotations

import math

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


def _reading(concept, tf, bars, close, close_tf, observation, implications, facts, unknowns=()):
    last = bars[-1]
    epoch = last["timestamp_epoch"] if last["source_timestamp_verified"] else None
    return ConceptReading(
        concept=concept, timeframe=tf, domain=EvidenceDomain.STRUCTURE,
        applicable=True, applicability_reason="Confirmed completed-bar anchors are present",
        observation=observation, implications=tuple(implications), facts=facts,source_bar_epoch=epoch,

        source_timestamp_verified=epoch is not None, reference_close=close, reference_timeframe=close_tf,
        unknowns=tuple(unknowns),
    )


def _imp(direction, statement, active, invalidation, *, stop=None, target=None, requires=(), conflicts=()):
    return Implication(
        direction=direction, statement=statement, strength=None, activated=bool(active),
        activation_reason=statement if active else "The defined geometric condition is not currently satisfied",
        invalidation_price=stop, invalidation_reason=invalidation, objective_price=target,
        requires=tuple(requires), conflicts_with=tuple(conflicts),
        management={"reassess_on": "new confirmed swing or completed close through anchor invalidation"},
    )


class SwingGeometryConcepts:
    key = "swing_geometry"
    family = "structure_geometry"
    covers_methods = ("fibonacci", "ote", "trend_channels")
    covers_lenses = ()

    def read(self, symbol: str, bars_by_tf: dict[str, list[dict]]) -> list[ConceptReading]:
        close, close_tf = reference_price(bars_by_tf)
        out = []
        for tf, raw in bars_by_tf.items():
            if tf not in TIMEFRAME_SECONDS or TIMEFRAME_SECONDS[tf] < 300:
                continue
            bars = _clean(raw)
            if len(bars) < 25:
                continue
            out.extend(self._fibonacci_ote(tf, bars, close, close_tf))
            out.extend(self._channel(tf, bars, close, close_tf))
        return out

    @staticmethod
    def _pivots(bars):
        pivots = []
        for i in range(1, len(bars) - 1):
            if bars[i]["high"] > bars[i-1]["high"] and bars[i]["high"] >= bars[i+1]["high"]:
                pivots.append(("high", i, bars[i]["high"]))
            if bars[i]["low"] < bars[i-1]["low"] and bars[i]["low"] <= bars[i+1]["low"]:
                pivots.append(("low", i, bars[i]["low"]))
        ordered = sorted(pivots, key=lambda x: x[1])
        # Collapse consecutive same-side extrema to the most extreme confirmed
        # pivot. Otherwise a weaker intervening high can incorrectly become the
        # impulse endpoint while a later, stronger high is ignored.
        alternating = []
        for pivot in ordered:
            if alternating and alternating[-1][0] == pivot[0]:
                previous = alternating[-1]
                more_extreme = (
                    pivot[2] >= previous[2] if pivot[0] == "high"
                    else pivot[2] <= previous[2]
                )
                if more_extreme:
                    alternating[-1] = pivot
                continue
            alternating.append(pivot)
        return alternating

    def _fibonacci_ote(self, tf, bars, close, close_tf):
        pivots = self._pivots(bars)
        pair = None
        for left, right in zip(reversed(pivots[:-1]), reversed(pivots[1:])):
            if left[0] != right[0]:
                pair = (left, right)
                break
        if pair is None:
            return []
        left, right = pair
        if left[0] == "low":
            start, end, side = left[2], right[2], LONG
        else:
            start, end, side = left[2], right[2], SHORT
        distance = abs(end - start)
        if distance <= 0:
            return []
        # Retracement measured from the impulse extreme back toward its origin.
        if side == LONG:
            fib = {level: end - distance * level for level in (0.5, 0.618, 0.786)}
            extension = end + distance * 0.272
            ote_low, ote_high = sorted((fib[0.618], fib[0.786]))
            invalid = bars[-1]["close"] <= start
        else:
            fib = {level: end + distance * level for level in (0.5, 0.618, 0.786)}
            extension = end - distance * 0.272
            ote_low, ote_high = sorted((fib[0.618], fib[0.786]))
            invalid = bars[-1]["close"] >= start
        current = bars[-1]["close"]
        in_ote = ote_low <= current <= ote_high
        active = in_ote and not invalid
        return [_reading(
            "fibonacci", tf, bars, close, close_tf,
            f"Latest confirmed {side.lower()} impulse {start:.10g} to {end:.10g}; "
            f"50/61.8/78.6% retracements={fib}; current close={current:.10g}; "
            f"OTE interval={ote_low:.10g}–{ote_high:.10g}; inside_ote={in_ote}.",
            [_imp(side, f"{side} swing retracement is inside the declared 61.8–78.6% OTE zone",
                  active, "A completed close through the impulse origin invalidates the anchored impulse",
                  stop=start, target=extension,
                  requires=("reaction and renewed directional structure", "cost-aware opportunity geometry"),
                  conflicts=("price has not reached the OTE interval",) if not in_ote else ()),
             _imp(FLAT, "Fib geometry alone is not directional confirmation", True, "Not applicable",
                  requires=("independent structure and participation evidence",))],
            {"impulse_start": start, "impulse_end": end, "impulse_direction": side,
             "start_pivot_index": left[1], "end_pivot_index": right[1],
             "retracements": {str(k): v for k, v in fib.items()},
             "ote_low": ote_low, "ote_high": ote_high, "inside_ote": in_ote,
             "impulse_invalidated": invalid, "extension_1_272": extension,
             "definition": "latest_confirmed_alternating_three_bar_pivots_ote_61_8_78_6_v1"},
            ("Swing anchors are confirmed fractal pivots and may lag; fib ratios are geometric conventions, not empirically privileged probabilities.",)
        )]

    def _channel(self, tf, bars, close, close_tf):
        window = bars[-30:]
        values = [row["close"] for row in window]
        n = len(values)
        x_mean = (n - 1) / 2.0
        y_mean = sum(values) / n
        denom = sum((i - x_mean) ** 2 for i in range(n))
        if denom <= 0:
            return []
        slope = sum((i - x_mean) * (y - y_mean) for i, y in enumerate(values)) / denom
        intercept = y_mean - slope * x_mean
        residuals = [y - (intercept + slope * i) for i, y in enumerate(values)]
        variance = sum(r * r for r in residuals) / max(1, n - 2)
        width = math.sqrt(max(0.0, variance))
        centre = intercept + slope * (n - 1)
        upper, lower = centre + 2.0 * width, centre - 2.0 * width
        current = values[-1]
        position = "above" if current > upper else "below" if current < lower else "inside"
        side = LONG if slope > 0 else SHORT if slope < 0 else FLAT
        broken = (side == LONG and current < lower) or (side == SHORT and current > upper)
        return [_reading(
            "trend_channels", tf, bars, close, close_tf,
            f"30-bar least-squares close channel slope={slope:.10g}; residual sigma={width:.10g}; "
            f"centre={centre:.10g}; envelope={lower:.10g}–{upper:.10g}; "
            f"latest close={current:.10g} ({position}); channel_break={broken}.",
            [_imp(side, f"{side} regression-channel context; price is {position} the two-sigma envelope",
                  side != FLAT and not broken,
                  "A confirmed close beyond the opposite channel boundary invalidates the current channel read",
                  stop=lower if side == LONG else upper if side == SHORT else None,
                  requires=("confirm with swing structure and volatility regime",),
                  conflicts=("price has broken the regression envelope",) if broken else ()),
             _imp(FLAT, "Regression channel is a statistical fit, not a causal price boundary", True,
                  "Not applicable", requires=("test stability across rolling windows",))],
            {"lookback_bars": n, "slope_per_bar": slope, "residual_sigma": width,
             "centre": centre, "upper_2sigma": upper, "lower_2sigma": lower,
             "price_position": position, "channel_break": broken,
             "fit_r_squared": (1.0 - sum(r*r for r in residuals) /
                               max(sum((y-y_mean)**2 for y in values), 1e-18)),
             "definition": "rolling_30_close_ols_two_residual_sigma_v1"},
            ("OLS channel width assumes a stable residual distribution; the two-sigma envelope is descriptive, not a calibrated confidence interval.",)
        )]
