"""Completed-bar quantitative state readings: dependence, analogues and novelty.

These readers describe the instrument's own historical distribution. They do not
forecast a direction, authorize a trade, or turn a novelty score into a signal.
Historical analogues are reported with their realized forward outcomes, separately
from the current observation; the current bar is never used as its own outcome.
"""
from __future__ import annotations

import math
from statistics import median, pstdev
from typing import Any

from cognition.concepts.auction_utils import clean_bars, last_source_epoch
from cognition.concepts.base import (
    TIMEFRAME_SECONDS, ConceptReading, percentile_rank, reference_price,
)
from cognition.concepts.market_structure import trend_state, fractal_swings
from cognition.concepts.strategy_method_readings import _adf_zero_lag
from cognition.contracts import EvidenceDomain


# Twenty completed bars define the descriptive state vector. This is a feature
# definition, not a trading threshold or a minimum sample claim.
_STATE_WINDOW = 20
_ANALOGUE_HORIZONS = (5, 20)


def _returns(closes: list[float]) -> list[float]:
    return [
        math.log(right / left)
        for left, right in zip(closes, closes[1:])
        if left > 0.0 and right > 0.0
    ]


def _acf(values: list[float], lag: int) -> float | None:
    if lag <= 0 or len(values) <= lag + 1:
        return None
    centre = sum(values) / len(values)
    left = [value - centre for value in values[:-lag]]
    right = [value - centre for value in values[lag:]]
    denominator = math.sqrt(
        sum(value * value for value in left) * sum(value * value for value in right)
    )
    return sum(a * b for a, b in zip(left, right)) / denominator if denominator > 0.0 else None


def _state_vector(bars: list[dict[str, Any]], end: int) -> tuple[float, ...] | None:
    start = end - _STATE_WINDOW + 1
    if start < 0:
        return None
    window = bars[start:end + 1]
    closes = [float(item["close"]) for item in window]
    if any(value <= 0.0 for value in closes):
        return None
    returns = _returns(closes)
    if len(returns) < 2:
        return None
    ranges = [
        (float(item["high"]) - float(item["low"])) / float(item["close"])
        for item in window
    ]
    return (
        sum(returns[-5:]),
        pstdev(returns),
        median(ranges),
    )


def _robust_scales(vectors: list[tuple[float, ...]]) -> tuple[tuple[float, ...], tuple[float, ...]]:
    columns = list(zip(*vectors))
    centres = tuple(median(column) for column in columns)
    # MAD is a robust scale estimate. A zero-MAD dimension is omitted rather
    # than divided by an invented epsilon or arbitrary minimum scale.
    scales = tuple(
        median([abs(value - centre) for value in column]) * 1.4826
        for column, centre in zip(columns, centres)
    )
    return centres, scales


def _distance(
    left: tuple[float, ...], right: tuple[float, ...],
    scales: tuple[float, ...],
) -> float | None:
    dimensions = [
        abs(a - b) / scale
        for a, b, scale in zip(left, right, scales)
        if math.isfinite(scale) and scale > 0.0
    ]
    return sum(dimensions) / len(dimensions) if dimensions else None


