"""Wyckoff and volume-intelligence readings over completed broker OHLCV.

These readers report auction effort/result, volume-derived measurements, and
competing Wyckoff interpretations. They are descriptive evidence only: they do
not infer aggressor flow from tick volume, choose a trade, or authorize action.
"""
from __future__ import annotations

import math
from statistics import mean, median, pstdev
from typing import Any

from cognition.concepts.auction_utils import clean_bars, last_source_epoch, volume_of, swing_points
from cognition.concepts.base import ConceptReading, percentile_rank, reference_price
from cognition.contracts import EvidenceDomain


_MIN_VOLUME_BARS = 21
_EFFORT_BASELINE = 20
_MFI_PERIOD = 14
_CMF_PERIOD = 20
_WYCKOFF_RANGE = 40
_SWING_RADIUS = 1
_TAIL_QUANTILE = 0.90


def _safe_ratio(numerator: float | None, denominator: float | None) -> float | None:
    if numerator is None or denominator is None or denominator == 0:
        return None
    value = numerator / denominator
    return value if math.isfinite(value) else None


def _true_range(bar: dict[str, Any], previous_close: float | None) -> float:
    if previous_close is None:
        return max(0.0, bar["high"] - bar["low"])
    return max(
        bar["high"] - bar["low"],
        abs(bar["high"] - previous_close),
        abs(bar["low"] - previous_close),
    )


def _close_location(bar: dict[str, Any]) -> float | None:
    width = bar["high"] - bar["low"]
    return (bar["close"] - bar["low"]) / width if width > 0 else None


def _series_volume(bars: list[dict[str, Any]], key: str = "volume") -> list[float | None]:
    values = []
    for bar in bars:
        raw = bar.get(key)
        if raw is None and key == "volume":
            raw, _ = volume_of(bar)
        try:
            value = float(raw)
        except (TypeError, ValueError, OverflowError):
            value = math.nan
        values.append(value if math.isfinite(value) and value >= 0 else None)
    return values


def _volume_basis(bars: list[dict[str, Any]]) -> str:
    bases = {
        str(bar.get("volume_source") or bar.get("volume_basis") or "unavailable").lower()
        for bar in bars if bar.get("volume") is not None
    }
    if not bases:
        return "unavailable"
    if len(bases) == 1:
        return next(iter(bases))
    return "mixed_volume_sources"


def _rolling_mean(values: list[float], period: int) -> list[float | None]:
    result: list[float | None] = []
    for index in range(len(values)):
        if index + 1 < period:
            result.append(None)
        else:
            result.append(mean(values[index + 1 - period:index + 1]))
    return result


def _obv(closes: list[float], volumes: list[float]) -> list[float]:
    result = [0.0]
    for index in range(1, len(closes)):
        delta = closes[index] - closes[index - 1]
        result.append(result[-1] + (volumes[index] if delta > 0 else -volumes[index] if delta < 0 else 0.0))
    return result


def _mfi(bars: list[dict[str, Any]], volumes: list[float]) -> list[float | None]:
    typical = [(bar["high"] + bar["low"] + bar["close"]) / 3.0 for bar in bars]
    flows = [typical[index] * volumes[index] for index in range(len(bars))]
    result: list[float | None] = [None] * len(bars)
    for end in range(_MFI_PERIOD, len(bars)):
        positive = negative = 0.0
        for index in range(end - _MFI_PERIOD + 1, end + 1):
            if typical[index] > typical[index - 1]:
                positive += flows[index]
            elif typical[index] < typical[index - 1]:
                negative += flows[index]
        if positive == 0.0 and negative == 0.0:
            result[end] = 50.0
        elif negative == 0.0:
            result[end] = 100.0
        else:
            result[end] = 100.0 - 100.0 / (1.0 + positive / negative)
    return result


def _cmf(bars: list[dict[str, Any]], volumes: list[float]) -> list[float | None]:
    multiplier_volume = []
    for bar, volume in zip(bars, volumes):
        width = bar["high"] - bar["low"]
        multiplier = ((bar["close"] - bar["low"]) - (bar["high"] - bar["close"])) / width if width > 0 else 0.0
        multiplier_volume.append(multiplier * volume)
    result: list[float | None] = [None] * len(bars)
    for end in range(_CMF_PERIOD - 1, len(bars)):
        denominator = sum(volumes[end - _CMF_PERIOD + 1:end + 1])
        result[end] = (
            sum(multiplier_volume[end - _CMF_PERIOD + 1:end + 1]) / denominator
            if denominator > 0 else None
        )
    return result


