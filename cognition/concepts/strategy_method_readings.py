"""Descriptive trend, breakout and statistical-reversion readings.

These are analytical readers, not executable strategies. They expose completed-bar
facts, structural context, distribution diagnostics, uncertainty and invalidation
conditions. They do not choose trades, produce directional votes, or authorize
entries. All observations share their originating OHLC independence group.
"""
from __future__ import annotations

import math
from statistics import mean, median, pstdev
from typing import Any

from cognition.concepts.auction_utils import clean_bars, last_source_epoch, volume_of
from cognition.concepts.base import TIMEFRAME_SECONDS, ConceptReading, percentile_rank, reference_price
from cognition.concepts.market_structure import fractal_swings, trend_state
from cognition.contracts import EvidenceDomain


# These are calculation-window definitions for the reported statistics, not
# entry thresholds or configurable trading parameters.
_TREND_EFFICIENCY_BARS = 20
_BREAKOUT_VOLUME_BASELINE_BARS = 20
_REVERSION_LOOKBACK_BARS = 30
_REVERSION_OUTCOME_HORIZON_BARS = 30

# Asymptotic 5% MacKinnon-style reference values for an ADF(0) regression:
# constant only, and constant plus deterministic linear trend. These are
# statistical-test references, not market-entry levels or calibrated p-values.
_ADF_CONSTANT_5PCT = -2.86
_ADF_TREND_5PCT = -3.41


def _efficiency(closes: list[float], end: int, period: int) -> float | None:
    start = end - period
    if start < 0:
        return None
    sample = closes[start:end + 1]
    travel = sum(abs(right - left) for left, right in zip(sample, sample[1:]))
    return abs(sample[-1] - sample[0]) / travel if travel > 0 else 0.0


def _acf1(values: list[float]) -> float | None:
    if len(values) < 3:
        return None
    centre = mean(values)
    left = [value - centre for value in values[:-1]]
    right = [value - centre for value in values[1:]]
    denominator = math.sqrt(sum(value * value for value in left) * sum(value * value for value in right))
    return sum(a * b for a, b in zip(left, right)) / denominator if denominator > 0 else None


def _pivot_sequences(bars: list[dict]) -> list[tuple[int, str, float]]:
    """Return confirmed internal swings; the final unconfirmed bar is excluded."""
    return fractal_swings(bars)


def _historical_pullback_depths(
    pivots: list[tuple[int, str, float]], direction: str, before_index: int,
) -> list[float]:
    depths: list[float] = []
    for index, (pivot_index, kind, price) in enumerate(pivots):
        if pivot_index >= before_index or kind != ("H" if direction == "up" else "L"):
            continue
        previous_opposites = [
            item for item in pivots[:index]
            if item[1] == ("L" if direction == "up" else "H")
        ]
        next_opposites = [
            item for item in pivots[index + 1:]
            if item[1] == ("L" if direction == "up" else "H")
        ]
        if not previous_opposites or not next_opposites:
            continue
        origin = previous_opposites[-1][2]
        retracement = next_opposites[0][2]
        impulse = price - origin if direction == "up" else origin - price
        depth = price - retracement if direction == "up" else retracement - price
        if impulse > 0 and depth >= 0:
            depths.append(depth / impulse)
    return depths