def _time_series_reading(
    timeframe: str, bars: list[dict[str, Any]], reference: float | None, reference_tf: str,
) -> ConceptReading:
    closes = [float(item["close"]) for item in bars]
    returns = _returns(closes)
    squared = [value * value for value in returns]
    acf_returns = {lag: _acf(returns, lag) for lag in range(1, 6)}
    acf_squared = {lag: _acf(squared, lag) for lag in range(1, 6)}
    adf = (
        _adf_zero_lag([math.log(value) for value in closes], include_trend=False)
        if len(closes) > 8 and all(value > 0.0 for value in closes)
        else {"status": "insufficient_or_non_positive_history", "test_statistic": None}
    )
    swing_state, swing_levels = trend_state(fractal_swings(bars))
    unknowns: list[str] = []
    if len(returns) < 8:
        unknowns.append("a longer completed-return history for stable autocorrelation diagnostics")
    if any(value is None for value in acf_returns.values()):
        unknowns.append("sufficient completed returns for all five reported autocorrelation lags")
    if adf.get("status") != "statistic_available":
        unknowns.append("sufficient positive ordered closes for the stated ADF(0) statistic")
    else:
        unknowns.append(
            "ADF(0) omits lagged-difference regressors; residual dependence and structural breaks limit inference"
        )
    if bars[-1].get("source_timestamp_verified") is not True:
        unknowns.append("source-verified timestamp for the latest completed bar")
    facts = {
        "log_return_count": len(returns),
        "log_return_autocorrelation_lags_1_to_5": acf_returns,
        "squared_return_autocorrelation_lags_1_to_5": acf_squared,
        "dependence_interpretation": (
            "positive_return_dependence" if (acf_returns.get(1) or 0.0) > 0.0 else
            "negative_return_dependence" if (acf_returns.get(1) or 0.0) < 0.0 else
            "no_measured_lag_one_dependence"
        ),
        "volatility_dependence_interpretation": (
            "volatility_clustering_observed" if (acf_squared.get(1) or 0.0) > 0.0 else
            "no_positive_lag_one_squared_return_dependence"
        ),
        "adf_zero_lag_constant": adf,
        "structural_regime_from_confirmed_swings": swing_state,
        "confirmed_swing_levels": dict(swing_levels),
        "history_bar_count": len(bars),
        "statistical_tests_are_diagnostics_not_directional_forecasts": True,
        "directional_decision_emitted": False,
        "entry_authorization_emitted": False,
    }
    return ConceptReading(
        concept="time_series",
        timeframe=timeframe,
        domain=EvidenceDomain.HISTORICAL_ANALOGUE,
        applicable=len(returns) >= 3,
        applicability_reason=(
            "Ordered completed closes support return-dependence diagnostics."
            if len(returns) >= 3 else "Insufficient completed closes for return-dependence diagnostics."
        ),
        observation=(
            f"{timeframe} time-series state: lag-one return autocorrelation="
            f"{acf_returns.get(1)}; squared-return autocorrelation={acf_squared.get(1)}; "
            f"ADF(0) status={adf.get('status')}. These are diagnostics, not trade signals."
        ),
        implications=(),
        facts=facts,
        source_bar_epoch=last_source_epoch(bars),
        source_timestamp_verified=last_source_epoch(bars) is not None,
        reference_close=reference,
        reference_timeframe=reference_tf,
        unknowns=tuple(dict.fromkeys(unknowns)),
        independence_group=f"ohlc:{timeframe}",
    )


def _historical_state_rows(
    bars: list[dict[str, Any]],
) -> list[tuple[int, tuple[float, ...], dict[str, float]]]:
    rows = []
    for end in range(_STATE_WINDOW - 1, len(bars)):
        vector = _state_vector(bars, end)
        if vector is None:
            continue
        outcomes = {}
        for horizon in _ANALOGUE_HORIZONS:
            future = end + horizon
            if future < len(bars) and bars[end]["close"] > 0.0:
                outcomes[str(horizon)] = (
                    float(bars[future]["close"]) / float(bars[end]["close"]) - 1.0
                )
        rows.append((end, vector, outcomes))
    return rows