def _ad_line(bars: list[dict[str, Any]], volumes: list[float]) -> list[float]:
    total = 0.0
    result = []
    for bar, volume in zip(bars, volumes):
        width = bar["high"] - bar["low"]
        multiplier = ((bar["close"] - bar["low"]) - (bar["high"] - bar["close"])) / width if width > 0 else 0.0
        total += multiplier * volume
        result.append(total)
    return result


def _roc(values: list[float], period: int) -> float | None:
    if len(values) <= period or values[-period - 1] == 0:
        return None
    return (values[-1] / values[-period - 1]) - 1.0


def _slope(values: list[float], period: int) -> float | None:
    if len(values) < period:
        return None
    sample = values[-period:]
    centre_x = (period - 1) / 2.0
    centre_y = mean(sample)
    denominator = sum((index - centre_x) ** 2 for index in range(period))
    return sum((index - centre_x) * (value - centre_y) for index, value in enumerate(sample)) / denominator if denominator else None


def _volume_divergence(
    bars: list[dict[str, Any]], indicator: list[float],
) -> dict[str, Any]:
    pivots = swing_points(bars)
    highs = [item for item in pivots if item["kind"] == "high"]
    lows = [item for item in pivots if item["kind"] == "low"]
    result: dict[str, Any] = {
        "bearish": {"observed": False, "reason": "two confirmed price swing highs with corresponding indicator values are required"},
        "bullish": {"observed": False, "reason": "two confirmed price swing lows with corresponding indicator values are required"},
        "lookahead": False,
    }
    for side, points, kind in (("bearish", highs, "high"), ("bullish", lows, "low")):
        valid = [
            item for item in points
            if item["index"] < len(indicator) and indicator[item["index"]] is not None
        ]
        if len(valid) < 2:
            continue
        prior, latest = valid[-2], valid[-1]
        prior_value = float(indicator[prior["index"]])
        latest_value = float(indicator[latest["index"]])
        price_higher = latest["price"] > prior["price"] if kind == "high" else latest["price"] < prior["price"]
        indicator_weaker = latest_value < prior_value if kind == "high" else latest_value > prior_value
        result[side] = {
            "observed": bool(price_higher and indicator_weaker),
            "prior_price_swing": prior["price"],
            "latest_price_swing": latest["price"],
            "prior_indicator": prior_value,
            "latest_indicator": latest_value,
            "price_extreme_relation": "higher" if (latest["price"] > prior["price"]) else "lower" if latest["price"] < prior["price"] else "equal",
            "indicator_relation": "lower" if latest_value < prior_value else "higher" if latest_value > prior_value else "equal",
            "reason": "confirmed price extreme is not matched by the same-direction indicator extreme" if price_higher and indicator_weaker else "no opposing confirmed price/indicator extreme relationship",
            "pivots": [prior["index"], latest["index"]],
        }
    return result