def _latest_pullback(
    pivots: list[tuple[int, str, float]], direction: str, current_close: float,
) -> dict[str, Any]:
    impulse_kind = "H" if direction == "up" else "L"
    origin_kind = "L" if direction == "up" else "H"
    impulses = [(idx, price) for idx, kind, price in pivots if kind == impulse_kind]
    if not impulses:
        return {"depth": None, "origin": None, "extreme": None, "anchor": None}
    impulse_index, impulse_price = impulses[-1]
    origins = [
        (idx, price) for idx, kind, price in pivots
        if kind == origin_kind and idx < impulse_index
    ]
    if not origins:
        return {"depth": None, "origin": None, "extreme": None, "anchor": None}
    origin_index, origin_price = origins[-1]
    later_retracements = [
        (idx, price) for idx, kind, price in pivots
        if kind == origin_kind and idx > impulse_index
    ]
    if later_retracements:
        anchor, extreme = "confirmed_retracement_swing", later_retracements[-1][1]
    else:
        anchor, extreme = "latest_completed_close", current_close
    impulse_size = impulse_price - origin_price if direction == "up" else origin_price - impulse_price
    depth = (
        (impulse_price - extreme) if direction == "up" else (extreme - impulse_price)
    ) / impulse_size if impulse_size > 0 else None
    return {
        "depth": depth,
        "origin": origin_price,
        "extreme": extreme,
        "anchor": anchor,
        "origin_pivot_index": origin_index,
        "impulse_pivot_index": impulse_index,
        "impulse_size": impulse_size,
    }


def _trend_reading(
    timeframe: str, bars: list[dict], reference: float | None, reference_tf: str,
) -> ConceptReading:
    closes = [bar["close"] for bar in bars]
    pivots = _pivot_sequences(bars)
    state, levels = trend_state(pivots)
    epoch = last_source_epoch(bars)
    unknowns: list[str] = []
    if state == "undetermined":
        unknowns.append("two confirmed swing highs and two confirmed swing lows are required to establish the swing sequence")
    efficiencies = [
        value for end in range(_TREND_EFFICIENCY_BARS, len(closes))
        if (value := _efficiency(closes, end, _TREND_EFFICIENCY_BARS)) is not None
    ]
    current_efficiency = efficiencies[-1] if efficiencies else None
    efficiency_percentile = percentile_rank(current_efficiency, efficiencies[:-1]) if (
        current_efficiency is not None and len(efficiencies) > 1
    ) else None
    if efficiency_percentile is None:
        unknowns.append("prior non-overlapping or rolling 20-bar path-efficiency observations for an own-history percentile")
    pullback = None
    pullback_percentile = None
    invalidation = None
    close_beyond_invalidation = None
    continuation_observation = "not_evaluable"
    if state in {"up", "down"}:
        pullback = _latest_pullback(pivots, state, closes[-1])
        historical_depths = _historical_pullback_depths(
            pivots, state, pullback.get("impulse_pivot_index", len(bars)),
        )
        if pullback["depth"] is not None and historical_depths:
            pullback_percentile = percentile_rank(pullback["depth"], historical_depths)
        else:
            unknowns.append("completed prior pullback-depth observations for an instrument-own-history comparison")
        invalidation = levels.get("last_low") if state == "up" else levels.get("last_high")
        close_beyond_invalidation = (
            closes[-1] < invalidation if state == "up" else closes[-1] > invalidation
        ) if invalidation is not None else None
        if state == "up":
            continuation_observation = (
                "latest_close_above_previous_bar_high"
                if closes[-1] > bars[-2]["high"] else
                "latest_close_did_not_exceed_previous_bar_high"
            )
        else:
            continuation_observation = (
                "latest_close_below_previous_bar_low"
                if closes[-1] < bars[-2]["low"] else
                "latest_close_did_not_break_previous_bar_low"
            )
    if epoch is None:
        unknowns.append("source-verified timestamp for the latest completed bar")
    facts = {
        "trend_state_from_confirmed_swings": state,
        "confirmed_internal_swings": [
            {"index": index, "kind": kind, "price": price}
            for index, kind, price in pivots
        ],
        "last_swing_levels": dict(levels),
        "pullback": pullback,
        "pullback_depth_own_history_percentile": pullback_percentile,
        "historical_pullback_sample_count": len(
            _historical_pullback_depths(
                pivots, state, pullback.get("impulse_pivot_index", len(bars))
            )
        ) if state in {"up", "down"} and pullback else 0,
        "path_efficiency_bars": _TREND_EFFICIENCY_BARS,
        "path_efficiency": current_efficiency,
        "path_efficiency_own_history_percentile": efficiency_percentile,
        "path_efficiency_sample_count": max(0, len(efficiencies) - 1),
        "continuation_price_observation": continuation_observation,
        "structural_invalidation_level": invalidation,
        "completed_close_beyond_invalidation": close_beyond_invalidation,
        "history_bar_count": len(bars),
        "directional_decision_emitted": False,
        "entry_authorization_emitted": False,
    }
    observation = (
        f"{timeframe} trend-following context: swing state={state}; "
        f"path efficiency={current_efficiency}; pullback depth={None if pullback is None else pullback.get('depth')}; "
        f"no trade direction or entry decision is emitted."
    )
    return ConceptReading(
        concept="trend_following",
        timeframe=timeframe,
        domain=EvidenceDomain.STRUCTURE,
        applicable=state in {"up", "down", "range_or_transition"},
        applicability_reason=(
            "Confirmed swing sequences are available for describing trend, pullback and structural failure."
            if state != "undetermined" else
            "The history does not contain enough confirmed swing points to establish a trend state."
        ),
        observation=observation,
        implications=(),
        facts=facts,
        source_bar_epoch=epoch,
        reference_close=reference,
        reference_timeframe=reference_tf,
        unknowns=tuple(dict.fromkeys(unknowns)),
    )


