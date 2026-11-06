"""Power-of-Three daily phase reading with explicit UTC-day boundaries.

This is a measurable OHLC proxy, not a claim that UTC thirds equal canonical
ICT or venue sessions. It requires a complete, verified 24-bar H1 UTC day.
"""
from __future__ import annotations

from datetime import datetime, timezone

from cognition.concepts.base import FLAT, LONG, SHORT, TIMEFRAME_SECONDS, ConceptReading, Implication, finite, reference_price
from cognition.contracts import EvidenceDomain


class PowerOfThreeConcepts:
    key = "power_of_three"
    family = "institutional_price_action"
    covers_methods = ("power_of_three",)
    covers_lenses = ()

    def read(self, symbol: str, bars_by_tf: dict[str, list[dict]]) -> list[ConceptReading]:
        hourly = bars_by_tf.get("H1") or []
        close, close_tf = reference_price(bars_by_tf)
        verified = [
            bar for bar in hourly
            if bar.get("source_timestamp_verified") is True
            and finite(bar.get("timestamp_epoch")) is not None
        ]
        groups: dict[str, list[dict]] = {}
        for bar in verified:
            dt = datetime.fromtimestamp(float(bar["timestamp_epoch"]), tz=timezone.utc)
            groups.setdefault(dt.strftime("%Y-%m-%d"), []).append(bar)
        complete_days = []
        for day, bars in groups.items():
            bars = sorted(bars, key=lambda item: item["timestamp_epoch"])
            epochs = [int(float(item["timestamp_epoch"])) for item in bars]
            if len(epochs) == 24 and all(
                epochs[i] - epochs[i-1] == TIMEFRAME_SECONDS["H1"]
                for i in range(1, 24)
            ):
                first = datetime.fromtimestamp(epochs[0], tz=timezone.utc)
                if first.hour == 0:
                    complete_days.append((day, bars))
        if not complete_days:
            last = hourly[-1] if hourly else {}
            epoch = finite(last.get("timestamp_epoch")) if last.get("source_timestamp_verified") is True else None
            return [ConceptReading(
                concept="power_of_three", timeframe="H1", domain=EvidenceDomain.STRUCTURE,
                applicable=False,
                applicability_reason="No complete verified 24-bar UTC day is available",
                observation="Power-of-Three phase sequence is not assessable without a complete verified UTC day.",
                implications=(Implication(
                    direction=FLAT, statement="Power-of-Three state is unknown", strength=None,
                    activated=False, activation_reason="Required session history is incomplete",
                    invalidation_reason="Not applicable",
                    requires=("complete verified H1 UTC-day history",),
                    conflicts_with=(),
                ),),
                facts={"applicable": False, "required_bars": 24, "available_verified_bars": len(verified),
                       "definition": "utc_day_24_h1_accumulation_manipulation_distribution_proxy_v1"},source_bar_epoch=epoch,

                source_timestamp_verified=epoch is not None, reference_close=close, reference_timeframe=close_tf,
                unknowns=("UTC day thirds are not interchangeable with broker-session or venue-session definitions.",),
            )]
        day, bars = max(complete_days, key=lambda item: item[0])
        accumulation = bars[:8]
        manipulation = bars[8:16]
        distribution = bars[16:24]
        acc_high = max(x["high"] for x in accumulation)
        acc_low = min(x["low"] for x in accumulation)
        buy_sweeps = [x for x in manipulation if x["high"] > acc_high and x["close"] < acc_high]
        sell_sweeps = [x for x in manipulation if x["low"] < acc_low and x["close"] > acc_low]
        sweep_side = "conflicted" if buy_sweeps and sell_sweeps else (
            "buy_side" if buy_sweeps else "sell_side" if sell_sweeps else "none"
        )
        direction = SHORT if sweep_side == "buy_side" else LONG if sweep_side == "sell_side" else FLAT
        final_close = distribution[-1]["close"]
        distributed = (
            direction == LONG and final_close > acc_high
        ) or (
            direction == SHORT and final_close < acc_low
        )
        active = direction in (LONG, SHORT) and distributed and sweep_side != "conflicted"
        facts = {
            "utc_day": day, "accumulation_start_epoch": bars[0]["timestamp_epoch"],
            "accumulation_high": acc_high, "accumulation_low": acc_low,
            "manipulation_sweep_side": sweep_side,
            "buy_side_sweep_count": len(buy_sweeps),
            "sell_side_sweep_count": len(sell_sweeps),
            "distribution_close": final_close, "distribution_confirmed": distributed,
            "direction": direction, "confirmed": active,
            "phase_definition": {"accumulation_utc_hours": "00-07",
                                 "manipulation_utc_hours": "08-15",
                                 "distribution_utc_hours": "16-23"},
            "definition": "utc_day_24_h1_accumulation_manipulation_distribution_proxy_v1",
        }
        epoch = finite(bars[-1].get("timestamp_epoch"))
        return [ConceptReading(
            concept="power_of_three", timeframe="H1", domain=EvidenceDomain.STRUCTURE,
            applicable=True,
            applicability_reason="A complete verified UTC day is available for the declared three-phase reading",
            observation=(f"{day} UTC Power-of-Three proxy: accumulation {acc_low:.10g}–{acc_high:.10g}; "
                         f"manipulation={sweep_side}; distribution close={final_close:.10g}; "
                         f"confirmed={active}."),
            implications=(Implication(
                direction=direction,
                statement=f"{direction} daily three-phase hypothesis" if direction != FLAT else
                          "No directional three-phase sequence is established",
                strength=None, activated=active,
                activation_reason="A completed manipulation sweep was followed by distribution beyond the accumulation range"
                                if active else "The required sweep/distribution sequence is incomplete or conflicting",
                invalidation_price=acc_low if direction == LONG else acc_high if direction == SHORT else None,
                invalidation_reason="A completed close back through the opposite accumulation boundary invalidates the sequence",
                requires=("confirm with market structure and independent participation evidence",),
                conflicts_with=("both sides of accumulation were swept",) if sweep_side == "conflicted" else (),
                management={"reassess_on":"next completed daily cycle"},
            ),),
            facts=facts,source_bar_epoch=epoch,
 source_timestamp_verified=epoch is not None, reference_close=close, reference_timeframe=close_tf,
            unknowns=("UTC thirds are a transparent proxy; canonical Power of Three session timing depends on venue, broker timezone and DST.",),
        )]