def _effort_result_facts(bars: list[dict[str, Any]]) -> tuple[dict[str, Any], list[str]]:
    unknowns: list[str] = []
    volumes = _series_volume(bars)
    valid_volumes = [value for value in volumes if value is not None]
    if len(valid_volumes) < _MIN_VOLUME_BARS or any(value is None for value in volumes[-_EFFORT_BASELINE:]):
        unknowns.append("at least 21 completed, non-missing volume observations for the effort baseline")
    ranges = [bar["high"] - bar["low"] for bar in bars]
    true_ranges = [
        _true_range(bar, bars[index - 1]["close"] if index else None)
        for index, bar in enumerate(bars)
    ]
    prior_volumes = [value for value in volumes[-_EFFORT_BASELINE - 1:-1] if value is not None]
    current_volume = volumes[-1] if volumes else None
    baseline_volume = median(prior_volumes) if len(prior_volumes) == _EFFORT_BASELINE else None
    prior_ranges = ranges[-_EFFORT_BASELINE - 1:-1]
    prior_true_ranges = true_ranges[-_EFFORT_BASELINE - 1:-1]
    current_range = ranges[-1] if ranges else None
    current_true_range = true_ranges[-1] if true_ranges else None
    volume_ratio = _safe_ratio(current_volume, baseline_volume)
    range_ratio = _safe_ratio(current_true_range, median(prior_true_ranges)) if len(prior_true_ranges) == _EFFORT_BASELINE else None
    volume_percentile = percentile_rank(current_volume, prior_volumes) if current_volume is not None and len(prior_volumes) >= 10 else None
    range_percentile = percentile_rank(current_range, prior_ranges) if current_range is not None and len(prior_ranges) >= 10 else None
    close_location = _close_location(bars[-1]) if bars else None
    body = abs(bars[-1]["close"] - bars[-1]["open"]) if bars else None
    body_to_range = _safe_ratio(body, current_range)
    price_change = bars[-1]["close"] - bars[-2]["close"] if len(bars) >= 2 else None
    atr_reference = median(prior_true_ranges) if len(prior_true_ranges) == _EFFORT_BASELINE else None
    result_per_effort = (
        _safe_ratio(current_true_range, baseline_volume and current_volume / baseline_volume)
        if current_true_range is not None and baseline_volume and baseline_volume > 0 and current_volume is not None
        else None
    )
    state = "not_assessable"
    if volume_percentile is not None and range_percentile is not None:
        if volume_percentile >= _TAIL_QUANTILE and range_percentile <= 1.0 - _TAIL_QUANTILE:
            state = "high_effort_limited_price_result"
        elif volume_percentile >= _TAIL_QUANTILE and range_percentile >= _TAIL_QUANTILE:
            state = "high_effort_expansive_result"
        elif volume_percentile <= 1.0 - _TAIL_QUANTILE and range_percentile <= 1.0 - _TAIL_QUANTILE:
            state = "low_effort_limited_result"
        else:
            state = "ordinary_effort_result"
    real_volumes = _series_volume(bars, "real_volume")
    real_valid = [value for value in real_volumes if value is not None and value > 0]
    real_volume_available = len(real_valid) == len(bars) and len(bars) >= _MIN_VOLUME_BARS
    facts = {
        "volume_basis": _volume_basis(bars),
        "current_volume": current_volume,
        "prior_volume_median": baseline_volume,
        "volume_ratio_to_prior_median": volume_ratio,
        "volume_percentile_vs_prior_observations": volume_percentile,
        "current_true_range": current_true_range,
        "prior_true_range_median": atr_reference,
        "true_range_ratio_to_prior_median": range_ratio,
        "range_percentile_vs_prior_observations": range_percentile,
        "close_location_in_bar": close_location,
        "body_to_range_ratio": body_to_range,
        "close_to_close_change": price_change,
        "range_per_relative_volume": result_per_effort,
        "effort_result_state": state,
        "tail_quantile_convention": _TAIL_QUANTILE,
        "real_volume": {
            "available_for_full_window": real_volume_available,
            "sample_count": len(real_valid),
            "current": real_volumes[-1] if real_volume_available else None,
            "basis": "broker_real_volume_field" if real_volume_available else "unavailable_or_incomplete",
        },
        "tick_volume_is_trade_aggressor_volume": False,
        "absorption_is_confirmed": False,
        "absorption_confirmation_requires": [
            "source-verified aggressor-side executed volume",
            "or verified trade-level volume-at-price with time ordering",
            "and independent price response / replenishment observations",
        ],
        "directional_decision_emitted": False,
        "entry_authorization_emitted": False,
        "reading_scope": "latest_completed_bar_effort_result",
        "interpretation_invalidation": [
            "a later completed close beyond the current bar's high/low changes the limited-result observation into follow-through",
            "source volume semantics or timestamp attestation fail validation",
        ],
    }
    if facts["volume_basis"] in {"tick_volume_proxy", "mixed_volume_sources"}:
        unknowns.append("tick volume is an activity proxy, not centralized executed volume or aggressor flow")
    if not real_volume_available:
        unknowns.append("complete broker real_volume observations are not available for the entire comparison window")
    if volume_percentile is None or range_percentile is None:
        unknowns.append("own-history volume and true-range percentiles cannot be established")
    return facts, unknowns


