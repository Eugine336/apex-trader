"""Wyckoff and volume-intelligence readings over completed broker OHLCV.

These readers provide full contextual Wyckoff phase, effort/result and volume
indicator interpretations. They are descriptive evidence only: they do not infer
aggressor flow from tick volume, choose a trade, or authorize action.
"""
from __future__ import annotations

import math
from statistics import mean, median, pstdev
from typing import Any

from cognition.concepts.auction_utils import (
    clean_bars, last_source_epoch, nearest_structural_level, volume_of, swing_points,
)
from cognition.concepts.base import (
    FLAT, LONG, SHORT, ConceptReading, Implication, percentile_rank, reference_price,
)
from cognition.contracts import EvidenceDomain


_MIN_VOLUME_BARS = 21
_EFFORT_BASELINE = 20
_MFI_PERIOD = 14
_CMF_PERIOD = 20
_WYCKOFF_RANGE = 40
_SWING_RADIUS = 1


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
    effort_result_gap = (
        volume_percentile - range_percentile
        if volume_percentile is not None and range_percentile is not None else None
    )
    historical_gaps: list[float] = []
    for end in range(_EFFORT_BASELINE + 1, len(bars) - 1):
        past_volume = volumes[end]
        prior_volume_window = volumes[end - _EFFORT_BASELINE:end]
        prior_range_window = ranges[end - _EFFORT_BASELINE:end]
        if (
            past_volume is None
            or any(value is None for value in prior_volume_window)
            or len(prior_range_window) != _EFFORT_BASELINE
        ):
            continue
        volume_rank = percentile_rank(past_volume, [float(v) for v in prior_volume_window])
        range_rank = percentile_rank(ranges[end], [float(v) for v in prior_range_window])
        if volume_rank is not None and range_rank is not None:
            historical_gaps.append(abs(volume_rank - range_rank))
    effort_result_strength = (
        percentile_rank(abs(effort_result_gap), historical_gaps)
        if effort_result_gap is not None and historical_gaps else None
    )
    state = (
        "volume_effort_rank_exceeds_range_result" if effort_result_gap is not None and effort_result_gap > 0 else
        "range_result_rank_exceeds_volume_effort" if effort_result_gap is not None and effort_result_gap < 0 else
        "effort_result_ranks_aligned" if effort_result_gap is not None else
        "not_assessable"
    )
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
        "effort_result_rank_gap": effort_result_gap,
        "effort_result_strength_percentile": effort_result_strength,
        "effort_result_state": state,
        "comparison_method": "volume_percentile_and_true_range_percentile_against_prior_own_history",
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
    def _alignment_at(end: int) -> tuple[float | None, float | None, float | None]:
        values: list[float] = []
        obv_value = _slope(obv[:end + 1], min(10, end + 1))
        mfi_value = mfi[end] - 50.0 if end < len(mfi) and mfi[end] is not None else None
        cmf_value = cmf[end] if end < len(cmf) else None
        ad_value = ad[end] - ad[end - 10] if end >= 10 else None
        for value in (obv_value, mfi_value, cmf_value, ad_value):
            if value is not None and value != 0:
                values.append(value)
        if not values:
            return None, None, None
        positive = sum(1 for value in values if value > 0) / len(values)
        negative = sum(1 for value in values if value < 0) / len(values)
        return positive, negative, abs(positive - negative)

    positive_ratio, negative_ratio, alignment = _alignment_at(len(clean) - 1)
    alignment_history = [
        score for end in range(10, len(clean) - 1)
        if (score := _alignment_at(end)[2]) is not None
    ]
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
        "indicator_positive_ratio": positive_ratio,
        "indicator_negative_ratio": negative_ratio,
        "indicator_alignment_score": alignment,
        "indicator_alignment_history": alignment_history,
        "indicator_alignment_sample_count": len(alignment_history),
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