def _breakout_side(
    bars: list[dict], pivots: list[tuple[int, str, float]], side: str,
) -> dict[str, Any]:
    kind = "H" if side == "above" else "L"
    candidates = [pivot for pivot in pivots if pivot[1] == kind and pivot[0] < len(bars) - 1]
    if not candidates:
        return {"status": "no_confirmed_reference", "boundary": None, "pivot_index": None}
    pivot_index, _, boundary = candidates[-1]
    crossings = []
    for index in range(max(1, pivot_index + 1), len(bars)):
        previous, current = bars[index - 1]["close"], bars[index]["close"]
        crossed = current > boundary and previous <= boundary if side == "above" else (
            current < boundary and previous >= boundary
        )
        if crossed:
            crossings.append(index)
    current_close = bars[-1]["close"]
    outside = current_close > boundary if side == "above" else current_close < boundary
    if not crossings:
        status = (
            "outside_reference_without_observed_crossing"
            if outside else "no_breakout_after_confirmed_reference"
        )
        return {
            "status": status, "boundary": boundary, "pivot_index": pivot_index,
            "crossing_index": None, "closes_beyond_boundary": 0,
            "retest_held": False, "close_distance": current_close - boundary,
            "completed_close_return_invalidates": True,
        }
    crossing_index = crossings[-1]
    after = bars[crossing_index + 1:]
    if side == "above":
        retest_held = any(bar["low"] <= boundary and bar["close"] > boundary for bar in after)
        beyond = lambda value: value > boundary
    else:
        retest_held = any(bar["high"] >= boundary and bar["close"] < boundary for bar in after)
        beyond = lambda value: value < boundary
    consecutive = 0
    for bar in reversed(bars[crossing_index:]):
        if beyond(bar["close"]):
            consecutive += 1
        else:
            break
    if not outside:
        status = "failed_return_inside"
    elif retest_held:
        status = "retested_and_held"
    elif crossing_index == len(bars) - 1:
        status = "first_completed_close_beyond"
    else:
        status = "holding_beyond_without_observed_retest"
    return {
        "status": status,
        "boundary": boundary,
        "pivot_index": pivot_index,
        "crossing_index": crossing_index,
        "bars_since_first_cross": len(bars) - crossing_index - 1,
        "consecutive_completed_closes_beyond": consecutive,
        "retest_held": retest_held,
        "close_distance": current_close - boundary,
        "completed_close_return_invalidates": True,
    }


