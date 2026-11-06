"""Opening-range auction reading from verified session boundaries and completed bars."""
from __future__ import annotations

from cognition.concepts.auction_utils import (
    clean_bars, last_source_epoch, nearest_structural_level, percentile_strength,
    session_info, source_epoch, timeframe_seconds,
)
from cognition.concepts.base import LONG, SHORT, ConceptReading, Implication, reference_price
from cognition.contracts import EvidenceDomain


# Opening-range definition: the first 30 completed minutes of a verified session.
OPENING_RANGE_SECONDS = 30 * 60


class OpeningRange:
    key = "opening_range"
    family = "session_structure"
    covers_methods = ()
    covers_lenses = ()
    data_gated_methods = {
        "opening_range": "authoritative venue session calendar with per-bar session id, timezone and open/close timestamps"
    }

    def read(self, symbol: str, bars_by_tf: dict[str, list[dict]]) -> list[ConceptReading]:
        reference, reference_tf = reference_price(bars_by_tf)
        readings = []
        for timeframe, raw in bars_by_tf.items():
            seconds = timeframe_seconds(timeframe)
            if not seconds or seconds > OPENING_RANGE_SECONDS:
                continue
            bars = clean_bars(raw)
            if not bars:
                continue
            current = bars[-1]
            current_session = session_info(current)
            epoch = last_source_epoch(bars)
            if not current_session["verified"]:
                readings.append(self._unknown_reading(
                    timeframe, bars, reference, reference_tf,
                    ["verified session id, timezone and open/close timestamps from an authoritative venue session calendar"],
                    "Opening-range analysis is relevant, but the current bar has no verified session boundary.",
                ))
                continue

            open_epoch = current_session["open_epoch"]
            session_id = current_session["session_id"]
            groups: dict[float, list[dict]] = {}
            for bar in bars:
                info = session_info(bar)
                bar_epoch = source_epoch(bar)
                if info["verified"] and bar_epoch is not None and info["session_id"] == session_id:
                    groups.setdefault(float(info["open_epoch"]), []).append(bar)
            current_group = groups.get(float(open_epoch), [])
            opening = [
                bar for bar in current_group
                if source_epoch(bar) is not None
                and open_epoch <= source_epoch(bar) < open_epoch + OPENING_RANGE_SECONDS
            ]
            if not opening:
                readings.append(self._unknown_reading(
                    timeframe, bars, reference, reference_tf,
                    ["completed bars covering the opening-range definition window"],
                    "The verified session is known, but its opening-range window has not produced completed bars.",
                    {"session_id": session_id, "session_open_epoch": open_epoch},
                ))
                continue
            high = max(bar["high"] for bar in opening)
            low = min(bar["low"] for bar in opening)
            window_complete = max(source_epoch(bar) + seconds for bar in opening if source_epoch(bar) is not None) >= open_epoch + OPENING_RANGE_SECONDS
            if not window_complete:
                readings.append(self._unknown_reading(
                    timeframe, bars, reference, reference_tf,
                    ["the first 30 session minutes must close before the opening range is final"],
                    "The opening-range window is still forming; no break or objective is inferred.",
                    {"session_id": session_id, "session_open_epoch": open_epoch,
                     "opening_range_high": high, "opening_range_low": low},
                ))
                continue

            midpoint = (high + low) / 2.0
            post = [bar for bar in current_group if source_epoch(bar) is not None
                    and source_epoch(bar) >= open_epoch + OPENING_RANGE_SECONDS]
            state, last_break, retest = "inside_range", None, None
            for bar in post:
                close = bar["close"]
                if close > high:
                    state = "accepted_above" if last_break == "above" else "break_above_unconfirmed"
                    last_break = "above"
                    if bar["low"] <= high:
                        retest = "retest_above"
                elif close < low:
                    state = "accepted_below" if last_break == "below" else "break_below_unconfirmed"
                    last_break = "below"
                    if bar["high"] >= low:
                        retest = "retest_below"
                elif last_break == "above":
                    state, last_break = "failed_above", None
                elif last_break == "below":
                    state, last_break = "failed_below", None
            if state == "accepted_above" and retest == "retest_above":
                state = "accepted_above_retested"
            elif state == "accepted_below" and retest == "retest_below":
                state = "accepted_below_retested"

            historical_sizes = []
            for prior_open, group in groups.items():
                if prior_open >= open_epoch:
                    continue
                prior_opening = [bar for bar in group if source_epoch(bar) is not None
                                 and prior_open <= source_epoch(bar) < prior_open + OPENING_RANGE_SECONDS]
                if prior_opening and max(source_epoch(bar) + seconds for bar in prior_opening) >= prior_open + OPENING_RANGE_SECONDS:
                    historical_sizes.append(max(bar["high"] for bar in prior_opening) - min(bar["low"] for bar in prior_opening))
            strength = percentile_strength(high - low, historical_sizes)
            price = current["close"]
            long_active = state in {"accepted_above", "accepted_above_retested", "failed_below"}
            short_active = state in {"accepted_below", "accepted_below_retested", "failed_above"}
            long_invalidation = low if state == "failed_below" else high
            short_invalidation = high if state == "failed_above" else low
            # A failed auction targets the opposite opening-range edge; an accepted
            # breakout targets only a prior confirmed structural level.
            long_target = high if state == "failed_below" else nearest_structural_level(bars, LONG, price)
            short_target = low if state == "failed_above" else nearest_structural_level(bars, SHORT, price)
            unknowns = []
            if long_active and long_target is None:
                unknowns.append("a prior confirmed structural high beyond price for a long breakout objective")
            if short_active and short_target is None:
                unknowns.append("a prior confirmed structural low beyond price for a short breakout objective")
            long_milestones = (midpoint,) if long_active and long_target is not None and price < midpoint < long_target else ()
            short_milestones = (midpoint,) if short_active and short_target is not None and short_target < midpoint < price else ()
            implications = (
                Implication(
                    direction=LONG,
                    statement=f"Opening-range state is {state}; long-side interpretation is {'active' if long_active else 'not triggered'}.",
                    strength=strength, activated=long_active, activation_reason=state,
                    invalidation_price=long_invalidation if long_active else None,
                    invalidation_reason="A completed close back inside invalidates an accepted upside break; a close below the range low invalidates a failed downside auction.",
                    objective_price=long_target if long_active else None, milestones=long_milestones,
                    requires=tuple(unknowns),
                    conflicts_with=("A completed close back inside after upside acceptance.", "A completed close below the opening-range low."),
                    management={"meaning": "Manage accepted breaks against the opening-range edge; failed downside auctions draw toward the opposite edge and then prior structure."},
                ),
                Implication(
                    direction=SHORT,
                    statement=f"Opening-range state is {state}; short-side interpretation is {'active' if short_active else 'not triggered'}.",
                    strength=strength, activated=short_active, activation_reason=state,
                    invalidation_price=short_invalidation if short_active else None,
                    invalidation_reason="A completed close back inside invalidates an accepted downside break; a close above the range high invalidates a failed upside auction.",
                    objective_price=short_target if short_active else None, milestones=short_milestones,
                    requires=tuple(unknowns),
                    conflicts_with=("A completed close back inside after downside acceptance.", "A completed close above the opening-range high."),
                    management={"meaning": "Manage accepted breaks against the opening-range edge; failed upside auctions draw toward the opposite edge and then prior structure."},
                ),
            )
            readings.append(ConceptReading(
                concept=self.key, timeframe=timeframe, domain=EvidenceDomain.SESSION,
                applicable=True,
                applicability_reason="A verified session and a completed opening-range window are present.",
                observation=f"{timeframe} opening range is {low:g}–{high:g}; latest auction state={state}.",
                implications=implications,
                facts={"session_id": session_id, "session_open_epoch": open_epoch,
                       "opening_range_seconds": OPENING_RANGE_SECONDS, "opening_range_high": high,
                       "opening_range_low": low, "opening_range_size": high-low,
                       "midpoint": midpoint, "auction_state": state,
                       "historical_opening_range_samples": len(historical_sizes),
                       "opening_range_size_percentile": strength},source_bar_epoch=epoch,

                source_timestamp_verified=epoch is not None, reference_close=reference, reference_timeframe=reference_tf,
                unknowns=tuple(unknowns),
            ))
        return readings

    @staticmethod
    def _unknown_reading(timeframe, bars, reference, reference_tf, unknowns, observation, facts=None):
        return ConceptReading(
            concept="opening_range", timeframe=timeframe, domain=EvidenceDomain.SESSION,
            applicable=True,
            applicability_reason="Opening-range analysis requires verified session boundaries; missing calendar data remains unknown.",
            observation=observation,
            implications=(
                Implication(LONG, "No verified opening-range state is available for the long side.", None, False,
                            "Session opening range is unavailable.", requires=tuple(unknowns),
                            management={"meaning": "Do not infer an opening range from static UTC windows."}),
                Implication(SHORT, "No verified opening-range state is available for the short side.", None, False,
                            "Session opening range is unavailable.", requires=tuple(unknowns),
                            management={"meaning": "Do not infer an opening range from static UTC windows."}),
            ),
            facts={"status": "data_gated", **(facts or {})},source_bar_epoch=last_source_epoch(bars),

            source_timestamp_verified=last_source_epoch(bars) is not None, reference_close=reference, reference_timeframe=reference_tf,
            unknowns=tuple(unknowns),
        )
