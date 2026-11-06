"""Point-in-time Bayesian, forecast-calibration and bootstrap risk readings.

These methods consume explicit outcome ledgers. Missing or unverified records
remain unknown; the readers never manufacture a prior from current price action,
treat a development sample as holdout, or present a historical bootstrap as a
forecast of the current market.
"""
from __future__ import annotations

import hashlib
import math
import random
from statistics import mean
from typing import Any

from cognition.concepts.auction_utils import clean_bars, last_source_epoch
from cognition.concepts.base import TIMEFRAME_SECONDS, ConceptReading, finite, reference_price
from cognition.contracts import EvidenceDomain


DATA_GATED_METHODS = {
    "bayesian": (
        "source-verified point-in-time concept outcomes labelled by concept and "
        "measured market state, with decision and outcome timestamps"
    ),
    "calibration": (
        "producer-attributed concept probability forecasts with source-verified "
        "outcomes, selection-complete holdout records and forecast timestamps"
    ),
    "monte_carlo": (
        "deduplicated broker-truth closed-trade R outcomes with verified close timestamps; "
        "current-plan stress additionally requires the current portfolio plan"
    ),
}


def _epoch(value: Any) -> float | None:
    parsed = finite(value)
    return parsed if parsed is not None and parsed > 0.0 else None


def _binary_outcome(value: Any) -> int | None:
    if isinstance(value, bool):
        return int(value)
    parsed = finite(value)
    if parsed in (0.0, 1.0):
        return int(parsed)
    return None


def _beta_fraction(a: float, b: float, x: float) -> float:
    """Continued fraction for the incomplete beta function (Lentz method)."""
    qab = a + b
    qap = a + 1.0
    qam = a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < 1e-300:
        d = 1e-300
    d = 1.0 / d
    result = d
    for m in range(1, 201):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        if abs(d) < 1e-300:
            d = 1e-300
        c = 1.0 + aa / c
        if abs(c) < 1e-300:
            c = 1e-300
        d = 1.0 / d
        result *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        if abs(d) < 1e-300:
            d = 1e-300
        c = 1.0 + aa / c
        if abs(c) < 1e-300:
            c = 1e-300
        d = 1.0 / d
        delta = d * c
        result *= delta
        if abs(delta - 1.0) < 3e-14:
            break
    return result


def _regularized_beta(x: float, a: float, b: float) -> float:
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    front = math.exp(
        math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b)
        + a * math.log(x) + b * math.log1p(-x)
    )
    if x < (a + 1.0) / (a + b + 2.0):
        return front * _beta_fraction(a, b, x) / a
    return 1.0 - front * _beta_fraction(b, a, 1.0 - x) / b


def _beta_quantile(probability: float, alpha: float, beta: float) -> float:
    low, high = 0.0, 1.0
    for _ in range(90):
        middle = (low + high) / 2.0
        if _regularized_beta(middle, alpha, beta) < probability:
            low = middle
        else:
            high = middle
    return (low + high) / 2.0