def _breakout_reading(
    timeframe: str, bars: list[dict], reference: float | None, reference_tf: str,
) -> ConceptReading:
    pivots = _pivot_sequences(bars)
    current = bars[-1]
    prior_ranges = [bar["high"] - bar["low"] for bar in bars[:-1] if bar["high"] >= bar["low"]]
    current_range = current["high"] - current["low"]
    range_percentile = percentile_rank(current_range, prior_ranges)
    volume_values: list[float] = []
    volume_bases: set[str] = set()
    for bar in bars[-(_BREAKOUT_VOLUME_BASELINE_BARS + 1):]:
        volume, basis = volume_of(bar)
        if volume is not None and volume > 0:
            volume_values.append(volume)
            volume_bases.add(basis)
    current_volume, current_volume_basis = volume_of(current)
    baseline = volume_values[:-1] if volume_values and current_volume is not None else volume_values
    volume_median = median(baseline) if baseline else None
    volume_ratio = current_volume / volume_median if (
        current_volume is not None and current_volume > 0 and volume_median is not None and volume_median > 0
    ) else None
    unknowns: list[str] = []
    if not any(kind == "H" for _, kind, _ in pivots):
        unknowns.append("a confirmed swing high for an upside breakout reference")
    if not any(kind == "L" for _, kind, _ in pivots):
        unknowns.append("a confirmed swing low for a downside breakout reference")
    if range_percentile is None:
        unknowns.append("prior completed-bar ranges for an own-history volatility comparison")
    if volume_ratio is None:
        unknowns.append("positive volume on the current completed bar and a positive preceding volume baseline")
    if current.get("source_timestamp_verified") is not True:
        unknowns.append("source-verified timestamp for the current completed bar")
    above = _breakout_side(bars, pivots, "above")
    below = _breakout_side(bars, pivots, "below")
    facts = {
        "upside_boundary_observation": above,
        "downside_boundary_observation": below,
        "latest_completed_bar_range": current_range,
        "range_own_history_percentile": range_percentile,
        "volume": current_volume,
        "volume_basis": current_volume_basis,
        "volume_baseline_median": volume_median,
        "volume_ratio_to_baseline": volume_ratio,
        "volume_source_bases_in_window": sorted(volume_bases),
        "volume_is_tick_proxy": "tick" in current_volume_basis.lower(),
        "confirmed_pivot_count": len(pivots),
        "history_bar_count": len(bars),
        "acceptance_is_descriptive_not_thresholded": True,
        "directional_decision_emitted": False,
        "entry_authorization_emitted": False,
    }
    observation = (
        f"{timeframe} breakout context: upside boundary={above['status']}; "
        f"downside boundary={below['status']}; current range percentile={range_percentile}; "
        "breakout persistence is reported as completed-bar observations, not an entry signal."
    )
    applicable = bool(pivots)
    return ConceptReading(
        concept="momentum_breakout",
        timeframe=timeframe,
        domain=EvidenceDomain.STRUCTURE,
        applicable=applicable,
        applicability_reason=(
            "At least one confirmed swing boundary is available for completed-close breakout/failure analysis."
            if applicable else "No confirmed swing boundary exists in the supplied completed-bar history."
        ),
        observation=observation,
        implications=(),
        facts=facts,
        source_bar_epoch=last_source_epoch(bars),
        reference_close=reference,
        reference_timeframe=reference_tf,
        unknowns=tuple(dict.fromkeys(unknowns)),
    )


def _rolling_zscores(closes: list[float]) -> list[dict[str, float | int]]:
    rows: list[dict[str, float | int]] = []
    for index in range(_REVERSION_LOOKBACK_BARS, len(closes)):
        baseline = closes[index - _REVERSION_LOOKBACK_BARS:index]
        centre = mean(baseline)
        sigma = pstdev(baseline)
        if sigma <= 0:
            continue
        rows.append({
            "index": index,
            "zscore": (closes[index] - centre) / sigma,
            "mean": centre,
            "std": sigma,
        })
    return rows