def _volume_indicator_facts(bars: list[dict[str, Any]]) -> tuple[dict[str, Any], list[str]]:
    unknowns: list[str] = []
    raw_volumes = _series_volume(bars)
    if len(bars) < _MIN_VOLUME_BARS or any(value is None for value in raw_volumes[-_MIN_VOLUME_BARS:]):
        unknowns.append("at least 21 consecutive completed bars with valid volume observations")
    # Use only the latest contiguous observed run. Compressing missing-volume
    # bars would make non-adjacent prices appear adjacent and corrupt indicators.
    last_missing = max(
        (index for index, value in enumerate(raw_volumes) if value is None),
        default=-1,
    )
    clean = bars[last_missing + 1:]
    contiguous_volumes = raw_volumes[last_missing + 1:]
    volumes = [float(value) for value in contiguous_volumes if value is not None]
    if len(clean) < _MIN_VOLUME_BARS or not any(value > 0 for value in volumes):
        return {
            "volume_basis": _volume_basis(bars),
            "valid_volume_observations": len(clean),
            "obv": None, "mfi_14": None, "chaikin_money_flow_20": None,
            "accumulation_distribution": None, "volume_roc_10": None,
            "divergence": {"observed": False, "reason": "insufficient complete volume history"},
            "no_directional_signal": True,
        }, unknowns
    closes = [bar["close"] for bar in clean]
    obv = _obv(closes, volumes)
    mfi = _mfi(clean, volumes)
    cmf = _cmf(clean, volumes)
    ad = _ad_line(clean, volumes)
    volume_roc = _roc(volumes, 10)
    volume_mean = mean(volumes[-_EFFORT_BASELINE:])
    volume_median = median(volumes[-_EFFORT_BASELINE:])
    volume_cv = (
        pstdev(volumes[-_EFFORT_BASELINE:]) / volume_mean
        if volume_mean > 0 else None
    )
    obv_slope = _slope(obv, min(10, len(obv)))
    mfi_history = [value for value in mfi if value is not None]
    divergence = {
        "obv": _volume_divergence(clean, obv),
        "mfi": _volume_divergence(clean, mfi),
    }
    real_volumes = _series_volume(bars, "real_volume")
    real_complete = len(real_volumes) == len(bars) and all(value is not None and value > 0 for value in real_volumes[-_MIN_VOLUME_BARS:])
    facts = {
        "volume_basis": _volume_basis(clean),
        "valid_volume_observations": len(clean),
        "volume_mean_20": volume_mean,
        "volume_median_20": volume_median,
        "volume_coefficient_of_variation_20": volume_cv,
        "volume_rate_of_change_10": volume_roc,
        "obv": obv[-1],
        "obv_slope_10": obv_slope,
        "mfi_14": mfi[-1],
        "mfi_14_recent_range": [min(mfi_history[-20:]), max(mfi_history[-20:])] if mfi_history else None,
        "chaikin_money_flow_20": cmf[-1],
        "accumulation_distribution_line": ad[-1],
        "ad_line_change_10": ad[-1] - ad[-11] if len(ad) >= 11 else None,
        "confirmed_price_indicator_divergence": divergence,
        "real_volume": {
            "complete_for_recent_window": real_complete,
            "current": real_volumes[-1] if real_complete else None,
        },
        "indicator_semantics": {
            "obv": "signed cumulative selected bar volume by close-to-close price change; not aggressor delta",
            "mfi": "typical-price money-flow ratio; depends on the selected volume basis",
            "chaikin_money_flow": "close-location weighted volume ratio over 20 completed bars",
            "accumulation_distribution": "close-location weighted cumulative bar volume; not exchange order flow",
        },
        "no_directional_signal": True,
        "divergence_invalidation_conditions": [
            "a later confirmed price and indicator swing jointly establish a new extreme in the direction that resolves the divergence",
            "the comparison pivots are superseded by newer confirmed swings",
        ],
        "directional_decision_emitted": False,
        "entry_authorization_emitted": False,
    }
    if facts["volume_basis"] == "tick_volume_proxy":
        unknowns.append("OBV, MFI, CMF and accumulation/distribution use broker tick-volume proxy, not true traded volume")
    if not real_complete:
        unknowns.append("real_volume is absent, zero, or incomplete; real-volume-specific confirmation is unavailable")
    if mfi[-1] is None:
        unknowns.append("MFI requires 14 prior typical-price comparisons with usable volume")
    if cmf[-1] is None:
        unknowns.append("Chaikin money flow requires a complete 20-bar volume window")
    return facts, unknowns