def _analogue_reading(
    timeframe: str, bars: list[dict[str, Any]], reference: float | None, reference_tf: str,
) -> ConceptReading:
    rows = _historical_state_rows(bars)
    current_index = len(bars) - 1
    current = next((vector for index, vector, _ in reversed(rows) if index == current_index), None)
    historical = [
        (index, vector, outcomes) for index, vector, outcomes in rows
        if index < current_index and all(
            index + horizon < current_index for horizon in _ANALOGUE_HORIZONS
        )
    ]
    unknowns: list[str] = []
    if current is None:
        unknowns.append(f"{_STATE_WINDOW} valid completed bars for the current market-state embedding")
    if len(historical) < 2:
        unknowns.append("at least two prior non-overlapping-in-time state observations with fully realized outcomes")
    if bars[-1].get("source_timestamp_verified") is not True:
        unknowns.append("source-verified timestamp for the latest completed bar")
    if current is None or len(historical) < 2:
        return ConceptReading(
            concept="clustering",
            timeframe=timeframe,
            domain=EvidenceDomain.HISTORICAL_ANALOGUE,
            applicable=False,
            applicability_reason="The current state or a usable historical analogue sample is unavailable.",
            observation=f"{timeframe} nearest-state analysis is unavailable; no analogues are fabricated.",
            implications=(),
            facts={
                "state_feature_names": ["five_bar_log_return", "return_volatility", "median_relative_range"],
                "current_state": list(current) if current is not None else None,
                "historical_analogue_count": len(historical),
                "nearest_historical_states": [],
                "directional_decision_emitted": False,
                "entry_authorization_emitted": False,
            },
            source_bar_epoch=last_source_epoch(bars),
        source_timestamp_verified=last_source_epoch(bars) is not None,
            reference_close=reference,
            reference_timeframe=reference_tf,
            unknowns=tuple(unknowns),
            independence_group=f"ohlc:{timeframe}",
        )

    vectors = [vector for _, vector, _ in historical]
    centres, scales = _robust_scales(vectors)
    distances = [
        (distance, index, outcomes)
        for index, vector, outcomes in historical
        if (distance := _distance(current, vector, scales)) is not None
    ]
    distances.sort(key=lambda item: (item[0], -item[1]))
    # sqrt(n) is the conventional nearest-neighbour sample-size heuristic; it
    # controls descriptive analogue count only, not a decision threshold.
    neighbour_count = max(1, math.ceil(math.sqrt(len(distances))))
    nearest = distances[:neighbour_count]
    analogue_records = [
        {
            "bar_index": index,
            "distance": distance,
            "forward_returns": dict(outcomes),
        }
        for distance, index, outcomes in nearest
    ]
    outcomes_by_horizon = {}
    for horizon in _ANALOGUE_HORIZONS:
        values = [
            item["forward_returns"][str(horizon)]
            for item in analogue_records
            if str(horizon) in item["forward_returns"]
        ]
        outcomes_by_horizon[str(horizon)] = {
            "sample_count": len(values),
            "median_forward_return": median(values) if values else None,
            "mean_forward_return": sum(values) / len(values) if values else None,
            "positive_count": sum(value > 0.0 for value in values),
            "negative_count": sum(value < 0.0 for value in values),
            "zero_count": sum(value == 0.0 for value in values),
            "outcomes_are_historical_not_forecasts": True,
        }
    if len(distances) < len(historical):
        unknowns.append("some historical state vectors have no non-zero robust scale across available dimensions")
    facts = {
        "state_feature_names": ["five_bar_log_return", "return_volatility", "median_relative_range"],
        "current_state": list(current),
        "historical_state_sample_count": len(historical),
        "nearest_neighbour_count": neighbour_count,
        "nearest_historical_states": analogue_records,
        "historical_outcomes_by_horizon": outcomes_by_horizon,
        "robust_feature_centres": list(centres),
        "robust_feature_scales": list(scales),
        "analogue_selection": "nearest_states_by_mean_absolute_mad_scaled_distance",
        "historical_analogue_results_are_not_forward_probabilities": True,
        "directional_decision_emitted": False,
        "entry_authorization_emitted": False,
    }
    return ConceptReading(
        concept="clustering",
        timeframe=timeframe,
        domain=EvidenceDomain.HISTORICAL_ANALOGUE,
        applicable=True,
        applicability_reason="A current state embedding and prior completed analogues with realized outcomes are available.",
        observation=(
            f"{timeframe} market-state analogue context: {len(nearest)} nearest historical states "
            f"from {len(historical)} eligible observations; their realized outcomes are descriptive only."
        ),
        implications=(),
        facts=facts,
        source_bar_epoch=last_source_epoch(bars),
        source_timestamp_verified=last_source_epoch(bars) is not None,
        reference_close=reference,
        reference_timeframe=reference_tf,
        unknowns=tuple(dict.fromkeys(unknowns)),
        independence_group=f"ohlc:{timeframe}",
    )