def _solve_linear_system(matrix: list[list[float]], vector: list[float]) -> list[float] | None:
    size = len(vector)
    augmented = [list(matrix[row]) + [vector[row]] for row in range(size)]
    for column in range(size):
        pivot = max(range(column, size), key=lambda row: abs(augmented[row][column]))
        if abs(augmented[pivot][column]) <= 1e-14:
            return None
        augmented[column], augmented[pivot] = augmented[pivot], augmented[column]
        divisor = augmented[column][column]
        augmented[column] = [value / divisor for value in augmented[column]]
        for row in range(size):
            if row == column:
                continue
            factor = augmented[row][column]
            augmented[row] = [
                value - factor * pivot_value
                for value, pivot_value in zip(augmented[row], augmented[column])
            ]
    return [augmented[row][-1] for row in range(size)]


def _adf_zero_lag(log_prices: list[float], include_trend: bool) -> dict[str, Any]:
    """OLS ADF(0), with optional deterministic trend; reports a statistic only."""
    deltas = [right - left for left, right in zip(log_prices, log_prices[1:])]
    lagged_levels = log_prices[:-1]
    count = len(deltas)
    regressors = 3 if include_trend else 2
    if count <= regressors + 2:
        return {"status": "insufficient_sample", "sample_count": count, "lag_order": 0}
    time_values = [index - (count - 1) / 2.0 for index in range(count)]
    design = [
        ([1.0, time_values[index] / max(1, count), lagged_levels[index]]
         if include_trend else [1.0, lagged_levels[index]])
        for index in range(count)
    ]
    x_transpose_x = [
        [sum(row[i] * row[j] for row in design) for j in range(regressors)]
        for i in range(regressors)
    ]
    x_transpose_y = [
        sum(row[column] * delta for row, delta in zip(design, deltas))
        for column in range(regressors)
    ]
    beta = _solve_linear_system(x_transpose_x, x_transpose_y)
    if beta is None:
        return {"status": "singular_regression", "sample_count": count, "lag_order": 0}
    residuals = [
        target - sum(value * coefficient for value, coefficient in zip(row, beta))
        for row, target in zip(design, deltas)
    ]
    degrees = count - regressors
    residual_variance = sum(value * value for value in residuals) / degrees
    inverse_columns = []
    for column in range(regressors):
        unit = [1.0 if row == column else 0.0 for row in range(regressors)]
        solution = _solve_linear_system(x_transpose_x, unit)
        if solution is None:
            return {"status": "singular_covariance", "sample_count": count, "lag_order": 0}
        inverse_columns.append(solution)
    covariance = [
        [residual_variance * inverse_columns[column][row] for column in range(regressors)]
        for row in range(regressors)
    ]
    level_index = regressors - 1
    standard_error = math.sqrt(max(0.0, covariance[level_index][level_index]))
    statistic = beta[level_index] / standard_error if standard_error > 0 else None
    critical = _ADF_TREND_5PCT if include_trend else _ADF_CONSTANT_5PCT
    return {
        "status": "statistic_available" if statistic is not None else "zero_standard_error",
        "sample_count": count,
        "lag_order": 0,
        "deterministic_terms": "constant_and_linear_trend" if include_trend else "constant",
        "test_statistic": statistic,
        "asymptotic_5pct_reference": critical,
        "reject_unit_root_at_asymptotic_5pct": statistic < critical if statistic is not None else None,
        "residual_lag1_autocorrelation": _acf1(residuals),
        "limitations": (
            "ADF(0) omits lagged-difference regressors; residual serial dependence can distort inference. "
            "The reference value is asymptotic, not a finite-sample p-value."
        ),
    }


def _historical_reversion_episodes(
    zscores: list[dict[str, float | int]],
) -> dict[str, Any]:
    """Retrospective, non-overlapping 1-standard-deviation displacement episodes."""
    event_count = recentered_count = 0
    event_indices: list[int] = []
    index = 0
    while index < len(zscores):
        z = float(zscores[index]["zscore"])
        if abs(z) < 1.0:
            index += 1
            continue
        end = index + _REVERSION_OUTCOME_HORIZON_BARS
        if end >= len(zscores):
            break
        event_count += 1
        event_indices.append(int(zscores[index]["index"]))
        sign = 1.0 if z > 0 else -1.0
        recovery = next(
            (future for future in range(index + 1, end + 1)
             if sign * float(zscores[future]["zscore"]) <= 0.0),
            None,
        )
        if recovery is not None:
            recentered_count += 1
            index = recovery + 1
        else:
            index = end + 1
    return {
        "definition": "absolute_rolling_zscore_at_least_one_then_cross_back_through_mean",
        "outcome_horizon_bars": _REVERSION_OUTCOME_HORIZON_BARS,
        "completed_episode_count": event_count,
        "episodes_recentered_within_horizon": recentered_count,
        "observed_recenter_fraction": recentered_count / event_count if event_count else None,
        "event_start_indices": event_indices,
        "is_forward_probability": False,
    }