def _wyckoff_facts(bars: list[dict[str, Any]], effort: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    unknowns: list[str] = []
    if len(bars) < _WYCKOFF_RANGE + 2:
        unknowns.append("at least 42 completed bars for the preceding trading-range and event-sequence context")
    prior = bars[-_WYCKOFF_RANGE - 1:-1]
    current = bars[-1]
    prior_high = max((bar["high"] for bar in prior), default=None)
    prior_low = min((bar["low"] for bar in prior), default=None)
    prior_range = prior_high - prior_low if prior_high is not None and prior_low is not None else None
    close_location = _close_location(current)
    spring = bool(prior_low is not None and current["low"] < prior_low and current["close"] > prior_low)
    upthrust = bool(prior_high is not None and current["high"] > prior_high and current["close"] < prior_high)
    upside_acceptance = bool(prior_high is not None and current["close"] > prior_high)
    downside_acceptance = bool(prior_low is not None and current["close"] < prior_low)
    pivots = swing_points(bars)
    recent_events = []
    for pivot in pivots[-12:]:
        bar = bars[pivot["index"]]
        recent_events.append({
            "kind": pivot["kind"],
            "price": pivot["price"],
            "index": pivot["index"],
            "timestamp_verified": pivot.get("epoch") is not None,
            "close_location": _close_location(bar),
            "volume": bar.get("volume"),
            "volume_basis": bar.get("volume_source") or bar.get("volume_basis"),
        })
    bullish_support = []
    bearish_support = []
    if spring:
        bullish_support.append("current bar traded below the preceding 40-bar range low and closed back inside")
    if upthrust:
        bearish_support.append("current bar traded above the preceding 40-bar range high and closed back inside")
    if upside_acceptance:
        bullish_support.append("latest completed close is above the preceding 40-bar range")
    if downside_acceptance:
        bearish_support.append("latest completed close is below the preceding 40-bar range")
    if effort.get("effort_result_state") == "high_effort_limited_price_result":
        bullish_support.append("high effort with limited range result; location and aggressor evidence are not yet known")
        bearish_support.append("high effort with limited range result; location and aggressor evidence are not yet known")
    if close_location is not None and close_location >= 0.75:
        bullish_support.append("close is in the upper quartile of the current bar")
    if close_location is not None and close_location <= 0.25:
        bearish_support.append("close is in the lower quartile of the current bar")
    if effort.get("effort_result_state") == "high_effort_expansive_result":
        bullish_support.append("high effort accompanied by an unusually expansive price result") if upside_acceptance else None
        bearish_support.append("high effort accompanied by an unusually expansive price result") if downside_acceptance else None
    bullish_invalidation = current["low"] if spring else prior_low
    bearish_invalidation = current["high"] if upthrust else prior_high
    if bullish_support and bearish_support:
        interpretation = "competing_accumulation_and_distribution_evidence"
    elif bullish_support:
        interpretation = "accumulation_or_markup_hypothesis"
    elif bearish_support:
        interpretation = "distribution_or_markdown_hypothesis"
    elif prior_range is not None and prior_range > 0:
        interpretation = "trading_range_development"
    else:
        interpretation = "unresolved"
    facts = {
        "preceding_range": {
            "bars": len(prior),
            "high": prior_high,
            "low": prior_low,
            "width": prior_range,
            "current_close_location_in_range": (
                (current["close"] - prior_low) / prior_range
                if prior_range and prior_range > 0 else None
            ),
        },
        "current_auction_events": {
            "spring_candidate": spring,
            "upthrust_candidate": upthrust,
            "sign_of_strength_close": upside_acceptance,
            "sign_of_weakness_close": downside_acceptance,
            "event_confirmation": "completed_ohlc_only; not confirmed order flow",
        },
        "confirmed_swing_sequence": recent_events,
        "competing_interpretations": {
            "accumulation_or_markup": {
                "supporting_observations": bullish_support,
                "invalidation_level": bullish_invalidation,
                "invalidation_condition": "completed close below the cited structural low",
                "counter_evidence": bearish_support,
                "status": "supported_for_brain_examination" if bullish_support else "not_supported_by_current_observations",
            },
            "distribution_or_markdown": {
                "supporting_observations": bearish_support,
                "invalidation_level": bearish_invalidation,
                "invalidation_condition": "completed close above the cited structural high",
                "counter_evidence": bullish_support,
                "status": "supported_for_brain_examination" if bearish_support else "not_supported_by_current_observations",
            },
        },
        "interpretation_state": interpretation,
        "coverage_status": "PARTIAL",
        "unimplemented_wyckoff_components": [
            "complete primary/secondary cause and test sequence",
            "confirmed selling climax or buying climax and automatic rally or reaction sequence",
            "secondary tests and last-point-of-support or last-point-of-supply sequence",
            "campaign-level phase transitions with point-of-control migration",
        ],
        "phase_not_overclaimed": True,
        "volume_is_not_aggressor_flow": True,
        "directional_decision_emitted": False,
        "entry_authorization_emitted": False,
    }
    if len(prior) < _WYCKOFF_RANGE:
        unknowns.append("full 40-bar preceding range history")
    if effort.get("volume_basis") in {"tick_volume_proxy", "unavailable"}:
        unknowns.append("true exchange or executed-volume confirmation for Wyckoff effort/result")
    unknowns.extend([
        "point-of-control movement and full cause/test sequence across the complete accumulation or distribution campaign",
        "source-verified aggressor and trade-level volume to distinguish absorption from passive bar-volume patterns",
    ])
    return facts, unknowns


class WyckoffVolumeReadings:
    key = "wyckoff_volume_readings"
    family = "auction_volume"
    covers_methods = ("wyckoff_effort_result", "volume_indicators")
    covers_lenses = ()

    def read(self, symbol: str, bars_by_tf: dict[str, list[dict]]) -> list[ConceptReading]:
        reference, reference_tf = reference_price(bars_by_tf)
        readings: list[ConceptReading] = []
        for timeframe in sorted(bars_by_tf, key=lambda item: item):
            bars = clean_bars(bars_by_tf.get(timeframe))
            if not bars:
                continue
            epoch = last_source_epoch(bars)
            base_unknowns = [] if epoch is not None else [
                "source-verified timestamp for the latest completed bar",
            ]
            effort, effort_unknowns = _effort_result_facts(bars)
            volume, volume_unknowns = _volume_indicator_facts(bars)
            wyckoff, wyckoff_unknowns = _wyckoff_facts(bars, effort)

            effort_applicable = effort["volume_basis"] != "unavailable" and effort["volume_percentile_vs_prior_observations"] is not None
            readings.append(ConceptReading(
                concept="wyckoff_effort_result",
                timeframe=timeframe,
                domain=EvidenceDomain.VOLUME,
                applicable=effort_applicable,
                applicability_reason=(
                    "Completed bars support a comparison of selected volume effort against price range/result."
                    if effort_applicable else "A complete own-history volume and range baseline is unavailable."
                ),
                observation=(
                    f"{symbol} {timeframe}: effort/result state is {effort['effort_result_state']}; "
                    f"volume basis is {effort['volume_basis']}. This does not establish aggressor direction or absorption."
                ),
                implications=(),
                facts=effort,
                source_bar_epoch=epoch,
                reference_close=reference,
                reference_timeframe=reference_tf,
                unknowns=tuple(dict.fromkeys(base_unknowns + effort_unknowns)),
            ))
            volume_applicable = volume.get("obv") is not None and volume.get("mfi_14") is not None
            readings.append(ConceptReading(
                concept="volume_indicators",
                timeframe=timeframe,
                domain=EvidenceDomain.VOLUME,
                applicable=volume_applicable,
                applicability_reason=(
                    "The selected volume basis supports OBV, MFI, CMF and accumulation/distribution measurements."
                    if volume_applicable else "Insufficient complete volume observations for the core indicators."
                ),
                observation=(
                    f"{symbol} {timeframe}: volume-derived measurements are available on "
                    f"{volume['volume_basis']} basis; indicator divergence is descriptive and not an order-flow signal."
                ),
                implications=(),
                facts=volume,
                source_bar_epoch=epoch,
                reference_close=reference,
                reference_timeframe=reference_tf,
                unknowns=tuple(dict.fromkeys(base_unknowns + volume_unknowns)),
            ))
            wyckoff_applicable = len(bars) >= _WYCKOFF_RANGE + 2
            readings.append(ConceptReading(
                concept="wyckoff",
                timeframe=timeframe,
                domain=EvidenceDomain.STRUCTURE,
                applicable=wyckoff_applicable,
                applicability_reason=(
                    "Completed price/volume history supports range, spring/upthrust and competing auction interpretations."
                    if wyckoff_applicable else "The preceding range and event sequence do not have sufficient completed-bar history."
                ),
                observation=(
                    f"{symbol} {timeframe}: partial Wyckoff auction observations are {wyckoff['interpretation_state']}; "
                    "the full Wyckoff campaign phase is not established, and the Brain must compare both accumulation and distribution hypotheses."
                ),
                implications=(),
                facts=wyckoff,
                source_bar_epoch=epoch,
                reference_close=reference,
                reference_timeframe=reference_tf,
                unknowns=tuple(dict.fromkeys(base_unknowns + wyckoff_unknowns)),
            ))
        return readings
