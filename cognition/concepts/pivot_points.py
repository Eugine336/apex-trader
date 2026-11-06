"""Classic floor pivot-point reading from a verified completed broker D1 bar."""
from __future__ import annotations

from cognition.concepts.base import FLAT, TIMEFRAME_SECONDS, ConceptReading, Implication, finite, reference_price
from cognition.contracts import EvidenceDomain


class PivotPointConcepts:
    key = "pivot_points"
    family = "price_location"
    covers_methods = ("pivot_points",)
    covers_lenses = ()

    def read(self, symbol: str, bars_by_tf: dict[str, list[dict]]) -> list[ConceptReading]:
        daily = bars_by_tf.get("D1") or []
        if not daily:
            return []
        bar = daily[-1]
        high, low, close = (finite(bar.get(k)) for k in ("high", "low", "close"))
        if None in (high, low, close) or high < low:
            return []
        pivot = (high + low + close) / 3.0
        r1, s1 = 2.0 * pivot - low, 2.0 * pivot - high
        r2, s2 = pivot + (high - low), pivot - (high - low)
        r3, s3 = high + 2.0 * (pivot - low), low - 2.0 * (high - pivot)
        current, current_tf = reference_price(bars_by_tf)
        levels = {"S3": s3, "S2": s2, "S1": s1, "P": pivot, "R1": r1, "R2": r2, "R3": r3}
        below = [(name, value) for name, value in levels.items() if current is not None and value <= current]
        above = [(name, value) for name, value in levels.items() if current is not None and value >= current]
        nearest_support = max(below, key=lambda x: x[1]) if below else None
        nearest_resistance = min(above, key=lambda x: x[1]) if above else None
        epoch = finite(bar.get("timestamp_epoch")) if bar.get("source_timestamp_verified") is True else None
        facts = {
            "source_timeframe": "D1", "source_bar_epoch": epoch,
            "previous_high": high, "previous_low": low, "previous_close": close,
            "pivot": pivot, "s1": s1, "s2": s2, "s3": s3,
            "r1": r1, "r2": r2, "r3": r3,
            "nearest_support": {"name": nearest_support[0], "price": nearest_support[1]} if nearest_support else None,
            "nearest_resistance": {"name": nearest_resistance[0], "price": nearest_resistance[1]} if nearest_resistance else None,
            "definition": "classic_floor_pivots_from_last_completed_broker_d1_bar_v1",
        }
        return [ConceptReading(
            concept="pivot_points", timeframe="D1", domain=EvidenceDomain.STRUCTURE,
            applicable=True,
            applicability_reason="A completed daily OHLC bar is available for classic floor pivot calculation",
            observation=(f"Classic floor pivots from previous completed D1 bar: P={pivot:.10g}; "
                         f"S1/S2/S3={s1:.10g}/{s2:.10g}/{s3:.10g}; "
                         f"R1/R2/R3={r1:.10g}/{r2:.10g}/{r3:.10g}; "
                         f"current reference timeframe={current_tf}."),
            implications=(Implication(
                direction=FLAT,
                statement="Daily floor pivots are reference levels, not directional forecasts",
                strength=None, activated=True,
                activation_reason="Pivot levels are calculated from the last completed daily candle",
                invalidation_reason="A new completed D1 bar replaces the reference levels",
                requires=("observe acceptance/rejection and compare with independent structure",
                          "confirm broker daily boundary and timezone"),
                conflicts_with=(),
                management={"recalculate_on":"new_completed_D1_bar"},
            ),),
            facts=facts,source_bar_epoch=epoch,
 source_timestamp_verified=epoch is not None, reference_close=current,
            reference_timeframe=current_tf,
            unknowns=("Floor pivots depend on broker daily candle boundaries and are not universal exchange levels.",),
        )]
