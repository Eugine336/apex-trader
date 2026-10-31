"""Evidence-grounded market context and epistemic limits for Apex System 2.

This module summarizes instrument/session/contract context, cross-market
relationships, historical evidence quality, and unknowns. It never supplies
missing values, asserts causation from correlation, or treats a model score as
measured predictive skill. It emits context for the CognitiveBrain; it cannot
authorize trades.
"""
from __future__ import annotations

from typing import Any, Iterable

from cognition.statistical_validation import validate_historical_rows


def _finite_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return number


def _measurements(item: Any) -> dict:
    value = getattr(item, "measurements", {})
    return value if isinstance(value, dict) else {}


def _verified(record: dict, *keys: str) -> bool:
    return all(record.get(key) is True for key in keys)


def analyze_market_context(evidence: Iterable[Any], symbol: str) -> dict:
    """Build an auditable context snapshot using only explicitly supplied facts.

    Unknown values stay None/unknown. A field is not considered verified merely
    because a provider or model supplied a plausible-looking value.
    """
    records = list(evidence)
    symbol = str(symbol or "")
    instrument_records = []
    session_records = []
    relationship_records = []
    cross_asset_records = []
    cross_timeframe_records = []
    historical_records = []
    execution_records = []
    contract_constraint_records = []
    unclassified = []
    missing = []

    for item in records:
        m = _measurements(item)
        domain = str(getattr(getattr(item, "domain", None), "value",
                             getattr(item, "domain", "")) or "").lower()
        source = str(getattr(item, "source_module", "") or "unknown")
        observed_symbol = str(getattr(item, "symbol", "") or "")
        if any(k in m for k in (
            "contract_size", "tick_size", "tick_value", "lot_size",
            "point_value", "quote_currency", "base_currency", "margin_rate",
            "trading_hours", "instrument_type", "minimum_volume",
            "volume_min", "volume_max", "volume_step", "minimum_stop_distance", "freeze_level", "trade_stops_level", "trade_freeze_level", "swap_long", "swap_short",
            "trade_mode", "swap_mode", "digits", "point",
        )):
            instrument_records.append({
                "source": source, "symbol": observed_symbol,
                "measurements": dict(m),
                "verified": _verified(m, "specification_verified", "source_verified"),
            })
        if domain == "session" or any(k in m for k in (
            "session_name", "market_session", "session_open", "session_close",
            "timezone", "session_timezone", "session_calendar_verified",
        )):
            session_records.append({
                "source": source, "symbol": observed_symbol,
                "measurements": dict(m),
                "verified": _verified(m, "source_verified", "session_calendar_verified"),
            })
        if domain in {"correlation", "multi_timeframe"} or any(k in m for k in (
            "correlation", "correlation_coefficient", "cross_asset_relationship",
            "lead_lag", "relative_strength",
        )):
            coefficient = _finite_number(
                m.get("correlation_coefficient", m.get("correlation"))
            )
            relationship = {
                "source": source, "symbol": observed_symbol,
                "relationship_type": "cross_timeframe_observation" if domain == "multi_timeframe" else "cross_asset_association",
                "related_symbol": m.get("related_symbol"),
                "coefficient": coefficient,
                "sample_count": m.get("sample_count"),
                # Known timestamps establish provenance, not alignment.
                # Synchronization requires an explicit, validated assertion.
                "synchronized": m.get("synchronized_timestamps") is True,
                "measurement_window": m.get("measurement_window"),
                "observed_relationship": m.get("cross_asset_relationship", m.get("alignment")),
                "causation_established": False,
                "source_verified": m.get("source_verified") is True,
            }
            relationship_records.append(relationship)
            (cross_timeframe_records if domain == "multi_timeframe" else cross_asset_records).append(relationship)
        if domain == "historical_analogue" or any(k in m for k in (
            "out_of_sample", "walk_forward", "holdout", "data_leakage_checked",
            "historical_sample_count", "backtest_sample_count",
        )):
            n = _finite_number(m.get("historical_sample_count",
                                     m.get("backtest_sample_count",
                                           m.get("sample_count"))))
            out_of_sample = m.get("out_of_sample") is True
            walk_forward = m.get("walk_forward") is True
            untouched_holdout = m.get("untouched_holdout") is True
            leakage_checked = m.get("data_leakage_checked") is True
            selection_bias_checked = m.get("selection_bias_checked") is True
            overfit_checked = m.get("overfitting_checked") is True
            rows = m.get("validation_rows")
            validation_report = (
                validate_historical_rows(
                    rows,
                    untouched_holdout=untouched_holdout,
                    multiple_testing_mitigated=selection_bias_checked and overfit_checked,
                )
                if isinstance(rows, list) else None
            )
            controls_reported = (
                out_of_sample and walk_forward and untouched_holdout
                and leakage_checked and selection_bias_checked and overfit_checked
                and validation_report is not None and validation_report.get("passed") is True
            )
            historical_records.append({
                "source": source, "symbol": observed_symbol,
                "sample_count": int(n) if n is not None and n >= 0 else None,
                "out_of_sample": out_of_sample,
                "walk_forward": walk_forward,
                "untouched_holdout": untouched_holdout,
                "data_leakage_checked": leakage_checked,
                "selection_bias_checked": selection_bias_checked,
                "overfitting_checked": overfit_checked,
                "row_validation": validation_report,
                "validation_status": "validated_controls_reported" if controls_reported else "unverified",
                "predictive_ability_demonstrated": False,
                "note": "Reported controls require independent audit; sample size alone is not predictive skill.",
            })
        if "available_multipliers" in m:
            contract_constraint_records.append({
                "source": source, "symbol": observed_symbol,
                "broker_symbol": m.get("broker_symbol"),
                "available_multipliers": m.get("available_multipliers"),
                "observed_epoch": m.get("constraints_observed_epoch"),
                "verified": _verified(m, "contract_constraints_verified", "source_verified"),
                "note": "Valid multiplier choices are contract constraints, not tick-value, contract-size, margin, or full instrument economics.",
            })
        if domain == "execution_quality" or any(k in m for k in (
            "spread", "slippage", "commission", "swap", "estimated_total_cost_r",
            "minimum_stop_distance", "execution_constraint",
        )):
            execution_records.append({
                "source": source, "symbol": observed_symbol,
                "measurements": dict(m),
                "verified": m.get("cost_verified") is True
                or _verified(m, "execution_constraints_verified", "source_verified"),
            })
        known_keys = {
            "source_kind", "source_timestamp_known", "timeframe", "timestamp",
            "timestamp_epoch", "completed", "provisional", "current_close",
            "prior_range_high", "prior_range_low", "range_position",
            "breakout_direction", "trend_state", "trend_strength", "close_change",
            "displacement", "bos", "structural_integrity", "support",
            "volume_z", "expansion", "delta", "imbalance", "alignment",
            "conflict_score", "volatility_state", "momentum_state",
            "rolling_zscore_30", "ema_spread_atr", "pattern_tags",
            "bullish_patterns", "bearish_patterns", "near_range_boundary",
            "probability_calibration_verified", "source_verified",
            "out_of_sample", "walk_forward", "untouched_holdout",
            "data_leakage_checked", "selection_bias_checked", "overfitting_checked",
            "calibration_side", "favourable_horizon_probability",
            "probability_semantics", "expected_return_r", "holdout_expected_return_r",
            "expected_return_gap", "sample_count", "brier_score", "reliability_gap",
            "calibration_method", "calibration_scope", "expected_return_method",
            "calibration_id", "calibration_artifact_id", "calibration_horizon_seconds",
            "dataset_digest", "walk_forward_folds",
            "selection_coverage", "producer_attested", "producer_version",
            "validation_protocol", "source_timestamp_epoch", "data_basis",
            "independence_group", "lineage_ids", "measurement_version",
            "sma", "sma_10", "sma_20", "sma_50", "sma_100", "sma_200",
            "ema", "ema_12", "ema_20", "ema_26", "ema_50", "ema_100", "ema_200",
            "wma", "wma_20", "wma_50", "hma", "hma_20", "vwma", "vwma_20",
            "ema_slope", "ema_spread_atr", "rsi", "rsi_2", "rsi_21", "rsi_fast",
            "rsi_slow", "stoch_rsi", "macd", "macd_signal", "macd_histogram",
            "stochastic", "stochastic_k",
            "stochastic_d", "cci", "williams_r", "roc_5", "roc_10", "adx",
            "plus_di", "minus_di", "aroon_up", "aroon_down", "trix",
            "atr", "atr_percent", "atr_percentile", "realized_volatility",
            "parkinson_volatility", "bollinger", "bollinger_upper", "bollinger_lower",
            "bollinger_width", "bollinger_percent_b", "zscore", "rolling_zscore",
            "mean_distance", "keltner",
            "keltner_upper", "keltner_lower", "donchian_high", "donchian_low",
            "obv", "mfi", "cmf", "vwap", "vwma_20", "volume_roc",
            "accumulation_distribution", "volume_source", "volume_is_tick_proxy",
            "indicator_periods", "calculation_method",
        }
        unknown_keys = sorted(str(k) for k in m if k not in known_keys and
                              k not in {
                                  "contract_size", "tick_size", "tick_value", "lot_size",
                                  "point_value", "quote_currency", "base_currency",
                                  "margin_rate", "trading_hours", "instrument_type",
                                  "minimum_volume", "volume_min", "volume_max", "volume_step", "minimum_stop_distance",
                                  "swap_long", "swap_short", "trade_mode", "swap_mode", "digits", "point", "specification_verified",
                                  "source_verified", "session_name", "market_session",
                                  "session_open", "session_close", "session_open_minutes",
                                  "minutes_to_next_session", "liquidity", "is_tradeable",
                                  "best_pairs", "clock_timezone", "timezone",
                                  "session_timezone", "session_calendar_verified",
                                  "correlation", "correlation_coefficient", "related_symbol",
                                  "sample_count", "synchronized_timestamps",
                                  "measurement_window", "cross_asset_relationship", "lead_lag",
                                  "relative_strength", "causation_established",
                                  "first_common_timestamp", "last_common_timestamp",
                                  "historical_sample_count",
                                  "backtest_sample_count", "realized_wins", "realized_losses",
                                  "realized_flat", "mean_realized_pnl_pips", "outcome_labels",
                                  "last_outcome_observed_at", "feature_timestamps_missing",
                                  "out_of_sample", "walk_forward", "untouched_holdout",
                                  "data_leakage_checked",
                                  "selection_bias_checked", "overfitting_checked", "validation_rows",
                                  "multiple_testing_mitigated", "available_multipliers",
                                  "contract_constraints_verified", "broker_symbol", "constraints_observed_epoch",
                                  "estimated_total_cost_r", "cost_verified",
                                  "execution_constraints_verified", "spread", "slippage",
                                  "commission", "swap", "execution_constraint",
                              })
        if unknown_keys:
            unclassified.append({
                "source": source, "symbol": observed_symbol,
                "measurement_keys": unknown_keys,
                "status": "unclassified_observation",
                "note": "Preserved for reasoning; not automatically promoted to a validated pattern.",
            })

    if not any(record["verified"] for record in instrument_records):
        missing.append("verified instrument contract/economic specifications (tick value, contract size, lot constraints, margin and financing)")
    if not any(record["verified"] for record in session_records):
        missing.append("verified market session calendar and instrument-specific trading hours/timezone")
    if not any(record["synchronized"] and record["source_verified"] for record in cross_timeframe_records):
        missing.append("synchronized cross-timeframe relationships with explicit timeframe and timestamp provenance")
    if not any(record["synchronized"] and record["source_verified"] for record in cross_asset_records):
        missing.append("synchronized cross-asset relationship measurements with a defined window and source provenance")
    if not any(record["validation_status"] == "validated_controls_reported" for record in historical_records):
        missing.append("historical outcome evidence with leakage, selection-bias and overfitting controls")
    if not any(record["verified"] for record in execution_records):
        missing.append("verified instrument-specific execution costs and broker constraints")

    return {
        "symbol": symbol,
        "epistemic_policy": {
            "missing_data_is_missing": True,
            "unverified_assumptions_remain_assumptions": True,
            "correlation_does_not_establish_causation": True,
            "model_confidence_is_not_predictive_validation": True,
        },
        "instrument_economics_and_contracts": {
            "records": instrument_records,
            "status": "verified_source_present" if any(r["verified"] for r in instrument_records) else
                      ("unverified_records_present" if instrument_records else "missing"),
        },
        "instrument_contract_constraints": {
            "records": contract_constraint_records,
            "status": "verified_source_present" if any(r["verified"] for r in contract_constraint_records) else
                      ("unverified_records_present" if contract_constraint_records else "missing"),
        },
        "sessions_and_execution_constraints": {
            "session_records": session_records,
            "execution_records": execution_records,
            "session_status": "verified_source_present" if any(r["verified"] for r in session_records) else
                              ("unverified_records_present" if session_records else "missing"),
            "execution_status": "verified_source_present" if any(r["verified"] for r in execution_records) else
                                ("unverified_records_present" if execution_records else "missing"),
        },
        "cross_market_relationships": {
            "records": relationship_records,
            "cross_timeframe_records": cross_timeframe_records,
            "cross_asset_records": cross_asset_records,
            "cross_timeframe_status": (
                "verified_synchronized_observations_present"
                if any(r["synchronized"] and r["source_verified"] for r in cross_timeframe_records)
                else ("unverified_observations_present" if cross_timeframe_records else "missing")
            ),
            "cross_asset_status": (
                "verified_synchronized_observations_present"
                if any(r["synchronized"] and r["source_verified"] for r in cross_asset_records)
                else ("unverified_observations_present" if cross_asset_records else "missing")
            ),
            "interpretation": "observed_association_only",
            "causation_established": False,
        },
        "historical_evidence": {
            "records": historical_records,
            "predictive_ability_demonstrated": False,
            "controls_required": [
                "chronological train/validation/test separation",
                "walk-forward evaluation",
                "untouched holdout",
                "data-leakage audit",
                "selection-bias and multiple-testing controls",
                "overfitting assessment",
                "realistic spread, slippage, commission and financing costs",
            ],
        },
        "unclassified_observations": unclassified[:100],
        "missing_information": missing,
        "cannot_infer_from_current_snapshot": [
            "causal effect from correlation or lead-lag association alone",
            "predictive edge from model confidence or in-sample performance alone",
            "contract/session facts not supplied by a verified instrument/broker source",
            "the meaning of an unfamiliar measurement without its provenance and definition",
            "market conditions beyond the freshness and coverage of supplied evidence",
        ],
    }


__all__ = ["analyze_market_context"]
