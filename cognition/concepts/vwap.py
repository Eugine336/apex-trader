"""Session and anchored VWAP as contextual, non-directional auction readings."""
from __future__ import annotations

from cognition.concepts.auction_utils import (
    clean_bars, last_source_epoch, nearest_structural_level, percentile_strength,
    session_info, swing_points, volume_of, weighted_vwap,
)
from cognition.concepts.base import LONG, SHORT, ConceptReading, Implication, reference_price
from cognition.contracts import EvidenceDomain


class VWAPConcept:
    key = "vwap"
    family = "volume_auction"
    covers_methods = ()
    covers_lenses = ()
    data_gated_methods = {
        "vwap": "verified venue session calendar for session VWAP and source-verified traded-volume provenance"
    }

    def read(self, symbol: str, bars_by_tf: dict[str, list[dict]]) -> list[ConceptReading]:
        reference, reference_tf = reference_price(bars_by_tf)
        readings = []
        for timeframe, raw in bars_by_tf.items():
            bars = clean_bars(raw)
            if not bars:
                continue
            current = bars[-1]
            epoch = last_source_epoch(bars)
            session = session_info(current)
            unknowns = []
            session_vwap = previous_session_vwap = None
            session_basis = "unavailable"
            session_bars = []
            if session["verified"]:
                session_bars = [
                    bar for bar in bars
                    if session_info(bar)["verified"]
                    and session_info(bar)["open_epoch"] == session["open_epoch"]
                ]
                session_vwap, session_basis = weighted_vwap(session_bars)
                previous_session_vwap, _ = weighted_vwap(session_bars[:-1])
            else:
                unknowns.append("verified venue session calendar and session open timestamp for session VWAP")

            prefix_pv, prefix_v = [0.0], [0.0]
            for bar in bars:
                volume, _basis = volume_of(bar)
                volume = volume if volume is not None and volume > 0 else 0.0
                typical = (bar["high"] + bar["low"] + bar["close"]) / 3.0
                prefix_pv.append(prefix_pv[-1] + typical * volume)
                prefix_v.append(prefix_v[-1] + volume)

            points = swing_points(bars)
            anchors = {point["index"]: point for point in points}
            last_anchor_index = None
            anchored_series = []
            deviation_history = []
            for index, bar in enumerate(bars):
                if index in anchors:
                    last_anchor_index = index
                if last_anchor_index is None:
                    anchored_series.append(None)
                    continue
                denominator = prefix_v[index + 1] - prefix_v[last_anchor_index]
                if denominator <= 0:
                    anchored_series.append(None)
                    continue
                value = (prefix_pv[index + 1] - prefix_pv[last_anchor_index]) / denominator
                anchored_series.append(value)
                deviation_history.append(abs(bar["close"] - value))
            anchored_vwap = anchored_series[-1] if anchored_series else None
            anchor = anchors.get(last_anchor_index) if last_anchor_index is not None else None
            if anchored_vwap is None:
                unknowns.append("a confirmed three-bar swing anchor and positive volume for anchored VWAP")
            primary = session_vwap if session_vwap is not None else anchored_vwap
            basis = session_basis if session_vwap is not None else "anchored_volume"
            if primary is None:
                state, deviation, strength = "unavailable", None, None
                accepted_above = accepted_below = rejected_below = rejected_above = False
            else:
                deviation = current["close"] - primary
                strength = percentile_strength(abs(deviation), deviation_history[:-1])
                previous = bars[-2] if len(bars) > 1 else None
                previous_vwap = previous_session_vwap if session_vwap is not None else (
                    anchored_series[-2] if len(anchored_series) > 1 else None
                )
                accepted_above = bool(previous and previous_vwap is not None
                                      and previous["close"] > previous_vwap and current["close"] > primary)
                accepted_below = bool(previous and previous_vwap is not None
                                      and previous["close"] < previous_vwap and current["close"] < primary)
                rejected_below = current["low"] <= primary and current["close"] > primary
                rejected_above = current["high"] >= primary and current["close"] < primary
                state = (
                    "accepted_above" if accepted_above else
                    "accepted_below" if accepted_below else
                    "rejected_below" if rejected_below else
                    "rejected_above" if rejected_above else "balanced_or_unconfirmed"
                )

            long_active, short_active = accepted_above or rejected_below, accepted_below or rejected_above
            long_target = nearest_structural_level(bars, LONG, current["close"])
            short_target = nearest_structural_level(bars, SHORT, current["close"])
            if long_active and long_target is None:
                unknowns.append("a confirmed structural high beyond price for a long objective")
            if short_active and short_target is None:
                unknowns.append("a confirmed structural low below price for a short objective")
            if "tick" in session_basis or any("tick" in str(bar.get("volume_basis", "")) for bar in bars):
                unknowns.append("tick volume is a proxy and is not centralized traded volume")
            implications = (
                Implication(
                    direction=LONG,
                    statement=f"VWAP state is {state}; the long-side implication is {'active' if long_active else 'not triggered'}.",
                    strength=strength, activated=long_active,
                    activation_reason=state if long_active else "No completed-bar acceptance or rejection supports the long side.",
                    invalidation_price=primary if long_active else None,
                    invalidation_reason="A completed close below the active VWAP invalidates the long interpretation.",
                    objective_price=long_target if long_active else None,
                    requires=tuple(dict.fromkeys(unknowns)),
                    conflicts_with=("Completed closes accepting below VWAP.",),
                    management={"meaning": "VWAP may act as trend support or a mean-reversion draw; reassess on completed closes and refresh the anchor at each confirmed swing."},
                ),
                Implication(
                    direction=SHORT,
                    statement=f"VWAP state is {state}; the short-side implication is {'active' if short_active else 'not triggered'}.",
                    strength=strength, activated=short_active,
                    activation_reason=state if short_active else "No completed-bar acceptance or rejection supports the short side.",
                    invalidation_price=primary if short_active else None,
                    invalidation_reason="A completed close above the active VWAP invalidates the short interpretation.",
                    objective_price=short_target if short_active else None,
                    requires=tuple(dict.fromkeys(unknowns)),
                    conflicts_with=("Completed closes accepting above VWAP.",),
                    management={"meaning": "VWAP may act as trend resistance or a mean-reversion draw; reassess on completed closes and refresh the anchor at each confirmed swing."},
                ),
            )
            readings.append(ConceptReading(
                concept=self.key, timeframe=timeframe, domain=EvidenceDomain.VOLUME,
                applicable=primary is not None,
                applicability_reason="Volume-weighted price is applicable when a verified session or confirmed structural anchor and positive volume are available.",
                observation=f"{timeframe} VWAP state={state}; session VWAP and anchored VWAP are reported separately.",
                implications=implications,
                facts={
                    "status": state, "session_vwap": session_vwap, "anchored_vwap": anchored_vwap,
                    "primary_vwap": primary, "primary_basis": basis, "session_volume_basis": session_basis,
                    "anchor_kind": anchor["kind"] if anchor else None,
                    "anchor_epoch": anchor["epoch"] if anchor else None,
                    "deviation_price": deviation, "absolute_deviation_percentile": strength,
                    "accepted_above": accepted_above, "accepted_below": accepted_below,
                    "rejected_below": rejected_below, "rejected_above": rejected_above,
                    "completed_bar_close": current["close"],
                },source_bar_epoch=epoch,

                source_timestamp_verified=epoch is not None, reference_close=reference, reference_timeframe=reference_tf,
                unknowns=tuple(dict.fromkeys(unknowns)),
            ))
        return readings