def _wyckoff_event_rows(bars: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Create completed-bar climax, Spring/UTAD and SOS/SOW event candidates."""
    volumes = _series_volume(bars)
    rows: list[dict[str, Any]] = []
    for index in range(_WYCKOFF_RANGE, len(bars)):
        prior = bars[index - _WYCKOFF_RANGE:index]
        current = bars[index]
        prior_high = max(item["high"] for item in prior)
        prior_low = min(item["low"] for item in prior)
        prior_volumes = volumes[index - _EFFORT_BASELINE:index]
        current_volume = volumes[index]
        volume_record = (
            current_volume is not None and len(prior_volumes) == _EFFORT_BASELINE
            and all(item is not None for item in prior_volumes)
            and current_volume > max(float(item) for item in prior_volumes)
        )
        spring = current["low"] < prior_low and current["close"] > prior_low
        upthrust = current["high"] > prior_high and current["close"] < prior_high
        rows.append({
            "index": index, "prior_range_high": prior_high, "prior_range_low": prior_low,
            "selling_climax": bool(volume_record and current["low"] < prior_low and current["close"] <= current["open"]),
            "buying_climax": bool(volume_record and current["high"] > prior_high and current["close"] >= current["open"]),
            "spring": spring, "utad": upthrust,
            "sos": current["close"] > prior_high, "sow": current["close"] < prior_low,
            "high": current["high"], "low": current["low"], "close": current["close"],
            "volume": current_volume,
            "volume_basis": current.get("volume_basis") or current.get("volume_source") or "unavailable",
            "source_epoch": current.get("timestamp_epoch") if current.get("source_timestamp_verified") is True else None,
        })
    return rows


def _first_pivot_after(pivots: list[dict[str, Any]], index: int, kind: str) -> dict[str, Any] | None:
    return next((point for point in pivots if point["index"] > index and point["kind"] == kind), None)


def _wyckoff_facts(
    bars: list[dict[str, Any]], effort: dict[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    """Read sequential accumulation and distribution campaigns independently."""
    unknowns: list[str] = []
    event_rows = _wyckoff_event_rows(bars)
    pivots = swing_points(bars)
    if len(bars) < _WYCKOFF_RANGE + 2:
        unknowns.append("at least 42 completed bars for a preceding range and sequential event context")
    if not event_rows:
        unknowns.append("a completed preceding range for Wyckoff event evaluation")
    sc = next((row for row in reversed(event_rows) if row["selling_climax"]), None)
    bc = next((row for row in reversed(event_rows) if row["buying_climax"]), None)
    accumulation: dict[str, Any] = {"phase": None, "events": [], "range_high": None, "range_low": None, "invalidation_level": None, "objective_price": None}
    distribution: dict[str, Any] = {"phase": None, "events": [], "range_high": None, "range_low": None, "invalidation_level": None, "objective_price": None}
    all_events: list[dict[str, Any]] = []

    def add_event(rows: list[dict[str, Any]], name: str, row: dict[str, Any] | None, key: str) -> None:
        if row is not None:
            rows.append({
                "event": name, "index": row["index"], "price": row.get(key),
                "source_timestamp_verified": row.get("source_epoch") is not None,
                "source_timestamp_epoch": row.get("source_epoch"),
            })

    if sc is not None:
        ar = _first_pivot_after(pivots, sc["index"], "high")
        st = _first_pivot_after(pivots, ar["index"], "low") if ar else None
        st_valid = bool(st is not None and st["price"] >= sc["low"])
        spring = next((row for row in event_rows if st_valid and row["index"] > st["index"] and row["spring"]), None)
        sos = next((row for row in event_rows if spring is not None and row["index"] > spring["index"] and row["sos"]), None)
        lps = _first_pivot_after(pivots, sos["index"], "low") if sos else None
        lps_held = bool(lps is not None and sos is not None and lps["price"] >= sos["prior_range_high"] and bars[-1]["close"] >= sos["prior_range_high"])
        phase_a = "E" if lps_held else "D" if sos is not None else "C" if spring is not None else "B" if st_valid else "A" if ar is not None else None
        acc_events: list[dict[str, Any]] = []
        add_event(acc_events, "SC", sc, "low")
        add_event(acc_events, "AR", ar, "price")
        add_event(acc_events, "ST", st if st_valid else None, "price")
        add_event(acc_events, "Spring", spring, "low")
        add_event(acc_events, "SOS", sos, "close")
        add_event(acc_events, "LPS_BU", lps if lps_held else None, "price")
        range_high = ar["price"] if ar is not None else sc["prior_range_high"]
        range_low = spring["low"] if spring is not None else sc["low"]
        width = range_high - range_low if range_high is not None and range_low is not None else None
        breakout = sos["prior_range_high"] if sos is not None else None
        accumulation = {
            "phase": phase_a, "events": acc_events, "range_high": range_high, "range_low": range_low,
            "invalidation_level": range_low, "breakout_level": breakout,
            "objective_price": breakout + width if breakout is not None and width is not None and width > 0 else None,
            "last_event_index": lps["index"] if lps_held else sos["index"] if sos is not None else spring["index"] if spring is not None else st["index"] if st_valid else ar["index"] if ar is not None else sc["index"],
        }
        all_events.extend(acc_events)

    if bc is not None:
        ar = _first_pivot_after(pivots, bc["index"], "low")
        st = _first_pivot_after(pivots, ar["index"], "high") if ar else None
        st_valid = bool(st is not None and st["price"] <= bc["high"])
        utad = next((row for row in event_rows if st_valid and row["index"] > st["index"] and row["utad"]), None)
        sow = next((row for row in event_rows if utad is not None and row["index"] > utad["index"] and row["sow"]), None)
        lpsy = _first_pivot_after(pivots, sow["index"], "high") if sow else None
        lpsy_held = bool(lpsy is not None and sow is not None and lpsy["price"] <= sow["prior_range_low"] and bars[-1]["close"] <= sow["prior_range_low"])
        phase_d = "E" if lpsy_held else "D" if sow is not None else "C" if utad is not None else "B" if st_valid else "A" if ar is not None else None
        dist_events: list[dict[str, Any]] = []
        add_event(dist_events, "BC", bc, "high")
        add_event(dist_events, "AR", ar, "price")
        add_event(dist_events, "ST", st if st_valid else None, "price")
        add_event(dist_events, "UTAD", utad, "high")
        add_event(dist_events, "SOW", sow, "close")
        add_event(dist_events, "LPSY", lpsy if lpsy_held else None, "price")
        range_high = utad["high"] if utad is not None else bc["high"]
        range_low = ar["price"] if ar is not None else bc["prior_range_low"]
        width = range_high - range_low if range_high is not None and range_low is not None else None
        breakout = sow["prior_range_low"] if sow is not None else None
        distribution = {
            "phase": phase_d, "events": dist_events, "range_high": range_high, "range_low": range_low,
            "invalidation_level": range_high, "breakout_level": breakout,
            "objective_price": breakout - width if breakout is not None and width is not None and width > 0 else None,
            "last_event_index": lpsy["index"] if lpsy_held else sow["index"] if sow is not None else utad["index"] if utad is not None else st["index"] if st_valid else ar["index"] if ar is not None else bc["index"],
        }
        all_events.extend(dist_events)

    if accumulation["phase"] is None and distribution["phase"] is None:
        phase, interpretation, selected = "unresolved", "trading_range_or_transition_unresolved", accumulation
    elif accumulation["phase"] is not None and distribution["phase"] is not None:
        phase, interpretation = "competing", "competing_accumulation_and_distribution_sequences"
        selected = accumulation if accumulation.get("last_event_index", -1) >= distribution.get("last_event_index", -1) else distribution
    elif accumulation["phase"] is not None:
        phase, interpretation, selected = f"accumulation_{accumulation['phase']}", "accumulation_or_markup_hypothesis", accumulation
    else:
        phase, interpretation, selected = f"distribution_{distribution['phase']}", "distribution_or_markdown_hypothesis", distribution

    range_high, range_low = selected.get("range_high"), selected.get("range_low")
    if range_high is None or range_low is None:
        prior = bars[-_WYCKOFF_RANGE - 1:-1]
        range_high = max((bar["high"] for bar in prior), default=None)
        range_low = min((bar["low"] for bar in prior), default=None)
    range_width = range_high - range_low if range_high is not None and range_low is not None else None
    # Structural range boundaries remain available as provisional invalidation
    # references even before a climax sequence is established.
    if accumulation.get("invalidation_level") is None:
        accumulation["invalidation_level"] = range_low
    if distribution.get("invalidation_level") is None:
        distribution["invalidation_level"] = range_high

    # A sequence can remain historically identifiable after it has failed.
    # Its current hypothesis status must nevertheless be invalidated by a
    # completed close through the actual structural boundary.
    latest_close = bars[-1]["close"] if bars else None
    accumulation["invalidated"] = bool(
        accumulation["phase"] is not None
        and latest_close is not None
        and accumulation.get("invalidation_level") is not None
        and latest_close < accumulation["invalidation_level"]
    )
    distribution["invalidated"] = bool(
        distribution["phase"] is not None
        and latest_close is not None
        and distribution.get("invalidation_level") is not None
        and latest_close > distribution["invalidation_level"]
    )
    accumulation["current_status"] = (
        "invalidated" if accumulation["invalidated"] else
        "supported_for_brain_examination" if accumulation["phase"] is not None else
        "not_supported_by_current_observations"
    )
    distribution["current_status"] = (
        "invalidated" if distribution["invalidated"] else
        "supported_for_brain_examination" if distribution["phase"] is not None else
        "not_supported_by_current_observations"
    )
    selected_events = {item["event"] for item in selected.get("events", [])}
    expected = {"SC", "AR", "ST", "Spring", "SOS", "LPS_BU"} if selected is accumulation else {"BC", "AR", "ST", "UTAD", "SOW", "LPSY"}
    completeness = len(selected_events.intersection(expected)) / len(expected) if expected else None
    historical: list[float] = []
    for end in range(_WYCKOFF_RANGE, max(_WYCKOFF_RANGE, len(event_rows) - 1)):
        sample = event_rows[max(0, end - _WYCKOFF_RANGE):end]
        labels = set()
        for row in sample:
            if row["selling_climax"]: labels.add("SC")
            if row["buying_climax"]: labels.add("BC")
            if row["spring"]: labels.add("Spring")
            if row["utad"]: labels.add("UTAD")
            if row["sos"]: labels.add("SOS")
            if row["sow"]: labels.add("SOW")
        historical.append(len(labels) / 6.0)
    phase_strength = percentile_rank(completeness, historical) if completeness is not None and historical else None
    if not sc and not bc:
        unknowns.append("a completed-volume selling or buying climax candidate to establish Phase A")
    if accumulation["phase"] in {"A", "B"}:
        unknowns.append("a confirmed Spring followed by SOS to establish later accumulation phases")
    if distribution["phase"] in {"A", "B"}:
        unknowns.append("a confirmed UTAD followed by SOW to establish later distribution phases")
    if effort.get("volume_basis") in {"tick_volume_proxy", "mixed_volume_sources", "unavailable"}:
        unknowns.append("true executed-volume confirmation; selected broker volume is not necessarily exchange trade volume")
    unknowns.append("aggressor-side trade volume and campaign point-of-control migration require trade-level/volume-at-price data")
    acc_support = [item["event"] for item in accumulation["events"]]
    dist_support = [item["event"] for item in distribution["events"]]
    return {
        "preceding_range": {"high": range_high, "low": range_low, "width": range_width, "bars": len(bars[-_WYCKOFF_RANGE - 1:-1])},
        "event_sequence": sorted(all_events, key=lambda item: item["index"]),
        "event_candidate_history": list(event_rows),
        "accumulation_or_markup": accumulation,
        "distribution_or_markdown": distribution,
        "competing_interpretations": {
            "accumulation_or_markup": {
                "supporting_observations": acc_support,
                "invalidation_level": accumulation.get("invalidation_level"),
                "status": accumulation["current_status"],
                "invalidated_by_completed_close": accumulation["invalidated"],
            },
            "distribution_or_markdown": {
                "supporting_observations": dist_support,
                "invalidation_level": distribution.get("invalidation_level"),
                "status": distribution["current_status"],
                "invalidated_by_completed_close": distribution["invalidated"],
            },
        },
        "current_auction_events": {
            "spring_candidate": bool(event_rows and event_rows[-1]["spring"]),
            "upthrust_candidate": bool(event_rows and event_rows[-1]["utad"]),
            "sign_of_strength_close": bool(event_rows and event_rows[-1]["sos"]),
            "sign_of_weakness_close": bool(event_rows and event_rows[-1]["sow"]),
            "event_confirmation": "completed OHLCV and confirmed pivot sequence; not confirmed order flow",
        },
        "interpretation_state": interpretation, "phase": phase,
        "selected_hypothesis_invalidated": bool(selected.get("invalidated", False)),
        "phase_sequence_completeness": completeness,
        "phase_sequence_strength_percentile": phase_strength,
        "phase_confidence_semantics": "own-history percentile rank of event-sequence completeness; not a calibrated probability",
        "phase_not_overclaimed": True, "volume_is_not_aggressor_flow": True,
        "unconfirmed_components": [
            "preliminary support/supply and climax labels remain candidates until later events confirm them",
            "secondary tests require later confirmed pivots",
            "campaign point-of-control migration requires verified volume-at-price history",
        ],
        "directional_decision_emitted": False, "entry_authorization_emitted": False,
    }, unknowns


def _structural_implication(
    direction: str, *, statement: str, strength: float | None, activated: bool,
    activation_reason: str, requires: tuple[str, ...], conflicts_with: tuple[str, ...] = (),
    invalidation_price: float | None = None, invalidation_reason: str = "",
    objective_price: float | None = None, milestones: tuple[float, ...] = (),
    management: dict[str, Any] | None = None,
) -> Implication:
    return Implication(
        direction=direction, statement=statement, strength=strength, activated=activated,
        activation_reason=activation_reason, invalidation_price=invalidation_price,
        invalidation_reason=invalidation_reason, objective_price=objective_price,
        milestones=milestones, requires=requires, conflicts_with=conflicts_with,
        management=management or {},
    )


def _wyckoff_implications(
    bars: list[dict[str, Any]], facts: dict[str, Any], unknowns: list[str],
) -> tuple[Implication, ...]:
    accumulation, distribution = facts["accumulation_or_markup"], facts["distribution_or_markdown"]
    long_events = {item["event"] for item in accumulation["events"]}
    short_events = {item["event"] for item in distribution["events"]}
    long_supported, short_supported = accumulation["phase"] is not None, distribution["phase"] is not None
    long_invalidated = accumulation.get("invalidated") is True
    short_invalidated = distribution.get("invalidated") is True
    requirements = tuple(item for item in unknowns if any(
        token in item.lower() for token in ("volume", "sequence", "aggressor", "point-of-control")
    ))
    if long_invalidated:
        requirements += ("a new completed accumulation sequence after the prior structural invalidation",)
    if short_invalidated:
        requirements += ("a new completed distribution sequence after the prior structural invalidation",)
    strength = facts.get("phase_sequence_strength_percentile")
    close = bars[-1]["close"] if bars else None
    long_milestones, short_milestones = (), ()
    if close is not None and accumulation.get("objective_price") is not None:
        level = nearest_structural_level(bars, "LONG", close)
        if level is not None and level < accumulation["objective_price"]:
            long_milestones = (level,)
    if close is not None and distribution.get("objective_price") is not None:
        level = nearest_structural_level(bars, "SHORT", close)
        if level is not None and level > distribution["objective_price"]:
            short_milestones = (level,)
    return (
        _structural_implication(
            LONG, statement="Accumulation/markup is supported only to the extent the completed SC–AR–ST–Spring–SOS–LPS sequence is present.",
            strength=strength if long_supported and not long_invalidated else None,
            activated=bool(long_supported and not long_invalidated and long_events.intersection({"Spring", "SOS", "LPS_BU"})),
            activation_reason=(
                "A completed close breached the accumulation invalidation level; this historical sequence is cancelled."
                if long_invalidated else
                "A completed Spring, SOS or LPS/backup follows the accumulation sequence."
                if long_supported and long_events.intersection({"Spring", "SOS", "LPS_BU"}) else
                "The accumulation sequence has not reached a completed current trigger."
            ),
            requires=requirements,
            conflicts_with=("distribution/markdown sequence also present",) if short_supported else (),
            invalidation_price=accumulation.get("invalidation_level"),
            invalidation_reason="A completed close below the spring/climax structural low invalidates the accumulation hypothesis.",
            objective_price=None if long_invalidated else accumulation.get("objective_price"),
            milestones=() if long_invalidated else long_milestones,
            management={"trail_reference": "confirmed higher lows after SOS/LPS", "reassess_on": "completed close below structural invalidation or a newer distribution sequence"},
        ),
        _structural_implication(
            SHORT, statement="Distribution/markdown is supported only to the extent the completed BC–AR–ST–UTAD–SOW–LPSY sequence is present.",
            strength=strength if short_supported and not short_invalidated else None,
            activated=bool(short_supported and not short_invalidated and short_events.intersection({"UTAD", "SOW", "LPSY"})),
            activation_reason=(
                "A completed close breached the distribution invalidation level; this historical sequence is cancelled."
                if short_invalidated else
                "A completed UTAD, SOW or LPSY follows the distribution sequence."
                if short_supported and short_events.intersection({"UTAD", "SOW", "LPSY"}) else
                "The distribution sequence has not reached a completed current trigger."
            ),
            requires=requirements,
            conflicts_with=("accumulation/markup sequence also present",) if long_supported else (),
            invalidation_price=distribution.get("invalidation_level"),
            invalidation_reason="A completed close above the UTAD/climax structural high invalidates the distribution hypothesis.",
            objective_price=None if short_invalidated else distribution.get("objective_price"),
            milestones=() if short_invalidated else short_milestones,
            management={"trail_reference": "confirmed lower highs after SOW/LPSY", "reassess_on": "completed close above structural invalidation or a newer accumulation sequence"},
        ),
        _structural_implication(
            FLAT, statement="The phase remains unresolved or competing until completed auction events establish a sequential interpretation.",
            strength=None, activated=False, activation_reason="Neutral evidence remains available whenever phase sequences are incomplete or competing.",
            requires=tuple(dict.fromkeys(unknowns)),
            management={"position_state": "reassess each completed bar and preserve opposing phase hypotheses"},
        ),
    )


def _effort_result_implications(
    bars: list[dict[str, Any]], facts: dict[str, Any], unknowns: list[str],
) -> tuple[Implication, ...]:
    change, location = facts.get("close_to_close_change"), facts.get("close_location_in_bar")
    gap = facts.get("effort_result_rank_gap")
    long_support: list[str] = []
    short_support: list[str] = []
    if change is not None and change > 0: long_support.append("latest completed close progressed upward")
    elif change is not None and change < 0: short_support.append("latest completed close progressed downward")
    if gap is not None and gap > 0 and location is not None:
        if location > 0.5: long_support.append("volume-effort rank exceeds true-range-result rank and close is in the upper half of the bar")
        elif location < 0.5: short_support.append("volume-effort rank exceeds true-range-result rank and close is in the lower half of the bar")
    elif gap is not None and gap < 0:
        if change is not None and change > 0: long_support.append("price-range result rank exceeds effort rank while price progresses upward")
        elif change is not None and change < 0: short_support.append("price-range result rank exceeds effort rank while price progresses downward")
    pivots = swing_points(bars)
    low = next((item["price"] for item in reversed(pivots) if item["kind"] == "low"), None)
    high = next((item["price"] for item in reversed(pivots) if item["kind"] == "high"), None)
    strength = facts.get("effort_result_strength_percentile")
    requirements = tuple(dict.fromkeys(unknowns + [
        "verified aggressor-side volume or trade-level volume-at-price before calling the observation confirmed absorption",
    ]))
    close = bars[-1]["close"] if bars else None
    return (
        _structural_implication(
            LONG, statement="Effort/result is compatible with upward auction progress; bar volume does not prove buyer aggression or absorption.",
            strength=strength if long_support else None, activated=bool(long_support),
            activation_reason="Completed upward progress and own-history effort/result ranks are present." if long_support else "No upward effort/result relationship is currently supported.",
            requires=requirements, conflicts_with=tuple(short_support), invalidation_price=low,
            invalidation_reason="A completed close below the latest confirmed structural swing low weakens the upward reading.",
            objective_price=nearest_structural_level(bars, "LONG", close) if close is not None else None,
            management={"trail_reference": "confirmed higher lows", "reassess_on": "next completed effort/result observation"},
        ),
        _structural_implication(
            SHORT, statement="Effort/result is compatible with downward auction progress; bar volume does not prove seller aggression or absorption.",
            strength=strength if short_support else None, activated=bool(short_support),
            activation_reason="Completed downward progress and own-history effort/result ranks are present." if short_support else "No downward effort/result relationship is currently supported.",
            requires=requirements, conflicts_with=tuple(long_support), invalidation_price=high,
            invalidation_reason="A completed close above the latest confirmed structural swing high weakens the downward reading.",
            objective_price=nearest_structural_level(bars, "SHORT", close) if close is not None else None,
            management={"trail_reference": "confirmed lower highs", "reassess_on": "next completed effort/result observation"},
        ),
        _structural_implication(
            FLAT, statement="Effort/result ranks are descriptive; missing volume semantics or conflicting price evidence keeps neutrality available.",
            strength=None, activated=False, activation_reason="No one-sided conclusion is established by effort/result alone.",
            requires=requirements, management={"position_state": "do not present volume proxy as direct order flow"},
        ),
    )


def _volume_indicator_implications(
    bars: list[dict[str, Any]], facts: dict[str, Any], unknowns: list[str],
) -> tuple[Implication, ...]:
    long_ratio, short_ratio = facts.get("indicator_positive_ratio"), facts.get("indicator_negative_ratio")
    history, coherence = facts.get("indicator_alignment_history", []), facts.get("indicator_alignment_score")
    strength = percentile_rank(coherence, history) if coherence is not None and history else None
    divergence = facts.get("confirmed_price_indicator_divergence", {})
    bullish_divergence = any(isinstance(divergence.get(name, {}).get("bullish"), dict) and divergence[name]["bullish"].get("observed") is True for name in ("obv", "mfi"))
    bearish_divergence = any(isinstance(divergence.get(name, {}).get("bearish"), dict) and divergence[name]["bearish"].get("observed") is True for name in ("obv", "mfi"))
    long_support = ["positive indicator ratio exceeds negative indicator ratio"] if long_ratio is not None and short_ratio is not None and long_ratio > short_ratio else []
    short_support = ["negative indicator ratio exceeds positive indicator ratio"] if long_ratio is not None and short_ratio is not None and short_ratio > long_ratio else []
    if bullish_divergence: long_support.append("confirmed bullish price/indicator divergence is present")
    if bearish_divergence: short_support.append("confirmed bearish price/indicator divergence is present")
    pivots = swing_points(bars)
    low = next((item["price"] for item in reversed(pivots) if item["kind"] == "low"), None)
    high = next((item["price"] for item in reversed(pivots) if item["kind"] == "high"), None)
    requirements = tuple(dict.fromkeys(unknowns + [
        "aggressor-side executed volume before interpreting bar-volume indicators as bid/ask delta",
    ]))
    close = bars[-1]["close"] if bars else None
    return (
        _structural_implication(
            LONG, statement="Volume-derived indicators provide upward confirmation or divergence context on the stated volume basis.",
            strength=strength if long_support else None, activated=bool(long_support),
            activation_reason="The indicator set has more positive than negative components or a confirmed bullish divergence." if long_support else "No upward indicator relationship is currently supported.",
            requires=requirements, conflicts_with=tuple(short_support), invalidation_price=low,
            invalidation_reason="A completed close below the latest confirmed structural low weakens the upward indicator reading.",
            objective_price=nearest_structural_level(bars, "LONG", close) if close is not None else None,
            management={"trail_reference": "confirmed higher lows; reassess when indicator/price divergence resolves"},
        ),
        _structural_implication(
            SHORT, statement="Volume-derived indicators provide downward confirmation or divergence context on the stated volume basis.",
            strength=strength if short_support else None, activated=bool(short_support),
            activation_reason="The indicator set has more negative than positive components or a confirmed bearish divergence." if short_support else "No downward indicator relationship is currently supported.",
            requires=requirements, conflicts_with=tuple(long_support), invalidation_price=high,
            invalidation_reason="A completed close above the latest confirmed structural high weakens the downward indicator reading.",
            objective_price=nearest_structural_level(bars, "SHORT", close) if close is not None else None,
            management={"trail_reference": "confirmed lower highs; reassess when indicator/price divergence resolves"},
        ),
        _structural_implication(
            FLAT, statement="Indicator readings can conflict or be unavailable; tick-volume-derived measures are not true aggressor-side delta.",
            strength=None, activated=False, activation_reason="The Brain retains neutrality when the indicator set is mixed or incomplete.",
            requires=requirements, management={"position_state": "do not replace verified aggressor delta with OHLCV indicators"},
        ),
    )


class WyckoffVolumeReadings:
    key = "wyckoff_volume_readings"
    family = "auction_volume"
    covers_methods = ("wyckoff", "wyckoff_effort_result", "volume_indicators")
    covers_lenses = ()

    def read(self, symbol: str, bars_by_tf: dict[str, list[dict]]) -> list[ConceptReading]:
        reference, reference_tf = reference_price(bars_by_tf)
        readings: list[ConceptReading] = []
        for timeframe in sorted(bars_by_tf, key=lambda item: item):
            bars = clean_bars(bars_by_tf.get(timeframe))
            if not bars:
                continue
            epoch = last_source_epoch(bars)
            base_unknowns = [] if epoch is not None else ["source-verified timestamp for the latest completed bar"]
            effort, effort_unknowns = _effort_result_facts(bars)
            volume, volume_unknowns = _volume_indicator_facts(bars)
            wyckoff, wyckoff_unknowns = _wyckoff_facts(bars, effort)
            effort_applicable = (
                effort["volume_percentile_vs_prior_observations"] is not None
                and effort["range_percentile_vs_prior_observations"] is not None
            )
            readings.append(ConceptReading(
                concept="wyckoff_effort_result", timeframe=timeframe, domain=EvidenceDomain.VOLUME,
                applicable=effort_applicable,
                applicability_reason="Own-history volume-effort and true-range-result comparison is available." if effort_applicable else "The complete own-history comparison window is unavailable.",
                observation=f"{symbol} {timeframe}: effort/result is {effort['effort_result_state']} on {effort['volume_basis']} volume; absorption and aggressor direction remain unconfirmed.",
                implications=_effort_result_implications(bars, effort, effort_unknowns),
                facts=effort,source_bar_epoch=epoch,
 source_timestamp_verified=epoch is not None, reference_close=reference, reference_timeframe=reference_tf,
                unknowns=tuple(dict.fromkeys(base_unknowns + effort_unknowns)),
                data_basis="completed_ohlcv_with_selected_volume_source",
                independence_group=f"wyckoff_effort_result:{symbol}:{timeframe}",
            ))
            volume_applicable = volume.get("obv") is not None and volume.get("mfi_14") is not None
            readings.append(ConceptReading(
                concept="volume_indicators", timeframe=timeframe, domain=EvidenceDomain.VOLUME,
                applicable=volume_applicable,
                applicability_reason="Contiguous volume history supports OBV, MFI, CMF and A/D." if volume_applicable else "Consecutive valid volume history for the core indicators is unavailable.",
                observation=f"{symbol} {timeframe}: volume indicators use {volume['volume_basis']} basis and are not direct bid/ask aggressor flow.",
                implications=_volume_indicator_implications(bars, volume, volume_unknowns),
                facts=volume,source_bar_epoch=epoch,
 source_timestamp_verified=epoch is not None, reference_close=reference, reference_timeframe=reference_tf,
                unknowns=tuple(dict.fromkeys(base_unknowns + volume_unknowns)),
                data_basis="completed_ohlcv_with_selected_volume_source",
                independence_group=f"volume_indicators:{symbol}:{timeframe}",
            ))
            applicable = len(bars) >= _WYCKOFF_RANGE + 2 and effort.get("volume_basis") != "unavailable"
            readings.append(ConceptReading(
                concept="wyckoff", timeframe=timeframe, domain=EvidenceDomain.STRUCTURE,
                applicable=applicable,
                applicability_reason="Completed OHLCV supports sequential climax, reaction, test, Spring/UTAD and SOS/SOW evaluation." if applicable else "A sufficiently long completed-bar range and selected-volume history is unavailable.",
                observation=f"{symbol} {timeframe}: Wyckoff sequence is {wyckoff['phase']} ({wyckoff['interpretation_state']}); both accumulation and distribution remain challengeable hypotheses.",
                implications=_wyckoff_implications(bars, wyckoff, wyckoff_unknowns),
                facts=wyckoff,source_bar_epoch=epoch,
 source_timestamp_verified=epoch is not None, reference_close=reference, reference_timeframe=reference_tf,
                unknowns=tuple(dict.fromkeys(base_unknowns + wyckoff_unknowns)),
                data_basis="completed_ohlcv_and_selected_volume_source",
                independence_group=f"wyckoff:{symbol}:{timeframe}",
            ))
        return readings
