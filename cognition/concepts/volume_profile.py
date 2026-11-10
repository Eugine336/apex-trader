"""Session/week volume-profile readings with explicit OHLCV proxy provenance."""
from __future__ import annotations

import copy
import hashlib
import threading
from collections import OrderedDict
from typing import Any

from cognition.concepts.auction_utils import (
    clean_bars, last_source_epoch, nearest_structural_level, percentile_strength,
    profile_from_bars, session_info, source_epoch, utc_week_key,
)
from cognition.concepts.base import LONG, SHORT, ConceptReading, Implication, reference_price
from cognition.contracts import EvidenceDomain


_WEEK_PROFILE_CACHE: OrderedDict[
    tuple[str, str, tuple[int, int]], tuple[str, dict[str, Any] | None]
] = OrderedDict()
_WEEK_PROFILE_CACHE_LOCK = threading.RLock()
_WEEK_PROFILE_CACHE_MAX_ENTRIES = 4096


def clear_volume_profile_cache() -> None:
    """Clear cached weekly profiles for deterministic tests and data reloads."""
    with _WEEK_PROFILE_CACHE_LOCK:
        _WEEK_PROFILE_CACHE.clear()


def _week_fingerprint(bars: list[dict]) -> str:
    digest = hashlib.blake2b(digest_size=20)
    for bar in bars:
        for key, value in sorted(bar.items(), key=lambda item: str(item[0])):
            digest.update(str(key).encode("utf-8", errors="replace"))
            digest.update(repr(value).encode("utf-8", errors="replace"))
    return digest.hexdigest()


def _cached_week_profile(
    symbol: str,
    timeframe: str,
    week: tuple[int, int],
    bars: list[dict],
    metrics: dict[str, int],
) -> dict[str, Any] | None:
    """Recompute only a week whose OHLCV content changed.

    Appending a new candle invalidates only the active week. Closed historical
    week profiles are reused; any historical correction changes that week's
    fingerprint and forces a recomputation.
    """
    key = (str(symbol or "").strip().upper(), str(timeframe or "").upper(), week)
    fingerprint = _week_fingerprint(bars)
    with _WEEK_PROFILE_CACHE_LOCK:
        cached = _WEEK_PROFILE_CACHE.get(key)
        if cached is not None and cached[0] == fingerprint:
            _WEEK_PROFILE_CACHE.move_to_end(key)
            metrics["hits"] = metrics.get("hits", 0) + 1
            return copy.deepcopy(cached[1])
    profile = profile_from_bars(bars)
    stored_profile = copy.deepcopy(profile)
    with _WEEK_PROFILE_CACHE_LOCK:
        _WEEK_PROFILE_CACHE[key] = (fingerprint, stored_profile)
        _WEEK_PROFILE_CACHE.move_to_end(key)
        while len(_WEEK_PROFILE_CACHE) > _WEEK_PROFILE_CACHE_MAX_ENTRIES:
            _WEEK_PROFILE_CACHE.popitem(last=False)
    metrics["misses"] = metrics.get("misses", 0) + 1
    return copy.deepcopy(profile)