def _quantile(values: list[float], probability: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * probability
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def _valid_outcome_rows(
    rows: Any, symbol: str, cutoff: float,
) -> tuple[list[dict[str, Any]], int]:
    if not isinstance(rows, (list, tuple)):
        return [], 0
    valid: list[dict[str, Any]] = []
    rejected = 0
    for row in rows:
        if not isinstance(row, dict):
            rejected += 1
            continue
        decision_epoch = _epoch(row.get("decision_epoch"))
        outcome_epoch = _epoch(row.get("outcome_epoch"))
        source_epoch = _epoch(row.get("source_timestamp_epoch"))
        outcome = _binary_outcome(row.get("outcome"))
        row_symbol = str(row.get("symbol", "") or "").strip().upper()
        if (
            row_symbol != symbol
            or not str(row.get("concept", "") or "").strip()
            or row.get("source_timestamp_verified") is not True
            or decision_epoch is None or outcome_epoch is None or source_epoch is None
            or decision_epoch > outcome_epoch or outcome_epoch > cutoff
            or source_epoch < outcome_epoch or source_epoch > cutoff or outcome is None
        ):
            rejected += 1
            continue
        valid.append({
            **row,
            "concept": str(row["concept"]).strip().lower(),
            "outcome": outcome,
            "decision_epoch": decision_epoch,
            "outcome_epoch": outcome_epoch,
            "source_timestamp_epoch": source_epoch,
        })
    return valid, rejected


def _bayesian_reading(
    symbol: str, timeframe: str, bars: list[dict], context: dict[str, Any],
    reference: float | None, reference_tf: str,
) -> ConceptReading:
    cutoff = last_source_epoch(bars)
    unknowns: list[str] = []
    current_state = str(context.get("market_state", "") or "").strip().lower()
    rows, rejected = _valid_outcome_rows(
        context.get("concept_outcome_history"), symbol, cutoff or 0.0,
    )
    by_concept: dict[str, dict[str, Any]] = {}
    for concept in sorted({item["concept"] for item in rows}):
        concept_rows = [item for item in rows if item["concept"] == concept]
        state_rows = [
            item for item in concept_rows
            if current_state and str(item.get("market_state", "") or "").strip().lower() == current_state
        ]
        successes = sum(item["outcome"] for item in state_rows)
        failures = len(state_rows) - successes
        alpha, beta = 0.5 + successes, 0.5 + failures
        if state_rows:
            by_concept[concept] = {
                "market_state": current_state,
                "prior": {"alpha": 0.5, "beta": 0.5, "family": "Jeffreys"},
                "likelihood": {"successes": successes, "failures": failures},
                "posterior": {
                    "alpha": alpha,
                    "beta": beta,
                    "mean": alpha / (alpha + beta),
                    "credible_interval_95": [
                        _beta_quantile(0.025, alpha, beta),
                        _beta_quantile(0.975, alpha, beta),
                    ],
                },
                "sample_count": len(state_rows),
            }
    if cutoff is None:
        unknowns.append("source-verified completed-bar cutoff for point-in-time outcome selection")
    if not current_state:
        unknowns.append("measured current market-state label to condition the posterior")
    if not by_concept:
        unknowns.append("point-in-time concept outcome rows for the current measured market state")
    if rejected:
        unknowns.append(f"{rejected} outcome row(s) rejected for missing, invalid or future provenance")
    latest = max((item["source_timestamp_epoch"] for item in rows), default=None)
    return ConceptReading(
        concept="bayesian", timeframe=timeframe, domain=EvidenceDomain.HISTORICAL_ANALOGUE,
        applicable=bool(by_concept) and cutoff is not None,
        applicability_reason=(
            "Jeffreys-prior Beta-Binomial posteriors are available for source-verified concept outcomes "
            "in the measured current market state."
            if by_concept and cutoff is not None else
            "Current-state Bayesian updating is unavailable without a verified point-in-time outcome ledger."
        ),
        observation=(
            f"{symbol} {timeframe}: Bayesian hit-rate posteriors are reported per concept and current market state; "
            "posterior means are historical evidence, not current opportunity probabilities."
        ),
        implications=(),
        facts={
            "posterior_by_concept": by_concept,
            "current_market_state": current_state or None,
            "outcome_rows_accepted": len(rows),
            "outcome_rows_rejected": rejected,
            "prior_family": "Jeffreys Beta(0.5, 0.5)",
            "posterior_is_current_trade_probability": False,
            "directional_decision_emitted": False,
            "entry_authorization_emitted": False,
            "data_gated_reason": None if by_concept else DATA_GATED_METHODS["bayesian"],
        },
        source_bar_epoch=cutoff,
        source_timestamp_epoch=latest,
        source_timestamp_verified=cutoff is not None,
        reference_close=reference, reference_timeframe=reference_tf,
        unknowns=tuple(unknowns),
        data_basis="point_in_time_concept_outcome_ledger",
        independence_group=f"concept_outcomes:{symbol}",
    )


def _calibration_reading(
    symbol: str, timeframe: str, bars: list[dict], context: dict[str, Any],
    reference: float | None, reference_tf: str,
) -> ConceptReading:
    cutoff = last_source_epoch(bars)
    records = context.get("concept_probability_forecasts")
    accepted: list[dict[str, Any]] = []
    rejected = 0
    if isinstance(records, (list, tuple)):
        for row in records:
            if not isinstance(row, dict):
                rejected += 1
                continue
            probability = finite(row.get("forecast_probability"))
            outcome = _binary_outcome(row.get("outcome"))
            forecast_epoch = _epoch(row.get("forecast_epoch"))
            outcome_epoch = _epoch(row.get("outcome_epoch"))
            source_epoch = _epoch(row.get("source_timestamp_epoch"))
            valid = (
                str(row.get("symbol", "") or "").strip().upper() == symbol
                and bool(str(row.get("concept", "") or "").strip())
                and row.get("source_timestamp_verified") is True
                and row.get("split") == "holdout"
                and row.get("selection_complete") is True
                and probability is not None and 0.0 <= probability <= 1.0
                and outcome is not None
                and forecast_epoch is not None and outcome_epoch is not None
                and source_epoch is not None
                and forecast_epoch < outcome_epoch
                and cutoff is not None and outcome_epoch <= cutoff
                and source_epoch >= outcome_epoch and source_epoch <= cutoff
            )
            if not valid:
                rejected += 1
                continue
            accepted.append({
                "concept": str(row["concept"]).strip().lower(),
                "probability": probability,
                "outcome": outcome,
                "source_timestamp_epoch": source_epoch,
            })
    by_concept: dict[str, dict[str, Any]] = {}
    for concept in sorted({item["concept"] for item in accepted}):
        sample = [item for item in accepted if item["concept"] == concept]
        brier = mean((item["probability"] - item["outcome"]) ** 2 for item in sample)
        forecast_rate = mean(item["probability"] for item in sample)
        observed_rate = mean(item["outcome"] for item in sample)
        by_concept[concept] = {
            "sample_count": len(sample),
            "brier_score": brier,
            "mean_forecast_probability": forecast_rate,
            "observed_frequency": observed_rate,
            "reliability_gap": abs(forecast_rate - observed_rate),
            "holdout_only": True,
            "selection_complete": True,
        }
    unknowns: list[str] = []
    if cutoff is None:
        unknowns.append("source-verified completed-bar cutoff for holdout outcome selection")
    if not by_concept:
        unknowns.append("selection-complete, source-verified concept forecasts with matured holdout outcomes")
    if rejected:
        unknowns.append(f"{rejected} forecast row(s) rejected because holdout, selection or timestamp requirements failed")
    latest = max(
        (item["source_timestamp_epoch"] for item in accepted),
        default=0.0,
    )
    return ConceptReading(
        concept="calibration", timeframe=timeframe, domain=EvidenceDomain.HISTORICAL_ANALOGUE,
        applicable=bool(by_concept) and cutoff is not None,
        applicability_reason=(
            "Per-concept Brier and reliability diagnostics are computed only from selection-complete untouched holdout outcomes."
            if by_concept and cutoff is not None else
            "Forecast calibration is unavailable without mature, selection-complete holdout outcomes."
        ),
        observation=(
            f"{symbol} {timeframe}: per-concept forecast calibration is reported from untouched holdout records; "
            "these diagnostics do not veto a thesis or establish current-regime expectancy."
        ),
        implications=(),
        facts={
            "calibration_by_concept": by_concept,
            "holdout_only": True,
            "selection_complete_only": True,
            "probability_calibration_verified": False,
            "predictive_ability_demonstrated": False,
            "forecast_rows_accepted": len(accepted),
            "forecast_rows_rejected": rejected,
            "directional_decision_emitted": False,
            "entry_authorization_emitted": False,
            "data_gated_reason": None if by_concept else DATA_GATED_METHODS["calibration"],
        },
        source_bar_epoch=cutoff,
        source_timestamp_epoch=latest if latest > 0.0 else None,
        source_timestamp_verified=cutoff is not None,
        reference_close=reference, reference_timeframe=reference_tf,
        unknowns=tuple(unknowns),
        data_basis="selection_complete_holdout_forecast_outcomes",
        independence_group=f"concept_calibration:{symbol}",
    )


def _monte_carlo_reading(
    symbol: str, timeframe: str, bars: list[dict], context: dict[str, Any],
    reference: float | None, reference_tf: str,
) -> ConceptReading:
    cutoff = last_source_epoch(bars)
    records = context.get("trade_outcome_samples")
    returns: list[float] = []
    seen: set[str] = set()
    rejected = 0
    if isinstance(records, (list, tuple)):
        for row in records:
            if not isinstance(row, dict):
                rejected += 1
                continue
            trade_id = str(row.get("trade_id", "") or "").strip()
            closed_epoch = _epoch(row.get("closed_epoch"))
            source_epoch = _epoch(row.get("source_timestamp_epoch"))
            value = finite(row.get("return_r"))
            if (
                not trade_id or trade_id in seen
                or str(row.get("symbol", "") or "").strip().upper() != symbol
                or row.get("source_timestamp_verified") is not True
                or closed_epoch is None or source_epoch is None
                or cutoff is None or closed_epoch > cutoff
                or source_epoch < closed_epoch or source_epoch > cutoff
                or value is None
            ):
                rejected += 1
                continue
            seen.add(trade_id)
            returns.append(value)
    accepted_source_epochs = [
        _epoch(row.get("source_timestamp_epoch"))
        for row in records
        if isinstance(row, dict)
        and str(row.get("trade_id", "") or "").strip() in seen
        and _epoch(row.get("source_timestamp_epoch")) is not None
    ] if isinstance(records, (list, tuple)) else []
    iterations_raw = finite(context.get("monte_carlo_iterations"))
    iterations = int(iterations_raw) if iterations_raw is not None and iterations_raw >= 100 else 5000
    iterations = min(iterations, 50000)
    digest = hashlib.sha256(
        (symbol + "|" + "|".join(f"{value:.12g}" for value in returns)).encode("utf-8")
    ).hexdigest()
    seed = int(digest[:16], 16)
    rng = random.Random(seed)
    terminal_returns: list[float] = []
    max_drawdowns: list[float] = []
    if len(returns) >= 2:
        for _ in range(iterations):
            equity = 0.0
            peak = 0.0
            drawdown = 0.0
            for _ in range(len(returns)):
                equity += rng.choice(returns)
                peak = max(peak, equity)
                drawdown = max(drawdown, peak - equity)
            terminal_returns.append(equity)
            max_drawdowns.append(drawdown)
    tail = _quantile(terminal_returns, 0.05)
    tail_values = [value for value in terminal_returns if tail is not None and value <= tail]
    unknowns: list[str] = []
    if cutoff is None:
        unknowns.append("source-verified completed-bar cutoff for trade outcome selection")
    if len(returns) < 2:
        unknowns.append("at least two distinct broker-truth closed-trade R outcomes with verified close timestamps")
    if rejected:
        unknowns.append(f"{rejected} trade outcome row(s) rejected for duplicate, invalid or future provenance")
    unknowns.append(
        "bootstrap assumes the supplied trade-return sample is representative; dependence, regime drift and execution changes may invalidate it"
    )
    return ConceptReading(
        concept="monte_carlo", timeframe=timeframe, domain=EvidenceDomain.PORTFOLIO,
        applicable=len(returns) >= 2 and cutoff is not None,
        applicability_reason=(
            "A deterministic-seed bootstrap of verified closed-trade R outcomes is available for historical distribution stress."
            if len(returns) >= 2 and cutoff is not None else
            "Portfolio stress simulation is unavailable without verified broker-truth trade outcomes."
        ),
        observation=(
            f"{symbol} {timeframe}: bootstrap stress describes historical trade-sequence dispersion, "
            "not a forecast or a current trade-plan approval."
        ),
        implications=(),
        facts={
            "trade_sample_count": len(returns),
            "simulation_count": len(terminal_returns),
            "bootstrap_terminal_return_r_mean": mean(terminal_returns) if terminal_returns else None,
            "bootstrap_terminal_return_r_5pct": tail,
            "bootstrap_terminal_return_r_95pct": _quantile(terminal_returns, 0.95),
            "bootstrap_expected_shortfall_r_5pct": mean(tail_values) if tail_values else None,
            "monte_carlo_drawdown_95pct_r": _quantile(max_drawdowns, 0.95),
            "monte_carlo_drawdown_max_r": max(max_drawdowns) if max_drawdowns else None,
            "simulation_method": "deterministic_seed_iid_trade_return_bootstrap",
            "current_portfolio_plan_stressed": False,
            "directional_decision_emitted": False,
            "entry_authorization_emitted": False,
            "data_gated_reason": None if len(returns) >= 2 else DATA_GATED_METHODS["monte_carlo"],
        },
        source_bar_epoch=cutoff,
        source_timestamp_epoch=max(accepted_source_epochs) if accepted_source_epochs else None,
        source_timestamp_verified=cutoff is not None,
        reference_close=reference, reference_timeframe=reference_tf,
        unknowns=tuple(unknowns),
        data_basis="broker_closed_trade_outcomes",
        independence_group=f"trade_outcomes:{symbol}",
    )


class QuantitativeOutcomeReadings:
    key = "quantitative_outcome_readings"
    family = "quantitative"
    covers_methods: tuple[str, ...] = ()
    covers_lenses: tuple[str, ...] = ()
    data_gated_methods = DATA_GATED_METHODS

    def read(self, symbol: str, bars_by_tf: dict[str, list[dict]]) -> list[ConceptReading]:
        return self.read_with_context(symbol, bars_by_tf, {})

    def read_with_context(
        self, symbol: str, bars_by_tf: dict[str, list[dict]], context: dict[str, Any],
    ) -> list[ConceptReading]:
        reference, reference_tf = reference_price(bars_by_tf)
        readings: list[ConceptReading] = []
        for timeframe, raw in (bars_by_tf or {}).items():
            if timeframe not in TIMEFRAME_SECONDS:
                continue
            bars = clean_bars(raw)
            if not bars:
                continue
            readings.extend((
                _bayesian_reading(symbol, timeframe, bars, context or {}, reference, reference_tf),
                _calibration_reading(symbol, timeframe, bars, context or {}, reference, reference_tf),
                _monte_carlo_reading(symbol, timeframe, bars, context or {}, reference, reference_tf),
            ))
        return readings


__all__ = ["DATA_GATED_METHODS", "QuantitativeOutcomeReadings"]
