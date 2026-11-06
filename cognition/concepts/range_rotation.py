"""Balance, edge rejection and range rotation from confirmed structural swings."""
from __future__ import annotations

from cognition.concepts.auction_utils import (
    clean_bars, last_source_epoch, percentile_strength, profile_from_bars, swing_points,
)
from cognition.concepts.base import LONG, SHORT, ConceptReading, Implication, reference_price
from cognition.contracts import EvidenceDomain


class RangeRotation:
    key = "range_rotation"
    family = "strategy"
    covers_methods = ("range_rotation",)
    covers_lenses = ()

    def read(self, symbol: str, bars_by_tf: dict[str, list[dict]]) -> list[ConceptReading]:
        reference, reference_tf = reference_price(bars_by_tf)
        readings = []
        for timeframe, raw in bars_by_tf.items():
            bars = clean_bars(raw)
            if len(bars) < 3:
                continue
            current = bars[-1]
            epoch = last_source_epoch(bars)
            points = swing_points(bars)
            highs = [point for point in points if point["kind"] == "high" and point["index"] < len(bars) - 1]
            lows = [point for point in points if point["kind"] == "low" and point["index"] < len(bars) - 1]
            pairs = [
                (high, low) for high in highs for low in lows
                if high["price"] > low["price"] and low["price"] < current["close"] < high["price"]
            ]
            unknowns = []
            if not pairs:
                unknowns.append("a confirmed swing-high and swing-low pair bracketing the latest completed close")
                readings.append(ConceptReading(
                    concept=self.key, timeframe=timeframe, domain=EvidenceDomain.STRUCTURE,
                    applicable=False,
                    applicability_reason="No confirmed structural balance range currently brackets price.",
                    observation=f"{timeframe} range rotation is not applicable because no confirmed swing range brackets the latest close.",
                    implications=(
                        Implication(LONG, "No confirmed balance range supports a long rotation.", None, False,
                                    "No active range edge is confirmed.", requires=tuple(unknowns)),
                        Implication(SHORT, "No confirmed balance range supports a short rotation.", None, False,
                                    "No active range edge is confirmed.", requires=tuple(unknowns)),
                    ),
                    facts={"status": "no_confirmed_range", "confirmed_swing_count": len(points)},source_bar_epoch=epoch,

                    source_timestamp_verified=epoch is not None, reference_close=reference, reference_timeframe=reference_tf,
                    unknowns=tuple(unknowns),
                ))
                continue

            upper_point, lower_point = max(
                pairs,
                key=lambda pair: (
                    max(pair[0]["index"], pair[1]["index"]),
                    min(pair[0]["index"], pair[1]["index"]),
                ),
            )
            upper, lower = upper_point["price"], lower_point["price"]
            midpoint = (upper + lower) / 2.0
            anchor_index = min(upper_point["index"], lower_point["index"])
            subsequent = bars[anchor_index + 1:]
            high_rejections = [bar for bar in subsequent if bar["high"] >= upper and bar["close"] < upper]
            low_rejections = [bar for bar in subsequent if bar["low"] <= lower and bar["close"] > lower]
            failed_high = current["high"] > upper and current["close"] < upper
            failed_low = current["low"] < lower and current["close"] > lower
            accepted_above, accepted_below = current["close"] > upper, current["close"] < lower
            balance = bool(high_rejections and low_rejections and lower < current["close"] < upper)
            state = (
                "accepted_above_range" if accepted_above else
                "accepted_below_range" if accepted_below else
                "failed_high_auction" if failed_high else
                "failed_low_auction" if failed_low else
                "balance_rotation" if balance else "inside_range_unconfirmed"
            )
            rotation_count = len(high_rejections) + len(low_rejections)
            historical_sizes = [
                high["price"] - low["price"]
                for high in highs for low in lows
                if high["price"] > low["price"]
                and max(high["index"], low["index"]) < max(upper_point["index"], lower_point["index"])
            ]
            strength = percentile_strength(upper - lower, historical_sizes)
            if strength is None:
                unknowns.append("prior confirmed range sizes for instrument-relative range-strength percentile")
            in_range_bars = [bar for bar in bars if bar["high"] >= lower and bar["low"] <= upper]
            profile = profile_from_bars(in_range_bars)
            if profile is None:
                unknowns.append("positive broker volume or explicitly labeled tick-volume proxy for value-area and POC context")
            else:
                unknowns.append("true volume-at-price is unavailable; POC/value area use the disclosed OHLCV price-assignment proxy")
            poc = profile["poc"] if profile else None
            long_active, short_active = failed_low, failed_high
            long_target = poc if poc is not None and current["close"] < poc < upper else upper
            short_target = poc if poc is not None and lower < poc < current["close"] else lower
            long_milestones = (midpoint,) if long_active and current["close"] < midpoint < long_target else ()
            short_milestones = (midpoint,) if short_active and short_target < midpoint < current["close"] else ()
            implications = (
                Implication(
                    direction=LONG,
                    statement=f"Range state is {state}; the long-side rotation is {'active' if long_active else 'not triggered'}.",
                    strength=strength, activated=long_active,
                    activation_reason="The latest completed bar swept below the confirmed range low and closed back above it." if long_active else "No latest-bar lower-edge rejection has activated the long rotation.",
                    invalidation_price=lower if long_active else None,
                    invalidation_reason="A completed close below the confirmed lower range edge invalidates the long rotation.",
                    objective_price=long_target if long_active else None, milestones=long_milestones,
                    requires=tuple(unknowns),
                    conflicts_with=("A completed close accepting below the lower edge.", "The upper boundary ceases to hold as a range edge."),
                    management={"meaning": "Manage toward the opposite range edge or the real profile POC; a close beyond the lower edge invalidates the rotation."},
                ),
                Implication(
                    direction=SHORT,
                    statement=f"Range state is {state}; the short-side rotation is {'active' if short_active else 'not triggered'}.",
                    strength=strength, activated=short_active,
                    activation_reason="The latest completed bar swept above the confirmed range high and closed back below it." if short_active else "No latest-bar upper-edge rejection has activated the short rotation.",
                    invalidation_price=upper if short_active else None,
                    invalidation_reason="A completed close above the confirmed upper range edge invalidates the short rotation.",
                    objective_price=short_target if short_active else None, milestones=short_milestones,
                    requires=tuple(unknowns),
                    conflicts_with=("A completed close accepting above the upper edge.", "The lower boundary ceases to hold as a range edge."),
                    management={"meaning": "Manage toward the opposite range edge or the real profile POC; a close beyond the upper edge invalidates the rotation."},
                ),
            )
            readings.append(ConceptReading(
                concept=self.key, timeframe=timeframe, domain=EvidenceDomain.STRUCTURE,
                applicable=True,
                applicability_reason="Confirmed swing boundaries bracket price and define an active auction range.",
                observation=f"{timeframe} range {lower:g}–{upper:g}; state={state}; latest edge rejection is read independently of prior rotations.",
                implications=implications,
                facts={
                    "status": state, "range_high": upper, "range_low": lower,
                    "range_midpoint": midpoint, "range_size": upper-lower,
                    "upper_anchor_epoch": upper_point["epoch"], "lower_anchor_epoch": lower_point["epoch"],
                    "upper_rejections": len(high_rejections), "lower_rejections": len(low_rejections),
                    "rotation_edge_events": rotation_count, "failed_high_auction": failed_high,
                    "failed_low_auction": failed_low, "profile": profile, "poc": poc,
                    "historical_range_samples": len(historical_sizes),
                    "range_size_percentile": strength,
                },source_bar_epoch=epoch,

                source_timestamp_verified=epoch is not None, reference_close=reference, reference_timeframe=reference_tf,
                unknowns=tuple(unknowns),
            ))
        return readings