def _mean_reversion_reading(
    timeframe: str, bars: list[dict], reference: float | None, reference_tf: str,
) -> ConceptReading:
    closes = [bar["close"] for bar in bars]
    rows = _rolling_zscores(closes)
    unknowns: list[str] = []
    if not rows:
        unknowns.append(
            f"at least {_REVERSION_LOOKBACK_BARS + 1} positive-variance completed closes for rolling displacement"
        )
        facts: dict[str, Any] = {
            "rolling_zscore": None, "mean_distance": None,
            "history_bar_count": len(bars), "directional_decision_emitted": False,
        }
        return ConceptReading(
            concept="mean_reversion", timeframe=timeframe, domain=EvidenceDomain.HISTORICAL_ANALOGUE,
            applicable=False,
            applicability_reason="The supplied history cannot form a non-zero-variance rolling price distribution.",
            observation=f"{timeframe} mean-reversion context is unavailable because the rolling distribution is not estimable.",
            implications=(), facts=facts, source_bar_epoch=last_source_epoch(bars),
            reference_close=reference, reference_timeframe=reference_tf,
            unknowns=tuple(unknowns),
        )
    current = rows[-1]
    prior_zscores = [abs(float(row["zscore"])) for row in rows[:-1]]
    zscore = float(current["zscore"])
    z_percentile = percentile_rank(abs(zscore), prior_zscores) if prior_zscores else None
    if z_percentile is None:
        unknowns.append("prior rolling displacement observations for an own-history percentile")
    deviations = [float(row["zscore"]) for row in rows]
    dependence = _acf1(deviations[-min(60, len(deviations)):])
    half_life = math.log(0.5) / math.log(dependence) if dependence is not None and 0 < dependence < 1 else None
    if dependence is None:
        unknowns.append("enough rolling displacement observations to estimate lag-one dependence")
    elif half_life is None:
        unknowns.append("a positive, sub-unit lag-one coefficient for a monotone exponential half-life estimate")
    if all(value > 0 for value in closes):
        log_prices = [math.log(value) for value in closes]
        adf_constant = _adf_zero_lag(log_prices, include_trend=False)
        adf_trend = _adf_zero_lag(log_prices, include_trend=True)
    else:
        adf_constant = {"status": "non_positive_price_in_series", "lag_order": 0}
        adf_trend = {"status": "non_positive_price_in_series", "lag_order": 0}
        unknowns.append("strictly positive prices throughout the ordered sample for log-price unit-root diagnostics")
    if adf_constant.get("status") != "statistic_available" or adf_trend.get("status") != "statistic_available":
        unknowns.append("sufficient ordered positive closes for both constant-only and trend ADF diagnostics")
    else:
        unknowns.append(
            "ADF(0) omits lagged differences; inspect residual autocorrelation and structural breaks before interpreting unit-root diagnostics"
        )
    episodes = _historical_reversion_episodes(rows[:-1])
    if episodes["completed_episode_count"] == 0:
        unknowns.append("completed historical displacement episodes with a full 30-bar outcome window")
    first_half = deviations[-60:-30] if len(deviations) >= 60 else []
    second_half = deviations[-30:] if len(deviations) >= 60 else []
    distribution_comparison = {
        "sample_count_per_half": len(first_half),
        "first_half_mean": mean(first_half) if first_half else None,
        "second_half_mean": mean(second_half) if second_half else None,
        "mean_shift": mean(second_half) - mean(first_half) if first_half and second_half else None,
        "first_half_std": pstdev(first_half) if first_half else None,
        "second_half_std": pstdev(second_half) if second_half else None,
        "variance_ratio_recent_to_prior": (
            (pstdev(second_half) ** 2) / (pstdev(first_half) ** 2)
            if first_half and second_half and pstdev(first_half) > 0 else None
        ),
        "formal_stationarity_claim": False,
    }
    if not first_half or not second_half:
        unknowns.append("two complete 30-observation rolling-displacement samples for distribution-stability comparison")
    if bars[-1].get("source_timestamp_verified") is not True:
        unknowns.append("source-verified timestamp for the latest completed bar")
    facts = {
        "rolling_lookback_bars": _REVERSION_LOOKBACK_BARS,
        "rolling_mean": current["mean"],
        "rolling_standard_deviation": current["std"],
        "mean_distance": closes[-1] - float(current["mean"]),
        "rolling_zscore": zscore,
        "absolute_zscore_own_history_percentile": z_percentile,
        "prior_zscore_sample_count": len(prior_zscores),
        "displacement_lag1_autocorrelation": dependence,
        "exponential_half_life_bars": half_life,
        "half_life_estimator": "log_0.5_over_log_lag1_autocorrelation_when_0_lt_rho_lt_1",
        "unit_root_diagnostics": {
            "constant_only_adf0": adf_constant,
            "constant_plus_trend_adf0": adf_trend,
        },
        "rolling_distribution_stability": distribution_comparison,
        "historical_reversion_episodes": episodes,
        "trend_path_efficiency_20": _efficiency(closes, len(closes) - 1, _TREND_EFFICIENCY_BARS),
        "history_bar_count": len(bars),
        "historical_statistics_are_not_forward_probabilities": True,
        "invalidation_conditions": [
            "the rolling distribution changes materially across successive completed windows",
            "unit-root diagnostics do not reject non-stationarity under the stated model",
            "structural trend and volatility context dominates the historical mean-reversion sample",
            "historical recentering episodes fail to repeat in later completed observations",
        ],
        "directional_decision_emitted": False,
        "entry_authorization_emitted": False,
    }
    observation = (
        f"{timeframe} statistical-displacement context: rolling z-score={zscore:.4f}; "
        f"own-history absolute displacement percentile={z_percentile}; "
        f"lag-one dependence={dependence}; no reversal forecast or entry decision is emitted."
    )
    return ConceptReading(
        concept="mean_reversion",
        timeframe=timeframe,
        domain=EvidenceDomain.HISTORICAL_ANALOGUE,
        applicable=True,
        applicability_reason="A non-zero-variance rolling distribution and completed-bar statistical diagnostics are available.",
        observation=observation,
        implications=(),
        facts=facts,
        source_bar_epoch=last_source_epoch(bars),
        reference_close=reference,
        reference_timeframe=reference_tf,
        unknowns=tuple(dict.fromkeys(unknowns)),
    )


class StrategyMethodReadings:
    """Full descriptive readings for trend continuation, breakout and reversion."""

    key = "strategy_method_readings"
    family = "strategy_methods"
    covers_methods = ("trend_following", "momentum_breakout", "mean_reversion")
    covers_lenses: tuple[str, ...] = ()

    def read(self, symbol: str, bars_by_tf: dict[str, list[dict]]) -> list[ConceptReading]:
        reference, reference_tf = reference_price(bars_by_tf)
        readings: list[ConceptReading] = []
        for timeframe, raw in (bars_by_tf or {}).items():
            if timeframe not in TIMEFRAME_SECONDS:
                continue
            bars = clean_bars(raw)
            if not bars:
                continue
            readings.append(_trend_reading(timeframe, bars, reference, reference_tf))
            readings.append(_breakout_reading(timeframe, bars, reference, reference_tf))
            readings.append(_mean_reversion_reading(timeframe, bars, reference, reference_tf))
        return readings