def _anomaly_reading(
    timeframe: str, bars: list[dict[str, Any]], reference: float | None, reference_tf: str,
) -> ConceptReading:
    rows = _historical_state_rows(bars)
    current_index = len(bars) - 1
    current = next((vector for index, vector, _ in reversed(rows) if index == current_index), None)
    historical = [vector for index, vector, _ in rows if index < current_index]
    unknowns: list[str] = []
    if current is None:
        unknowns.append(f"{_STATE_WINDOW} valid completed bars for the current market-state embedding")
    if len(historical) < 3:
        unknowns.append("at least three prior market-state observations for an own-history novelty comparison")
    if bars[-1].get("source_timestamp_verified") is not True:
        unknowns.append("source-verified timestamp for the latest completed bar")
    if current is None or len(historical) < 3:
        return ConceptReading(
            concept="anomaly_detection",
            timeframe=timeframe,
            domain=EvidenceDomain.HISTORICAL_ANALOGUE,
            applicable=False,
            applicability_reason="The current state or sufficient prior state distribution is unavailable.",
            observation=f"{timeframe} novelty analysis is unavailable; insufficient historical state distribution.",
            implications=(),
            facts={
                "current_state": list(current) if current is not None else None,
                "historical_state_count": len(historical),
                "novelty_distance": None,
                "novelty_own_history_percentile": None,
                "directional_decision_emitted": False,
                "entry_authorization_emitted": False,
                "novelty_is_not_a_trade_signal": True,
            },
            source_bar_epoch=last_source_epoch(bars),
        source_timestamp_verified=last_source_epoch(bars) is not None,
            reference_close=reference,
            reference_timeframe=reference_tf,
            unknowns=tuple(unknowns),
            independence_group=f"ohlc:{timeframe}",
        )
    centres, scales = _robust_scales(historical)
    historical_distances = [
        value for vector in historical
        if (value := _distance(vector, centres, scales)) is not None
    ]
    current_distance = _distance(current, centres, scales)
    novelty_percentile = (
        percentile_rank(current_distance, historical_distances)
        if current_distance is not None and historical_distances else None
    )
    if current_distance is None or novelty_percentile is None:
        unknowns.append("at least one non-constant robust-scaled state feature and its historical distance distribution")
    facts = {
        "state_feature_names": ["five_bar_log_return", "return_volatility", "median_relative_range"],
        "current_state": list(current),
        "historical_state_count": len(historical),
        "novelty_distance": current_distance,
        "novelty_own_history_percentile": novelty_percentile,
        "historical_distance_sample_count": len(historical_distances),
        "robust_feature_centres": list(centres),
        "robust_feature_scales": list(scales),
        "novelty_is_not_a_trade_signal": True,
        "novelty_requires_wider_uncertainty_not_directional_action": True,
        "directional_decision_emitted": False,
        "entry_authorization_emitted": False,
    }
    return ConceptReading(
        concept="anomaly_detection",
        timeframe=timeframe,
        domain=EvidenceDomain.HISTORICAL_ANALOGUE,
        applicable=current_distance is not None,
        applicability_reason=(
            "A robust-scaled current state can be compared with its own prior state distribution."
            if current_distance is not None else "No non-constant robust-scaled state dimensions are available."
        ),
        observation=(
            f"{timeframe} market-state novelty distance={current_distance}; "
            f"own-history percentile={novelty_percentile}. Novelty widens uncertainty; it is not a signal."
        ),
        implications=(),
        facts=facts,
        source_bar_epoch=last_source_epoch(bars),
        source_timestamp_verified=last_source_epoch(bars) is not None,
        reference_close=reference,
        reference_timeframe=reference_tf,
        unknowns=tuple(dict.fromkeys(unknowns)),
        independence_group=f"ohlc:{timeframe}",
    )


class QuantitativeStateReadings:
    """Full completed-bar readings for time series, analogues and novelty."""

    key = "quantitative_state_readings"
    family = "quantitative"
    covers_methods = ("time_series", "clustering", "anomaly_detection")
    covers_lenses: tuple[str, ...] = ()

    def read(self, symbol: str, bars_by_tf: dict[str, list[dict]]) -> list[ConceptReading]:
        reference, reference_tf = reference_price(bars_by_tf)
        readings = []
        for timeframe, raw in (bars_by_tf or {}).items():
            if timeframe not in TIMEFRAME_SECONDS:
                continue
            bars = clean_bars(raw)
            if not bars:
                continue
            for reader in (_time_series_reading, _analogue_reading, _anomaly_reading):
                reading = reader(timeframe, bars, reference, reference_tf)
                reading = ConceptReading(
                    **{
                        **reading.__dict__,
                        "independence_group": f"ohlc:{str(symbol or '').upper()}:{timeframe}",
                    }
                )
                if bars[-1].get("source_timestamp_verified") is not True:
                    reason = "source-verified timestamp for the latest completed bar"
                    if reason not in reading.unknowns:
                        reading = ConceptReading(
                            **{
                                **reading.__dict__,
                                "source_bar_epoch": None,
                                "unknowns": reading.unknowns + (reason,),
                            }
                        )
                readings.append(reading)
        return readings