class VolumeProfileConcept:
    key = "volume_profile"
    family = "volume_auction"
    covers_methods = ()
    covers_lenses = ()
    data_gated_methods = {
        "volume_profile": "verified session calendar and true trade-level volume-at-price feed for session profiles and exact nodes"
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
            unknowns = [
                "true trade-level volume-at-price and intrabar volume distribution; current profile uses an explicit OHLCV price-assignment proxy"
            ]
            week_groups: dict[tuple[int, int], list[dict]] = {}
            for bar in bars:
                bar_epoch = source_epoch(bar)
                if bar_epoch is not None:
                    week_groups.setdefault(utc_week_key(bar_epoch), []).append(bar)
            ordered_weeks = sorted(week_groups)
            current_week = week_groups.get(ordered_weeks[-1], []) if ordered_weeks else []
            previous_week = week_groups.get(ordered_weeks[-2], []) if len(ordered_weeks) > 1 else []
            profile_cache_metrics = {"hits": 0, "misses": 0}
            current_profile = (
                _cached_week_profile(symbol, timeframe, ordered_weeks[-1],
                                     current_week, profile_cache_metrics)
                if ordered_weeks else None
            )
            previous_profile = (
                _cached_week_profile(symbol, timeframe, ordered_weeks[-2],
                                     previous_week, profile_cache_metrics)
                if len(ordered_weeks) > 1 else None
            )
            historical_concentrations = []
            for week in ordered_weeks[:-1]:
                profile = _cached_week_profile(
                    symbol, timeframe, week, week_groups[week], profile_cache_metrics
                )
                if profile and profile["poc_concentration"] is not None:
                    historical_concentrations.append(profile["poc_concentration"])
            strength = percentile_strength(
                current_profile["poc_concentration"] if current_profile else None,
                historical_concentrations,
            )

            session_rows = [(bar, session_info(bar)) for bar in bars]
            session = session_rows[-1][1]
            session_profile = None
            if session["verified"]:
                session_bars = [
                    bar for bar, bar_session in session_rows
                    if bar_session["verified"]
                    and bar_session["open_epoch"] == session["open_epoch"]
                ]
                session_profile = profile_from_bars(session_bars)
            else:
                unknowns.append("verified session calendar for session-scoped profile and session value-area migration")
            if current_profile is None:
                unknowns.append("positive broker volume or explicitly labeled tick-volume proxy")
                state = "profile_unavailable"
                vah = val = poc = None
                long_active = short_active = False
            else:
                vah, val, poc = current_profile["vah"], current_profile["val"], current_profile["poc"]
                close = current["close"]
                if close > vah:
                    state = "accepted_above_value"
                elif close < val:
                    state = "accepted_below_value"
                elif current["high"] > vah and close <= vah:
                    state = "rejected_at_vah"
                elif current["low"] < val and close >= val:
                    state = "rejected_at_val"
                else:
                    state = "inside_value"
                long_active = state in {"accepted_above_value", "rejected_at_val"}
                short_active = state in {"accepted_below_value", "rejected_at_vah"}

            naked_pocs = []
            if previous_profile is not None:
                prior_poc = previous_profile["poc"]
                previous_end = max(source_epoch(item) for item in previous_week)
                current_and_later = [
                    bar for bar in bars if source_epoch(bar) is not None
                    and source_epoch(bar) > previous_end
                ]
                if not any(bar["low"] <= prior_poc <= bar["high"] for bar in current_and_later):
                    naked_pocs.append(prior_poc)
            long_targets = [value for value in naked_pocs if value > current["close"]]
            short_targets = [value for value in naked_pocs if value < current["close"]]
            long_target = min(long_targets) if long_targets else nearest_structural_level(bars, LONG, current["close"])
            short_target = max(short_targets) if short_targets else nearest_structural_level(bars, SHORT, current["close"])
            if long_active and long_target is None:
                unknowns.append("a real untouched POC or confirmed structural high above price for a long objective")
            if short_active and short_target is None:
                unknowns.append("a real untouched POC or confirmed structural low below price for a short objective")
            if current_profile and current_profile["volume_basis"] == "tick_volume_proxy":
                unknowns.append("tick volume is a proxy and does not represent centralized traded volume")

            migration = "unknown"
            if current_profile and previous_profile:
                migration = (
                    "higher_value" if current_profile["poc"] > previous_profile["poc"] else
                    "lower_value" if current_profile["poc"] < previous_profile["poc"] else "unchanged_poc"
                )
            long_invalidation = (val if state == "rejected_at_val" else vah) if long_active and current_profile else None
            short_invalidation = (vah if state == "rejected_at_vah" else val) if short_active and current_profile else None
            long_milestones = (poc,) if long_active and poc is not None and long_target is not None and current["close"] < poc < long_target else ()
            short_milestones = (poc,) if short_active and poc is not None and short_target is not None and short_target < poc < current["close"] else ()
            implications = (
                Implication(
                    direction=LONG,
                    statement=f"Volume auction state is {state}; long-side interpretation is {'active' if long_active else 'not triggered'}.",
                    strength=strength, activated=long_active,
                    activation_reason=state if long_active else "No completed-bar acceptance or rejection supports the long side.",
                    invalidation_price=long_invalidation,
                    invalidation_reason="A completed close back below the accepted value edge, or below the rejected lower node, invalidates the long interpretation.",
                    objective_price=long_target if long_active else None, milestones=long_milestones,
                    requires=tuple(dict.fromkeys(unknowns)),
                    conflicts_with=("Acceptance back inside value after an upside break.", "Value migration down."),
                    management={"meaning": "Use the nearest real naked POC or structural draw; re-evaluate as value migrates."},
                ),
                Implication(
                    direction=SHORT,
                    statement=f"Volume auction state is {state}; short-side interpretation is {'active' if short_active else 'not triggered'}.",
                    strength=strength, activated=short_active,
                    activation_reason=state if short_active else "No completed-bar acceptance or rejection supports the short side.",
                    invalidation_price=short_invalidation,
                    invalidation_reason="A completed close back above the accepted value edge, or above the rejected upper node, invalidates the short interpretation.",
                    objective_price=short_target if short_active else None, milestones=short_milestones,
                    requires=tuple(dict.fromkeys(unknowns)),
                    conflicts_with=("Acceptance back inside value after a downside break.", "Value migration up."),
                    management={"meaning": "Use the nearest real naked POC or structural draw; re-evaluate as value migrates."},
                ),
            )
            facts = {
                "status": state, "profile_period": "UTC_ISO_WEEK",
                "current_week_profile": current_profile, "previous_week_profile": previous_profile,
                "session_profile": session_profile, "poc": poc, "vah": vah, "val": val,
                "hvn": current_profile["hvn"] if current_profile else (),
                "lvn": current_profile["lvn"] if current_profile else (),
                "value_area_migration": migration, "naked_pocs": tuple(naked_pocs),
                "historical_profile_samples": len(historical_concentrations),
                "poc_concentration_percentile": strength,
                "price_assignment": current_profile["price_assignment"] if current_profile else "unavailable",
                "weekly_profile_cache_hits": profile_cache_metrics["hits"],
                "weekly_profile_cache_misses": profile_cache_metrics["misses"],
            }
            readings.append(ConceptReading(
                concept=self.key, timeframe=timeframe, domain=EvidenceDomain.VOLUME,
                applicable=current_profile is not None,
                applicability_reason="A volume profile is applicable when completed OHLC bars have positive broker volume or an explicitly labeled tick-volume proxy.",
                observation=f"{timeframe} weekly volume profile state={state}; session profile availability is reported separately.",
                implications=implications, facts=facts, source_bar_epoch=epoch,
                source_timestamp_verified=epoch is not None,
                reference_close=reference, reference_timeframe=reference_tf,
                unknowns=tuple(dict.fromkeys(unknowns)),
            ))
        return readings


__all__ = ["VolumeProfileConcept", "clear_volume_profile_cache"]
